from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.call_audio import CallAudio
from app.models import TERMINAL_STATES, CallState
from tests.conftest import seed_call
from tests.test_call_audio import SILENCE, VOICE


def tool_event(tool_id="end_1", farewell="Thanks, goodbye.", delegation_id="delegation_1"):
    return {
        "type": "response.event",
        "delegation_id": delegation_id,
        "event": {
            "type": "response.output_item.done",
            "response_id": "resp_1",
            "item": {
                "type": "function_call",
                "call_id": tool_id,
                "name": "finish_call_after_goodbye",
                "arguments": json.dumps({"reason": "objective_completed", "farewell": farewell}),
            },
        },
    }


def frames(service, call_id, track="outbound", start=0, end=40, payload=VOICE):
    for ms in range(start, end, 20):
        service.observe_media_audio(call_id, track, ms, payload)


async def prepared_goodbye(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._call_audio[call_id] = CallAudio(connected=True)
    frames(service, call_id)
    await service.handle_live_event(
        call_id,
        {
            "type": "session.output_transcript.delta",
            "delta": "Thanks, goodbye.",
            "start_ms": 0,
            "end_ms": 40,
        },
    )
    return call_id


async def settle():
    await asyncio.sleep(0.1)


async def wait_for_terminal_state(service, call_id, reason: str, *, budget_seconds: float = 5.0):
    """Wait for the durable terminal state, not just the claim-time reason.

    ``termination_reason`` is written when the termination claim is taken, which is
    before the remote hangup and the final terminal write. Waiting on the real
    terminal state means callers can assert provider cleanup (hangup) afterwards
    without racing the teardown pipeline. Polling replaces a fixed sleep so a
    loaded CI runner does not turn a slow-but-correct teardown into a failure.
    """
    terminal_states = {state.value for state in TERMINAL_STATES}
    deadline = asyncio.get_running_loop().time() + budget_seconds
    call = await service.db.get_call(call_id)
    while call is not None and not (
        call["state"] in terminal_states and call["termination_reason"] == reason
    ):
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.02)
        call = await service.db.get_call(call_id)
    return call


async def test_farewell_without_native_delegation_requests_review_once(service, packet):
    call_id = await prepared_goodbye(service, packet)
    frames(service, call_id, start=40, end=400, payload=SILENCE)
    await settle()
    await service._review_spoken_closing(call_id)
    assert service.live.events.count(("closing_review", call_id)) == 1
    assert service.live.hangups == []
    assert (await service.db.get_call(call_id))["state"] == "active"


async def test_busy_backend_does_not_consume_the_pending_closing_review(
    service, packet, monkeypatch
):
    call_id = await prepared_goodbye(service, packet)
    service._call_audio[call_id].outbound.speaking = False
    review = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(service.live, "review_closing", review)
    await service._review_spoken_closing(call_id)
    assert service._live_conversations[call_id].closing_review_revision == -1
    await service.handle_live_event(
        call_id,
        {
            "type": "response.event",
            "delegation_id": "delegation_1",
            "event": {"type": "response.completed", "response": {"id": "response_1"}},
        },
    )
    await settle()
    assert review.await_count == 2
    assert service._live_conversations[call_id].closing_review_revision > 0


async def test_normal_reply_does_not_start_closing_backend_work(service, packet):
    call_id = await prepared_goodbye(service, packet)
    service._live_conversations[call_id].assistant_text = "Your appointment is at three."
    service._live_conversations[call_id].closing_candidate_revision = 0
    frames(service, call_id, start=40, end=400, payload=SILENCE)
    await settle()
    assert ("closing_review", call_id) not in service.live.events


async def test_farewell_candidate_inside_one_fragment_is_reviewed(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._call_audio[call_id] = CallAudio(connected=True)
    frames(service, call_id)
    await service.handle_live_event(
        call_id,
        {
            "type": "session.output_transcript.delta",
            "delta": "Goodbye, and thanks for the test.",
            "start_ms": 0,
            "end_ms": 40,
        },
    )
    frames(service, call_id, start=40, end=400, payload=SILENCE)
    await settle()
    assert ("closing_review", call_id) in service.live.events


async def test_overlapping_callee_word_preserves_farewell_for_backend_review(service, packet):
    call_id = await prepared_goodbye(service, packet)
    frames(service, call_id, track="inbound")
    assert service._live_conversations[call_id].assistant_text == "Thanks, goodbye."
    await service.handle_live_event(
        call_id,
        {
            "type": "session.output_transcript.delta",
            "delta": " Thanks again.",
            "start_ms": 40,
            "end_ms": 80,
        },
    )
    frames(service, call_id, track="inbound", start=40, end=400, payload=SILENCE)
    frames(service, call_id, start=40, end=400, payload=SILENCE)
    await settle()
    # Later assistant speech preserves the full overlapping farewell for semantic
    # review; the review itself still does not authorize a close.
    assert ("closing_review", call_id) in service.live.events
    assert service.live.hangups == []


async def test_verified_farewell_survives_a_trailing_nonfarewell_fragment(service, packet):
    call_id = await prepared_goodbye(service, packet)
    await service.handle_live_event(
        call_id,
        {
            "type": "session.output_transcript.delta",
            "delta": " [gasp]",
            "start_ms": 40,
            "end_ms": 80,
        },
    )
    await service.handle_live_event(call_id, tool_event())
    assert service.live.tool_results[-1][2]["accepted"] is True


async def test_reply_window_is_three_seconds_of_carrier_silence(service, packet):
    call_id = await prepared_goodbye(service, packet)
    await service.handle_live_event(call_id, tool_event())
    assert service.live.tool_results[-1][2]["status"] == "closing_pending"
    frames(service, call_id, start=40, end=3000, payload=SILENCE)
    await settle()
    assert service.live.hangups == []
    assert (await service.db.get_call(call_id))["state"] == "active"
    frames(service, call_id, start=3000, end=3060, payload=SILENCE)
    call = await wait_for_terminal_state(service, call_id, "voice_model_end_call")
    assert call["state"] in {state.value for state in TERMINAL_STATES}
    assert call["termination_reason"] == "voice_model_end_call"
    assert service.live.hangups == ["rtc_test"]


async def test_generation_completion_and_reflected_audio_do_not_prove_playback(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._call_audio[call_id] = CallAudio(connected=True)
    await service.handle_live_event(
        call_id,
        {
            "type": "session.output_transcript.delta",
            "delta": "Thanks, goodbye.",
        },
    )
    await service.handle_live_event(call_id, {"type": "session.output_audio.delta", "delta": VOICE})
    await service.handle_live_event(
        call_id,
        {
            "type": "response.event",
            "delegation_id": "delegation_1",
            "event": {"type": "response.completed", "response": {"id": "resp_1", "output": []}},
        },
    )
    await service.handle_live_event(call_id, tool_event())
    assert service.live.tool_results[-1][2]["accepted"] is False
    assert not service.live.hangups
    assert call_id not in service._voice_end_pending


@pytest.mark.parametrize("signal", ["audio", "transcript"])
async def test_callee_reply_cancels_close_before_dispatch(service, packet, signal):
    call_id = await prepared_goodbye(service, packet)
    await service.handle_live_event(call_id, tool_event())
    frames(service, call_id, start=40, end=3000, payload=SILENCE)
    if signal == "audio":
        frames(service, call_id, track="inbound")
    else:
        service._observe_live_event(
            call_id, {"type": "session.input_transcript.delta", "delta": "Wait, one more thing."}
        )
    frames(service, call_id, start=3000, end=3060, payload=SILENCE)
    await settle()
    assert (await service.db.get_call(call_id))["state"] == "active"
    assert service.live.hangups == []
    assert call_id not in service._voice_end_pending
    assert service.live.resumed_calls == [call_id]


async def test_reply_while_database_claim_is_waiting_rolls_back_close(service, packet, monkeypatch):
    call_id = await prepared_goodbye(service, packet)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = service.db.claim_termination

    async def delayed_claim(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(service.db, "claim_termination", delayed_claim)
    await service.handle_live_event(call_id, tool_event())
    frames(service, call_id, start=40, end=3060, payload=SILENCE)
    await asyncio.wait_for(entered.wait(), timeout=1)
    frames(service, call_id, track="inbound")
    release.set()
    await settle()
    call = await service.db.get_call(call_id)
    assert call["state"] == "active"
    assert call["termination_claimed"] == 0
    assert not service.live.hangups


async def test_stale_farewell_cannot_close_a_followup(service, packet):
    call_id = await prepared_goodbye(service, packet)
    frames(service, call_id, track="inbound")
    frames(service, call_id, track="inbound", start=40, end=400, payload=SILENCE)
    await service.handle_live_event(call_id, tool_event())
    assert service.live.tool_results[-1][2]["accepted"] is False
    assert not service.live.hangups
    # A new answer and a new actual farewell can close, after fresh playback silence.
    await service.handle_live_event(
        call_id,
        {"type": "session.output_transcript.delta", "delta": "It's at noon. Thanks, goodbye."},
    )
    frames(service, call_id, start=40, end=80)
    await service.handle_live_event(call_id, tool_event("end_2"))
    assert service.live.tool_results[-1][2]["accepted"] is True
    frames(service, call_id, start=80, end=3100, payload=SILENCE)
    call = await wait_for_terminal_state(service, call_id, "voice_model_end_call")
    assert call["state"] in {state.value for state in TERMINAL_STATES}
    assert service.live.hangups == ["rtc_test"]


@pytest.mark.parametrize("mode", ["gap", "stale", "disconnected", "still_speaking"])
async def test_missing_or_unfinished_playback_never_uses_timeout_fallback(service, packet, mode):
    call_id = await prepared_goodbye(service, packet)
    await service.handle_live_event(call_id, tool_event())
    if mode == "gap":
        frames(service, call_id, start=4000, end=8000, payload=SILENCE)
    elif mode == "stale":
        frames(service, call_id, start=40, end=3060, payload=SILENCE)
        service._call_audio[call_id].outbound.received_at -= 10
    elif mode == "disconnected":
        service._call_audio[call_id].connected = False
    else:
        frames(service, call_id, start=40, end=3060)
    await settle()
    assert service.live.hangups == []
    assert (await service.db.get_call(call_id))["state"] == "active"


async def test_stale_delegation_cannot_end_new_callee_turn(service, packet):
    call_id = await prepared_goodbye(service, packet)
    service._observe_live_event(
        call_id, {"type": "session.delegation.created", "delegation_id": "old"}
    )
    frames(service, call_id, track="inbound")
    await service.handle_live_event(call_id, tool_event(delegation_id="old"))
    assert service.live.tool_results[-1][2]["error"] == "request_superseded"


@pytest.mark.parametrize("farewell", ["", "Tomorrow at noon", "A made-up farewell"])
async def test_farewell_must_match_actual_latest_transcript(service, packet, farewell):
    call_id = await prepared_goodbye(service, packet)
    await service.handle_live_event(call_id, tool_event(farewell=farewell))
    assert service.live.tool_results[-1][2]["accepted"] is False
    assert call_id not in service._voice_end_pending


async def test_tool_persistence_failure_does_not_bypass_playback(service, packet, monkeypatch):
    call_id = await prepared_goodbye(service, packet)

    async def fail(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(service.db, "record_tool_call", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.handle_live_event(call_id, tool_event())
    await settle()
    assert not service.live.hangups
    assert call_id in service._voice_end_pending


async def test_reused_delegation_uses_each_responses_own_request_epoch(service, packet):
    call_id = await prepared_goodbye(service, packet)

    async def created(response_id):
        await service.handle_live_event(
            call_id,
            {
                "type": "response.event",
                "delegation_id": "delegation_1",
                "event": {"type": "response.created", "response": {"id": response_id}},
            },
        )

    await created("resp_old")
    frames(service, call_id, track="inbound")
    frames(service, call_id, track="inbound", start=40, end=400, payload=SILENCE)
    await service.handle_live_event(
        call_id,
        {
            "type": "session.output_transcript.delta",
            "delta": "Thanks, goodbye.",
            "start_ms": 400,
            "end_ms": 440,
        },
    )
    await created("resp_1")
    stale = tool_event(tool_id="old")
    stale["event"]["response_id"] = "resp_old"
    await service.handle_live_event(call_id, stale)
    assert service.live.tool_results[-1][2]["error"] == "request_superseded"
    await service.handle_live_event(call_id, tool_event(tool_id="new"))
    assert service.live.tool_results[-1][2]["status"] == "closing_pending"
