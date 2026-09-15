"""R04: remote resources stay owned across the whole create -> persist -> cleanup window.

Twilio's sync SDK runs in a worker thread. Cancelling the awaiting coroutine does
not cancel the remote create, so the protected region must span three steps:
the create, the persistence of the returned SID, and cleanup of whatever was
created when either of those is interrupted. These tests cover the failure
combinations, not just the happy cancellation path:

- cancel + remote finally succeeds
- cancel + remote finally fails
- cancel while the SID is being persisted
- remote succeeds but persistence fails (database unavailable)
- cancel during media-stream creation
- late creation during shutdown
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.models import TERMINAL_STATES, CallState, PreparePhoneCallInput
from app.twilio_bridge import ParticipantInfo
from tests.conftest import seed_call, wait_background


async def _prepare(service, packet):
    return await service.prepare(
        PreparePhoneCallInput(
            context=packet,
            authority_basis="Owner explicitly requested this call",
            requested_by_owner=True,
        )
    )


async def _start(service, prepared):
    return await service.start(
        prepared.plan_id,
        explicit_confirmation=True,
        confirmation_text=prepared.confirmation_summary,
    )


@pytest.mark.asyncio
async def test_cancelled_agent_create_removes_late_leg_and_terminalizes(service, packet):
    prepared = await _prepare(service, packet)
    entered = asyncio.Event()
    release = asyncio.Event()
    late = ParticipantInfo("CA" + "z" * 32, "CF" + "z" * 32)

    async def slow_create(**_kwargs):
        entered.set()
        await release.wait()
        return late

    service.twilio.create_agent_participant = slow_create
    task = asyncio.create_task(_start(service, prepared))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_background()

    calls = await service.db.list_calls()
    assert len(calls) == 1
    call = calls[0]
    assert call["twilio_ai_call_sid"] == late.call_sid
    assert call["conference_sid"] == late.conference_sid
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "agent_leg_setup_failed"
    assert service.twilio.removed == [(late.conference_sid, late.call_sid)]
    assert service.twilio.completed == [late.conference_sid]


@pytest.mark.asyncio
async def test_cancelled_callee_create_removes_late_leg_and_terminalizes(service, packet):
    call_id = await seed_call(service.db, packet)
    entered = asyncio.Event()
    release = asyncio.Event()
    late = ParticipantInfo("CA" + "y" * 32, "CF" + "y" * 32)

    async def slow_create(**_kwargs):
        entered.set()
        await release.wait()
        return late

    service.twilio.create_callee_participant = slow_create
    task = asyncio.create_task(service.handle_sideband_open(call_id))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_background()

    call = await service.db.get_call(call_id)
    assert call["twilio_callee_call_sid"] == late.call_sid
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "callee_leg_setup_failed"
    assert (late.conference_sid, late.call_sid) in service.twilio.removed


@pytest.mark.asyncio
async def test_cancelled_agent_create_with_definite_failure_still_terminalizes(service, packet):
    prepared = await _prepare(service, packet)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def failing_create(**_kwargs):
        entered.set()
        await release.wait()
        raise RuntimeError("twilio rejected the create")

    service.twilio.create_agent_participant = failing_create
    task = asyncio.create_task(_start(service, prepared))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_background()

    call = (await service.db.list_calls())[0]
    # A definite failure has no remote leg to remove, but the call row already
    # exists and must not be left occupying single-call capacity in prewarming.
    assert call["twilio_ai_call_sid"] is None
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "agent_leg_setup_failed"
    assert service.twilio.removed == []


@pytest.mark.asyncio
async def test_cancel_while_persisting_sid_removes_leg(service, packet, monkeypatch):
    prepared = await _prepare(service, packet)
    original_update = service.db.update_call
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_update(call_id, **values):
        if "twilio_ai_call_sid" in values:
            entered.set()
            await release.wait()
        return await original_update(call_id, **values)

    monkeypatch.setattr(service.db, "update_call", slow_update)
    task = asyncio.create_task(_start(service, prepared))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_background()

    call = (await service.db.list_calls())[0]
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "agent_leg_setup_failed"
    # The leg is removed with the SID we held, not left for a retry to rediscover.
    assert service.twilio.removed == [("CF" + "a" * 32, "CA" + "a" * 32)]


@pytest.mark.asyncio
async def test_persist_failure_removes_leg_without_database(service, packet, monkeypatch):
    prepared = await _prepare(service, packet)

    async def db_down(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    # Both the ownership write and the DB-backed termination path are unavailable;
    # targeted provider cleanup must still run because it only needs the SID.
    monkeypatch.setattr(service.db, "update_call", db_down)
    monkeypatch.setattr(service.db, "claim_termination", db_down)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await _start(service, prepared)
    await wait_background()

    assert service.twilio.removed == [("CF" + "a" * 32, "CA" + "a" * 32)]


@pytest.mark.asyncio
async def test_definite_agent_create_failure_is_not_treated_as_unknown(service, packet):
    prepared = await _prepare(service, packet)

    async def fail(**_kwargs):
        raise RuntimeError("twilio rejected the create")

    service.twilio.create_agent_participant = fail
    with pytest.raises(RuntimeError, match="twilio rejected the create"):
        await _start(service, prepared)
    await wait_background()

    calls = await service.db.list_calls()
    assert len(calls) == 1
    call = calls[0]
    # A definite RPC failure has no remote SID to record and must not be retried
    # as if the result were merely unknown.
    assert call["twilio_ai_call_sid"] is None
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "agent_leg_setup_failed"


@pytest.mark.asyncio
async def test_cancelled_media_stream_is_stopped_and_call_terminalized(service, packet):
    call_id = await seed_call(service.db, packet)
    entered = asyncio.Event()
    release = asyncio.Event()
    stream_sid = "MZ" + "q" * 32

    async def slow_stream(**_kwargs):
        entered.set()
        await release.wait()
        return stream_sid

    service.twilio.start_audio_monitor = slow_stream
    task = asyncio.create_task(service._start_media_monitor(await service.db.get_call(call_id)))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_background()

    call = await service.db.get_call(call_id)
    assert call["media_stream_sid"] == stream_sid
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "media_monitor_setup_failed"
    assert service.twilio.stopped_streams == [("CA" + "b" * 32, stream_sid)]


@pytest.mark.asyncio
async def test_late_creation_during_shutdown_is_still_cleaned_up(service, packet):
    call_id = await seed_call(service.db, packet)
    entered = asyncio.Event()
    release = asyncio.Event()
    late = ParticipantInfo("CA" + "x" * 32, "CF" + "x" * 32)

    async def slow_create(**_kwargs):
        entered.set()
        await release.wait()
        return late

    service.twilio.create_callee_participant = slow_create
    service._stopping = True
    task = asyncio.create_task(service.handle_sideband_open(call_id))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    service._stopping = False
    await wait_background()

    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.FAILED.value
    assert (late.conference_sid, late.call_sid) in service.twilio.removed


@pytest.mark.asyncio
async def test_stop_waits_for_inflight_create_and_abandons_late_leg(service, packet):
    """A real stop() must not return while an owned create is still unreturned."""
    prepared = await _prepare(service, packet)
    entered = asyncio.Event()
    release = asyncio.Event()
    late = ParticipantInfo("CA" + "w" * 32, "CF" + "w" * 32)

    async def slow_create(**_kwargs):
        entered.set()
        await release.wait()
        return late

    service.twilio.create_agent_participant = slow_create
    start_task = asyncio.create_task(_start(service, prepared))
    await asyncio.wait_for(entered.wait(), timeout=1)

    stop_task = asyncio.create_task(service.stop())
    await asyncio.sleep(0.05)
    # stop() has terminated the call and is draining the must-finish owned create.
    assert not stop_task.done()

    release.set()
    await asyncio.wait_for(stop_task, timeout=5)

    # The caller was not cancelled; it learns the call stopped accepting a leg.
    with pytest.raises(ValueError) as exc_info:
        await start_task
    assert json.loads(str(exc_info.value))["code"] == "call_ending"

    assert (late.conference_sid, late.call_sid) in service.twilio.removed
    stored = await service.db.get_call((await service.db.list_calls())[0]["call_id"])
    assert stored["state"] in {state.value for state in TERMINAL_STATES}


@pytest.mark.asyncio
async def test_failed_targeted_leg_cleanup_keeps_retrying(service, packet, monkeypatch):
    """A terminal/transferred call with one cleanup failure must stay owned."""
    monkeypatch.setattr("app.call_state.RESOURCE_CLEANUP_RETRY_BASE_SECONDS", 0.01)
    monkeypatch.setattr("app.call_state.RESOURCE_CLEANUP_RETRY_MAX_SECONDS", 0.02)
    call_id = await seed_call(service.db, packet, state=CallState.TRANSFERRED)
    attempts: list[str] = []
    succeeded = asyncio.Event()
    late = ParticipantInfo("CA" + "c" * 32, "CF" + "c" * 32)

    async def flaky_remove(_conference, sid):
        attempts.append(sid)
        if len(attempts) == 1:
            raise RuntimeError("twilio hiccup")
        succeeded.set()

    service.twilio.remove_participant = flaky_remove
    await service._abandon_participant(
        call_id, "callee", "callee_leg_setup_failed", late, conference_hint=late.conference_sid
    )
    await asyncio.wait_for(succeeded.wait(), timeout=2)

    assert len(attempts) == 2
    # Cleanup is scoped to the named leg; the transferred conference is untouched.
    assert service.twilio.completed == []


@pytest.mark.asyncio
async def test_failed_targeted_stream_cleanup_keeps_retrying(service, packet, monkeypatch):
    monkeypatch.setattr("app.call_state.RESOURCE_CLEANUP_RETRY_BASE_SECONDS", 0.01)
    monkeypatch.setattr("app.call_state.RESOURCE_CLEANUP_RETRY_MAX_SECONDS", 0.02)
    call_id = await seed_call(service.db, packet, state=CallState.TRANSFERRED)
    stream_sid = "MZ" + "s" * 32
    attempts: list[str] = []
    succeeded = asyncio.Event()

    async def flaky_stop(_callee, sid):
        attempts.append(sid)
        if len(attempts) == 1:
            raise RuntimeError("twilio hiccup")
        succeeded.set()

    service.twilio.stop_audio_monitor = flaky_stop
    await service._abandon_media_stream(
        call_id,
        "media_monitor_setup_failed",
        stream_sid,
        callee_call_sid="CA" + "b" * 32,
    )
    await asyncio.wait_for(succeeded.wait(), timeout=2)

    assert len(attempts) == 2
    assert service.twilio.completed == []
