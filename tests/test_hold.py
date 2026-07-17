from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from app.call_state import HoldState
from app.models import CallState
from tests.conftest import seed_call, wait_background


def _tool_event(tool_call_id: str, name: str, arguments: str) -> dict[str, str]:
    return {
        "type": "response.function_call_arguments.done",
        "event_id": f"evt_{tool_call_id}",
        "call_id": tool_call_id,
        "name": name,
        "arguments": arguments,
    }


def _transcript_event(text: str, item_id: str = "item_1") -> dict[str, str]:
    return {
        "type": "conversation.item.input_audio_transcription.completed",
        "event_id": f"evt_{item_id}",
        "item_id": item_id,
        "transcript": text,
    }


async def _make_call_stale(service, call_id: str) -> None:
    stale = datetime.now(UTC) - timedelta(minutes=1)
    await service.db.execute(
        "UPDATE calls SET last_event_at=? WHERE call_id=?", (stale.isoformat(), call_id)
    )


@pytest.fixture
async def hold_service(service):
    service.settings.hold_detection_enabled = True
    return service


@pytest.mark.asyncio
async def test_hold_phrase_in_transcript_enters_hold(hold_service, packet):
    call_id = await seed_call(hold_service.db, packet, state=CallState.ACTIVE)

    await hold_service.handle_realtime_event(
        call_id, _transcript_event("Please hold while we connect you.")
    )
    await wait_background()

    assert call_id in hold_service._hold_state
    assert call_id in hold_service._test_realtime.suspend_calls


@pytest.mark.asyncio
async def test_hold_detection_disabled_ignores_hold_phrase(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)

    await service.handle_realtime_event(
        call_id, _transcript_event("Please hold while we connect you.")
    )
    await wait_background()

    assert call_id not in service._hold_state
    assert service._test_realtime.suspend_calls == []


@pytest.mark.asyncio
async def test_watchdog_does_not_terminate_while_on_hold(hold_service, packet):
    call_id = await seed_call(hold_service.db, packet, state=CallState.ACTIVE)
    await _make_call_stale(hold_service, call_id)
    hold_service._hold_state[call_id] = HoldState(started_monotonic=time.monotonic())

    await hold_service._watchdog_once()

    call = await hold_service.db.get_call(call_id)
    assert call["state"] == CallState.ACTIVE.value
    assert call_id in hold_service._hold_state
    assert call_id not in hold_service._watchdog_claims


@pytest.mark.asyncio
async def test_watchdog_terminates_after_hold_budget_exceeded(hold_service, packet):
    hold_service.settings.hold_max_seconds = 0.01
    call_id = await seed_call(hold_service.db, packet, state=CallState.ACTIVE)
    hold_service._hold_state[call_id] = HoldState(started_monotonic=time.monotonic() - 1)

    await hold_service._watchdog_once()
    await wait_background()

    call = await hold_service.db.get_call(call_id)
    assert call["state"] == CallState.TIMED_OUT.value
    assert call["termination_reason"] == "hold_timeout"
    assert call_id not in hold_service._hold_state


@pytest.mark.asyncio
async def test_non_hold_transcript_exits_hold_and_sends_resume_nudge(hold_service, packet):
    call_id = await seed_call(hold_service.db, packet, state=CallState.ACTIVE)
    hold_service._hold_state[call_id] = HoldState(started_monotonic=time.monotonic())

    await hold_service.handle_realtime_event(
        call_id, _transcript_event("Sorry about that, I'm back now.")
    )
    await wait_background()

    assert call_id not in hold_service._hold_state
    assert ("session.update", call_id) in hold_service._test_realtime.events
    resumes = [c for c in hold_service._test_realtime.request_response_calls if c[0] == call_id]
    assert resumes
    assert "I'm back now" in resumes[-1][1]


@pytest.mark.asyncio
async def test_hold_reannouncement_stays_on_hold(hold_service, packet):
    call_id = await seed_call(hold_service.db, packet, state=CallState.ACTIVE)
    hold_service._hold_state[call_id] = HoldState(started_monotonic=time.monotonic())

    await hold_service.handle_realtime_event(
        call_id,
        _transcript_event("Your call is important to us, please continue to hold."),
    )
    await wait_background()

    assert call_id in hold_service._hold_state
    assert hold_service._test_realtime.request_response_calls == []
    assert ("session.update", call_id) not in hold_service._test_realtime.events


@pytest.mark.asyncio
async def test_report_hold_tool_enters_hold_and_suppresses_continuation(hold_service, packet):
    call_id = await seed_call(hold_service.db, packet, state=CallState.ACTIVE)

    await hold_service.handle_realtime_event(
        call_id, _tool_event("tc_hold", "report_hold", '{"reason": "hold music"}')
    )
    await wait_background()

    assert call_id in hold_service._hold_state
    assert call_id in hold_service._test_realtime.suspend_calls
    result_index = next(
        index
        for index, result in enumerate(hold_service._test_realtime.tool_results)
        if result[1] == "tc_hold"
    )
    assert hold_service._test_realtime.tool_results[result_index][2] == {"status": "holding"}
    assert hold_service._test_realtime.tool_result_continuations[result_index] is False


@pytest.mark.asyncio
async def test_report_hold_tool_when_not_active_leaves_model_talking(hold_service, packet):
    call_id = await seed_call(hold_service.db, packet, state=CallState.PREWARMING)

    await hold_service.handle_realtime_event(call_id, _tool_event("tc_hold_2", "report_hold", "{}"))
    await wait_background()

    assert call_id not in hold_service._hold_state
    result_index = next(
        index
        for index, result in enumerate(hold_service._test_realtime.tool_results)
        if result[1] == "tc_hold_2"
    )
    assert hold_service._test_realtime.tool_results[result_index][2] == {"status": "not_on_hold"}
    assert hold_service._test_realtime.tool_result_continuations[result_index] is True


@pytest.mark.asyncio
async def test_termination_while_on_hold_clears_hold_state(hold_service, packet):
    call_id = await seed_call(hold_service.db, packet, state=CallState.ACTIVE)
    hold_service._hold_state[call_id] = HoldState(started_monotonic=time.monotonic())

    assert await hold_service.terminate_call(call_id, "owner_request") is True
    await wait_background()

    assert call_id not in hold_service._hold_state


@pytest.mark.asyncio
async def test_enter_hold_returns_false_when_suspend_fails(hold_service, packet):
    call_id = await seed_call(hold_service.db, packet, state=CallState.ACTIVE)
    hold_service._test_realtime.suspend_failures_remaining = 1

    entered = await hold_service._enter_hold(call_id, trigger="test")

    assert entered is False
    assert call_id not in hold_service._hold_state
    call = await hold_service.db.get_call(call_id)
    assert call["state"] == CallState.ACTIVE.value
