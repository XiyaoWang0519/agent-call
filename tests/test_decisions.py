from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.decisions import ClosingAnswer, ClosingDecisions
from app.models import ContextPacket
from app.policy import validate_context
from app.prompts import live_instructions
from tests.test_call_audio import SILENCE
from tests.test_voice_closing import (
    frames,
    prepared_goodbye,
    settle,
    tool_event,
    wait_for_terminal_state,
)


def answer(choice="ready_to_close", confidence=0.99):
    return {
        "type": "choice",
        "name": "closing_readiness",
        "choice": choice,
        "confidence": confidence,
        "probabilities": [
            {"value": value, "probability": 0.99 if value == choice else 0.005}
            for value in ("ready_to_close", "keep_open", "uncertain")
        ],
    }


async def test_decision_request_payload(settings):
    post = AsyncMock(return_value=SimpleNamespace(json=lambda: {"answers": [answer()]}))
    result = await ClosingDecisions(SimpleNamespace(post=post), settings).review({"revision": 3})
    assert result.choice == "ready_to_close"
    assert result.decisive(0.95)
    assert post.call_args.args == ("/decisions",)
    body = post.call_args.kwargs["body"]
    assert body["model"] == "gpt-6-luna"
    assert body["input"] == '{"revision": 3}'
    assert body["questions"][0]["name"] == "closing_readiness"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"answers": []},
        {"answers": [answer(), answer()]},
        {"answers": [{"type": "refusal", "name": "closing_readiness"}]},
        {"answers": [dict(answer(), choice="invented")]},
        {"answers": [dict(answer(), confidence=float("nan"))]},
        {"answers": [dict(answer(), confidence=2)]},
        {"answers": [dict(answer(), name="other_question")]},
        {"answers": [dict(answer(), probabilities=[])]},
        {
            "answers": [
                dict(answer(), probabilities=[{"value": "ready_to_close", "probability": 1}] * 3)
            ]
        },
        {"answers": [dict(answer(), choice="keep_open")]},
    ],
)
async def test_invalid_or_refused_decisions_fall_back(settings, payload):
    client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(json=lambda: payload)))
    assert await ClosingDecisions(client, settings).review({}) is None


async def test_timeout_and_provider_errors_are_bounded(settings):
    settings.closing_decisions_timeout_seconds = 0.01

    async def slow(*args, **kwargs):
        await asyncio.Event().wait()

    for post in (AsyncMock(side_effect=RuntimeError("private provider body")), slow):
        assert await ClosingDecisions(SimpleNamespace(post=post), settings).review({}) is None


async def test_decision_cancellation_propagates(settings):
    client = SimpleNamespace(post=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await ClosingDecisions(client, settings).review({})


async def setup_review(service, packet, mode="active", result=None):
    service.settings.closing_decisions_mode = mode
    call_id = await prepared_goodbye(service, packet)
    service._call_audio[call_id].outbound.speaking = False
    service.closing_decisions.review = AsyncMock(return_value=result)
    return call_id


async def test_active_decision_uses_shared_reply_window_without_tool_output(service, packet):
    call_id = await setup_review(service, packet, result=ClosingAnswer.model_validate(answer()))
    await service._review_spoken_closing(call_id)
    token = service._voice_end_reply_waits[call_id]
    assert service.live.tool_results == []
    assert ("closing_review", call_id) not in service.live.events
    assert service.live.hangups == []
    await service._review_spoken_closing(call_id)
    assert service._voice_end_reply_waits[call_id] is token
    assert service.closing_decisions.review.await_count == 1
    await service.handle_live_event(
        call_id, {"type": "session.input_transcript.delta", "delta": "Wait, another question."}
    )
    assert call_id not in service._voice_end_reply_waits
    assert service.live.hangups == []


async def test_responses_close_after_decision_is_idempotent(service, packet):
    call_id = await setup_review(service, packet, result=ClosingAnswer.model_validate(answer()))
    await service._review_spoken_closing(call_id)
    token = service._voice_end_reply_waits[call_id]
    await service.handle_live_event(call_id, tool_event())
    assert service._voice_end_reply_waits[call_id] is token
    assert service.live.tool_results[-1][2]["status"] == "closing_pending"
    assert service.live.hangups == []


@pytest.mark.parametrize(
    "choice,confidence,expect_backend",
    [
        ("ready_to_close", 0.99, False),
        ("keep_open", 0.99, False),
        ("uncertain", 0.99, True),
        ("ready_to_close", 0.5, True),
    ],
)
async def test_active_choices_and_confidence(service, packet, choice, confidence, expect_backend):
    call_id = await setup_review(
        service, packet, result=ClosingAnswer.model_validate(answer(choice, confidence))
    )
    await service._review_spoken_closing(call_id)
    assert (("closing_review", call_id) in service.live.events) == expect_backend
    assert (call_id in service._voice_end_reply_waits) == (
        choice == "ready_to_close" and not expect_backend
    )


async def test_disabled_skips_decisions(service, packet):
    call_id = await setup_review(service, packet, mode="disabled")
    await service._review_spoken_closing(call_id)
    service.closing_decisions.review.assert_not_awaited()
    assert ("closing_review", call_id) in service.live.events


async def test_observe_does_not_wait_for_decision_or_act(service, packet):
    call_id = await setup_review(service, packet, mode="observe")
    release = asyncio.Event()

    async def slow(evidence):
        await release.wait()
        return ClosingAnswer.model_validate(answer())

    service.closing_decisions.review.side_effect = slow
    await service._review_spoken_closing(call_id)
    assert ("closing_review", call_id) in service.live.events
    release.set()
    await settle()
    assert call_id not in service._voice_end_pending
    assert service.live.tool_results == []


@pytest.mark.parametrize("change", ["input", "output", "speech", "backend", "terminal"])
async def test_stale_result_never_closes_or_requests_stale_fallback(service, packet, change):
    call_id = await setup_review(service, packet)

    async def changed(evidence):
        state = service._live_conversations[call_id]
        if change == "input":
            state.input_revision += 1
        elif change == "output":
            state.output_revision += 1
        elif change == "speech":
            service._callee_speech_epochs[call_id] = 1
        elif change == "backend":
            state.control_revision += 1
        else:
            service._activity.tombstone(call_id)
        return ClosingAnswer.model_validate(answer())

    service.closing_decisions.review.side_effect = changed
    await service._review_spoken_closing(call_id)
    assert call_id not in service._voice_end_pending
    assert ("closing_review", call_id) not in service.live.events


@pytest.mark.parametrize("blocker", ["hold", "answer", "tool", "playback", "backend"])
async def test_active_result_preserves_pending_work_and_playback_checks(service, packet, blocker):
    call_id = await setup_review(service, packet, result=ClosingAnswer.model_validate(answer()))
    if blocker == "hold":
        service._hold_state[call_id] = object()
    elif blocker == "answer":
        service._undelivered_answers[call_id] = {"question"}
    elif blocker == "tool":
        service._inflight_tools.add(call_id)
    elif blocker == "playback":
        service._call_audio[call_id].outbound.last_voice_ms = None
    else:
        service.live.closing_review_available = lambda call_id: False
    await service._review_spoken_closing(call_id)
    assert call_id not in service._voice_end_pending
    assert service.live.hangups == []


async def test_failed_decision_uses_existing_backend(service, packet):
    call_id = await setup_review(service, packet)
    await service._review_spoken_closing(call_id)
    assert ("closing_review", call_id) in service.live.events


async def test_snapshot_timeout_falls_back(service, packet):
    call_id = await setup_review(service, packet)
    service.settings.closing_decisions_timeout_seconds = 0.01

    async def stalled(evidence):
        await asyncio.Event().wait()

    service.closing_decisions.review.side_effect = stalled
    await service._review_spoken_closing(call_id)
    assert ("closing_review", call_id) in service.live.events


async def test_decision_closing_waits_for_carrier_silence(service, packet):
    call_id = await setup_review(service, packet, result=ClosingAnswer.model_validate(answer()))
    await service._review_spoken_closing(call_id)
    frames(service, call_id, start=40, end=3000, payload=SILENCE)
    await settle()
    assert call_id in service._voice_end_pending
    assert service.live.hangups == []
    frames(service, call_id, start=3000, end=3060, payload=SILENCE)
    call = await wait_for_terminal_state(service, call_id, "voice_model_end_call")
    assert call["state"] == "completed"
    assert service.live.hangups == ["rtc_test"]


async def test_oversized_evidence_falls_back_without_decision(service, packet, monkeypatch):
    call_id = await setup_review(service, packet)
    monkeypatch.setattr(
        service.db,
        "get_transcript",
        AsyncMock(return_value=[SimpleNamespace(speaker="callee", text="x" * (64 * 1024))]),
    )
    await service._review_spoken_closing(call_id)
    service.closing_decisions.review.assert_not_awaited()
    assert ("closing_review", call_id) in service.live.events


async def test_timeout_after_new_speech_does_not_start_stale_fallback(service, packet):
    call_id = await setup_review(service, packet)
    service.settings.closing_decisions_timeout_seconds = 0.01

    async def stalled(evidence):
        service._live_conversations[call_id].input_revision += 1
        await asyncio.Event().wait()

    service.closing_decisions.review.side_effect = stalled
    await service._review_spoken_closing(call_id)
    assert ("closing_review", call_id) not in service.live.events
    assert call_id not in service._voice_end_pending


def test_empty_spoken_facts_preserve_existing_serialized_plan_size(packet):
    assert "spoken_facts" not in packet.model_dump()
    assert "spoken_facts" not in packet.approved_context_json()


def test_large_restrictions_fall_back_to_backend_instead_of_inflating_voice_prompt(packet):
    packet.spoken_facts = ["Party size is four."]
    packet.hard_constraints = ["x" * 4096]
    voice = live_instructions(
        packet, web_search_enabled=True, ask_agent_enabled=False, hold_detection_enabled=False
    )
    assert "Party size is four." not in voice
    assert "x" * 4096 not in voice


def test_spoken_facts_are_explicit_and_restrictions_reach_live(packet, settings):
    packet.spoken_facts = ["Party size is four."]
    packet.relevant_facts = ["Backend-only account notes."]
    packet.hard_constraints = ["Do not accept a deposit."]
    voice = live_instructions(
        packet, web_search_enabled=True, ask_agent_enabled=False, hold_detection_enabled=False
    )
    assert "Party size is four." in voice
    assert "Backend-only account notes." not in voice
    assert "Do not accept a deposit." in voice
    packet.spoken_facts = ["The password is secret."]
    assert any(error.code == "sensitive_data" for error in validate_context(packet, settings))


@pytest.mark.parametrize("facts", [["x"] * 9, ["x" * 241], [""], ["界" * 240] * 3])
def test_spoken_facts_are_bounded(packet, facts):
    with pytest.raises(ValidationError):
        ContextPacket.model_validate(dict(packet.model_dump(), spoken_facts=facts))
