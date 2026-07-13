from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import CallState
from tests.conftest import seed_call, wait_background


@pytest.mark.asyncio
async def test_callee_exit_completes_conference_and_hangup_exactly_once(service, packet):
    call_id = await seed_call(service.db, packet)
    form = {
        "StatusCallbackEvent": "participant-leave",
        "ParticipantLabel": "callee",
        "CallSid": "CA" + "b" * 32,
    }
    await service.handle_conference_event(call_id, form)
    await service.handle_conference_event(call_id, form)
    await wait_background()
    assert len(service._test_twilio.completed) == 1
    assert service._test_realtime.hangups == ["rtc_test"]
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.COMPLETED.value
    assert service._test_finalizer.states_seen == [CallState.COMPLETED.value]


@pytest.mark.asyncio
async def test_transfer_removes_ai_without_completing_owner_callee_conference(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.GREETING_STARTED)
    event = service._owner_join_events.setdefault(call_id, __import__("asyncio").Event())
    event.set()
    result = await service.transfer_to_owner(call_id, "owner needed")
    await wait_background()
    assert result["accepted"] is True
    assert service._test_twilio.removed == [("CF" + "a" * 32, "CA" + "a" * 32)]
    assert service._test_twilio.completed == []
    assert (await service.db.get_call(call_id))["state"] == CallState.TRANSFERRED.value


@pytest.mark.asyncio
async def test_agent_completed_race_during_transfer_preserves_conference(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.GREETING_STARTED)
    service._owner_join_events.setdefault(call_id, __import__("asyncio").Event()).set()
    original_remove = service._test_twilio.remove_participant

    async def remove_with_racing_callback(conference, participant_call_sid):
        await service.handle_participant_status(call_id, "agent", {"CallStatus": "completed"})
        await original_remove(conference, participant_call_sid)

    service._test_twilio.remove_participant = remove_with_racing_callback
    result = await service.transfer_to_owner(call_id, "owner needed")
    await wait_background()

    assert result["accepted"] is True
    assert service._test_twilio.completed == []
    assert (await service.db.get_call(call_id))["state"] == CallState.TRANSFERRED.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("time_limit", CallState.TIMED_OUT),
        ("watchdog_stale", CallState.TIMED_OUT),
    ],
)
async def test_timeout_paths_persist_terminal_result(service, packet, reason, expected):
    call_id = await seed_call(service.db, packet, call_id=f"call_{reason}")
    await service.terminate_call(call_id, reason)
    await wait_background()
    assert (await service.db.get_call(call_id))["state"] == expected.value
    result = await service.get_result(call_id)
    assert result["state"] == expected
    assert result["result"]["call_status"] == "timed_out"


@pytest.mark.asyncio
async def test_restart_recovery_retries_claim_and_finalizes(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.db.update_call(call_id, termination_claimed=1)
    await service.recover_startup()
    await wait_background()
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "startup_recovery"
    assert await service.db.get_result(call_id) is not None
    assert service._test_finalizer.states_seen == [CallState.FAILED.value]


@pytest.mark.asyncio
async def test_restart_recovery_finalizes_terminal_call_missing_result(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.COMPLETED)
    await service.recover_startup()
    assert await service.db.get_result(call_id) is not None
    assert service._test_finalizer.states_seen == [CallState.COMPLETED.value]


@pytest.mark.asyncio
async def test_restart_recovery_resumes_telephony_only_checkpoint(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.COMPLETED)
    await service._test_finalizer.finalize(call_id)
    service._test_finalizer.states_seen.clear()

    await service.recover_startup()

    assert service._test_finalizer.states_seen == [CallState.COMPLETED.value]


@pytest.mark.asyncio
async def test_watchdog_finds_stale_call(service, packet):
    call_id = await seed_call(service.db, packet)
    stale = datetime.now(UTC) - timedelta(minutes=1)
    await service.db.update_call(call_id, last_event_at=stale.isoformat())
    # update_call refreshes last_event_at by design, so set the authoritative field directly.
    await service.db.execute(
        "UPDATE calls SET last_event_at=? WHERE call_id=?", (stale.isoformat(), call_id)
    )
    await service._watchdog_once()
    await wait_background()
    assert (await service.db.get_call(call_id))["state"] == CallState.TIMED_OUT.value


@pytest.mark.asyncio
async def test_twilio_time_limit_callback_produces_terminal_result(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.handle_participant_status(
        call_id,
        "callee",
        {"CallStatus": "completed", "CallDuration": str(service.settings.max_call_seconds)},
    )
    await wait_background()
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.TIMED_OUT.value
    assert call["termination_reason"] == "time_limit"
    assert await service.db.get_result(call_id) is not None


@pytest.mark.asyncio
async def test_setup_deadline_times_out_only_unactivated_calls(service, packet):
    waiting_call = await seed_call(service.db, packet, call_id="call_waiting")
    active_voicemail = await seed_call(
        service.db,
        packet,
        call_id="call_voicemail",
        state=CallState.GREETING_STARTED,
        openai_call_id="rtc_voicemail",
    )
    await service.db.update_call(active_voicemail, voicemail_sent=1, greeting_sent=0)
    service.settings.setup_deadline_seconds = 0

    await service._setup_deadline(waiting_call)
    await service._setup_deadline(active_voicemail)
    await wait_background()

    assert (await service.db.get_call(waiting_call))["state"] == CallState.TIMED_OUT.value
    assert (await service.db.get_call(active_voicemail))[
        "state"
    ] == CallState.GREETING_STARTED.value


@pytest.mark.asyncio
async def test_get_result_never_in_progress_after_terminal(service, packet):
    call_id = await seed_call(service.db, packet)
    await service.terminate_call(call_id, "callee_call_completed")
    response = await service.get_result(call_id)
    assert response["state"] == CallState.COMPLETED
    assert response["result"] is not None
