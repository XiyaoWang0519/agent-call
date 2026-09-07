from __future__ import annotations

import asyncio

import pytest

from app.models import CallState
from tests.conftest import seed_call, wait_background
from tests.test_voice_closing import classification_event, finish_playback, spoken_farewell


async def wait_for_attempt(service, count):
    async with asyncio.timeout(1):
        event = service._test_realtime.closing_check_started.setdefault(count, asyncio.Event())
        await event.wait()


def rejection(attempt=1):
    return {
        "type": "error",
        "error": {
            "event_id": f"closing_check_goodbye_spoken_{attempt}",
            "code": "server_error",
        },
    }


@pytest.fixture(autouse=True)
def short_recovery_deadlines(monkeypatch, service):
    monkeypatch.setattr("app.call_state.CLOSING_CHECK_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr("app.call_state.CLOSING_CHECK_RETRY_DELAY_SECONDS", 0.01)
    monkeypatch.setattr("app.call_state.VOICE_END_REPLY_GRACE_SECONDS", 0.02)
    realtime = service._test_realtime
    realtime.closing_check_started = {}
    original = realtime.check_spoken_closing

    async def note_attempt(call_id, response_id, *, request_id):
        await original(call_id, response_id, request_id=request_id)
        realtime.closing_check_started.setdefault(
            len(realtime.closing_checks), asyncio.Event()
        ).set()

    monkeypatch.setattr(realtime, "check_spoken_closing", note_attempt)


async def test_failed_send_retries_and_closes_after_original_playback(service, packet, monkeypatch):
    original = service._test_realtime.check_spoken_closing

    async def fail_first_send(call_id, response_id, *, request_id):
        await original(call_id, response_id, request_id=request_id)
        if request_id.endswith("_1"):
            raise ConnectionError("injected send failure")

    monkeypatch.setattr(service._test_realtime, "check_spoken_closing", fail_first_send)
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await spoken_farewell(service, call_id)
    await wait_for_attempt(service, 2)
    await service.handle_realtime_event(call_id, classification_event(attempt=2))
    assert service._test_realtime.hangups == []
    assert service._audio_drain_terminations[call_id] == (
        "goodbye_spoken",
        "voice_model_end_call",
    )
    await finish_playback(service, call_id, "spoken")
    await wait_background()
    assert (await service.db.get_call(call_id))["termination_reason"] == "voice_model_end_call"
    assert service._test_realtime.hangups == ["rtc_test"]
    assert service._test_realtime.tool_results == []
    assert service._test_realtime.request_response_calls == []


async def test_provider_rejection_retries_and_late_attempt_cannot_override_recovery(
    service, packet
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await spoken_farewell(service, call_id)
    await service.handle_realtime_event(call_id, rejection())
    await wait_for_attempt(service, 2)
    await service.handle_realtime_event(call_id, rejection())
    # Even an older successful classification is obsolete, but its usage is billed.
    old = classification_event("CONTINUE")
    old["response"]["usage"] = {
        "input_token_details": {"text_tokens": 12},
        "output_token_details": {"text_tokens": 1},
    }
    await service.handle_realtime_event(call_id, old)
    await service.handle_realtime_event(call_id, old)
    assert not service._closing_check_attempts[(call_id, "goodbye_spoken")].result.done()
    assert (await service.db.get_call(call_id))["realtime_input_text_tokens"] == 12
    await finish_playback(service, call_id, "spoken")
    stopped_at = service._voice_response_audio[call_id]["goodbye_spoken"].stopped_at
    await service.handle_realtime_event(call_id, classification_event(attempt=2))
    assert service._voice_response_audio[call_id]["goodbye_spoken"].stopped_at == stopped_at
    await wait_background()
    assert service._test_realtime.hangups == ["rtc_test"]


@pytest.mark.parametrize(
    "status,text", [("failed", ""), ("incomplete", "FINISHED"), ("completed", "Probably FINISHED")]
)
async def test_unsuccessful_classification_retries_without_treating_it_as_finished(
    service, packet, status, text
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await spoken_farewell(service, call_id)
    await service.handle_realtime_event(call_id, classification_event(text, status))
    await wait_for_attempt(service, 2)
    assert call_id not in service._audio_drain_terminations
    await service.handle_realtime_event(call_id, classification_event("CONTINUE", attempt=2))
    await wait_background()
    assert len(service._test_realtime.closing_checks) == 2
    assert service._test_realtime.hangups == []


@pytest.mark.parametrize("stall_send", [False, True])
async def test_stalled_send_or_missing_result_is_bounded_and_retried(
    service, packet, monkeypatch, stall_send
):
    original = service._test_realtime.check_spoken_closing

    async def send(call_id, response_id, *, request_id):
        await original(call_id, response_id, request_id=request_id)
        if stall_send and request_id.endswith("_1"):
            await asyncio.Event().wait()

    monkeypatch.setattr(service._test_realtime, "check_spoken_closing", send)
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await spoken_farewell(service, call_id)
    await finish_playback(service, call_id, "spoken")
    await wait_for_attempt(service, 2)
    await service.handle_realtime_event(call_id, classification_event(attempt=2))
    await wait_background()
    assert service._test_realtime.hangups == ["rtc_test"]
    assert len(service._test_realtime.closing_checks) == 2


async def test_recovery_is_bounded_and_never_guesses_completion(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await spoken_farewell(service, call_id)
    await service.handle_realtime_event(call_id, rejection())
    await wait_for_attempt(service, 2)
    await service.handle_realtime_event(call_id, rejection(2))
    await wait_background()
    audio = service._voice_response_audio[call_id]["goodbye_spoken"]
    assert not audio.closing_check_requested
    # A duplicate response.done must not reset the per-response attempt budget.
    service._request_spoken_closing_check(
        call_id,
        {
            "id": "goodbye_spoken",
            "status": "completed",
            "output": [{"content": [{"transcript": "Thanks, goodbye!"}]}],
        },
    )
    await asyncio.sleep(0)
    assert len(service._test_realtime.closing_checks) == 2
    assert service._test_realtime.hangups == []
    assert service._closing_check_attempts == {}
    assert service._closing_check_tasks == {}
    assert (await service.db.get_call(call_id))["state"] == CallState.ACTIVE.value


@pytest.mark.parametrize("change", ["speech", "cleared", "new_response", "hold"])
async def test_conversation_change_before_retry_prevents_stale_close(
    service, packet, monkeypatch, change
):
    monkeypatch.setattr("app.call_state.CLOSING_CHECK_RETRY_DELAY_SECONDS", 0.05)
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await spoken_farewell(service, call_id)
    await service.handle_realtime_event(call_id, rejection())
    if change == "speech":
        service._observe_opening_event(call_id, {"type": "input_audio_buffer.speech_started"})
        service._observe_opening_event(call_id, {"type": "input_audio_buffer.speech_stopped"})
    elif change == "cleared":
        service._observe_opening_event(
            call_id, {"type": "output_audio_buffer.cleared", "response_id": "goodbye_spoken"}
        )
    elif change == "hold":
        service.settings.hold_detection_enabled = True
        await service._enter_hold(call_id, trigger="transcript")
    else:
        service._observe_opening_event(
            call_id, {"type": "response.created", "response": {"id": "new_response"}}
        )
    await wait_background()
    assert len(service._test_realtime.closing_checks) == 1
    assert service._test_realtime.hangups == []
    assert service._closing_check_tasks == {}


async def test_termination_cancels_inflight_closing_check(service, packet, monkeypatch):
    original = service._test_realtime.check_spoken_closing
    cancelled = asyncio.Event()

    async def blocked_send(call_id, response_id, *, request_id):
        await original(call_id, response_id, request_id=request_id)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(service._test_realtime, "check_spoken_closing", blocked_send)
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await spoken_farewell(service, call_id)
    await service.terminate_call(call_id, "callee_participant_leave")
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert service._closing_check_tasks == {}
    assert service._closing_check_attempts == {}
    assert service._test_realtime.hangups == ["rtc_test"]


async def test_received_decision_does_not_retry_while_usage_persistence_waits(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await spoken_farewell(service, call_id)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = service._record_realtime_usage

    async def slow_usage(cid, response):
        entered.set()
        await release.wait()
        await original(cid, response)

    monkeypatch.setattr(service, "_record_realtime_usage", slow_usage)
    decision = asyncio.create_task(service.handle_realtime_event(call_id, classification_event()))
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.sleep(0.15)
    assert len(service._test_realtime.closing_checks) == 1
    release.set()
    await decision
    await finish_playback(service, call_id, "spoken")
    await wait_background()
    assert service._test_realtime.hangups == ["rtc_test"]
