from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.db import LatencyMark
from app.models import CallState
from tests.conftest import seed_call, wait_background


async def _make_call_stale(service, call_id: str) -> None:
    stale = datetime.now(UTC) - timedelta(minutes=1)
    await service.db.execute(
        "UPDATE calls SET last_event_at=? WHERE call_id=?", (stale.isoformat(), call_id)
    )


@pytest.mark.asyncio
async def test_realtime_deltas_flush_one_latest_arrival_without_per_event_writes(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    original_touch_calls = service.db.touch_calls
    batches: list[list[tuple[str, str]]] = []

    async def record_batch(activity) -> None:
        rows = list(activity)
        batches.append(rows)
        await original_touch_calls(rows)

    async def forbid_single_touch(*args, **kwargs) -> None:
        raise AssertionError("Realtime events must not write one heartbeat per frame")

    monkeypatch.setattr(service.db, "touch_calls", record_batch)
    monkeypatch.setattr(service.db, "touch_call", forbid_single_touch)
    wall_base = datetime.now(UTC) + timedelta(seconds=1)
    monotonic_base = LatencyMark.now().monotonic_ns

    latest: LatencyMark | None = None
    for sequence in range(125):
        latest = LatencyMark(
            occurred_at=(wall_base + timedelta(microseconds=sequence)).isoformat(),
            monotonic_ns=monotonic_base + sequence,
        )
        # Production invokes this on the reader before dispatching the delta.
        service._note_call_activity(call_id, latest)
        await service.handle_realtime_event(
            call_id,
            {"type": "response.audio.delta", "event_id": f"evt_{sequence}"},
        )

    expected = service._latest_call_activity[call_id]
    await service._flush_call_activity()

    assert latest is not None
    assert batches == [[(call_id, expected.occurred_at)]]
    assert (await service.db.get_call(call_id))["last_event_at"] == expected.occurred_at


@pytest.mark.asyncio
async def test_batched_arrival_never_overwrites_newer_durable_activity(service, packet):
    call_id = await seed_call(service.db, packet)
    newer = datetime.now(UTC) + timedelta(seconds=2)
    older = newer - timedelta(seconds=1)
    await service.db.execute(
        "UPDATE calls SET last_event_at=? WHERE call_id=?", (newer.isoformat(), call_id)
    )

    await service.db.touch_calls([(call_id, older.isoformat())])

    assert (await service.db.get_call(call_id))["last_event_at"] == newer.isoformat()


@pytest.mark.asyncio
async def test_failed_flush_keeps_fresh_memory_and_prevents_false_stale_timeout(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet)
    await _make_call_stale(service, call_id)
    service._note_call_activity(call_id)
    original_touch_calls = service.db.touch_calls
    attempts = 0

    async def fail_once(activity) -> None:
        nonlocal attempts
        rows = list(activity)
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated heartbeat write failure")
        await original_touch_calls(rows)

    monkeypatch.setattr(service.db, "touch_calls", fail_once)

    await service._watchdog_once()

    assert (await service.db.get_call(call_id))["state"] == CallState.PREWARMING.value
    assert call_id in service._latest_call_activity
    assert call_id in service._dirty_call_activity
    assert call_id not in service._watchdog_claims

    # A later successful tick retries the retained latest observation.
    await service._flush_call_activity()
    assert attempts == 2


@pytest.mark.asyncio
async def test_failed_flush_never_requeues_over_newer_inflight_activity(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet)
    base = LatencyMark.now()
    earlier = LatencyMark(base.occurred_at, base.monotonic_ns)
    later = LatencyMark(
        (datetime.fromisoformat(base.occurred_at) + timedelta(seconds=1)).isoformat(),
        base.monotonic_ns + 1,
    )
    service._note_call_activity(call_id, earlier)
    original_touch_calls = service.db.touch_calls
    attempts = 0

    async def fail_after_new_activity(activity) -> None:
        nonlocal attempts
        rows = list(activity)
        attempts += 1
        if attempts == 1:
            service._note_call_activity(call_id, later)
            raise RuntimeError("simulated concurrent flush failure")
        await original_touch_calls(rows)

    monkeypatch.setattr(service.db, "touch_calls", fail_after_new_activity)

    await service._flush_call_activity()

    assert service._latest_call_activity[call_id] == later
    assert service._dirty_call_activity[call_id] == later
    await service._flush_call_activity()
    assert attempts == 2


@pytest.mark.asyncio
async def test_activity_arriving_during_stale_reread_wins_before_watchdog_claim(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet)
    await _make_call_stale(service, call_id)
    original_get_call = service.db.get_call
    injected = False

    async def get_call_with_racing_activity(requested_call_id: str):
        nonlocal injected
        call = await original_get_call(requested_call_id)
        if requested_call_id == call_id and not injected:
            injected = True
            service._note_call_activity(call_id)
        return call

    monkeypatch.setattr(service.db, "get_call", get_call_with_racing_activity)

    await service._watchdog_once()

    assert injected is True
    assert (await original_get_call(call_id))["state"] == CallState.PREWARMING.value
    assert call_id not in service._watchdog_claims


@pytest.mark.asyncio
async def test_activity_after_watchdog_claim_does_not_reverse_real_timeout(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet)
    await _make_call_stale(service, call_id)
    original_terminate = service.terminate_call
    claim_observed = False

    async def terminate_after_late_activity(requested_call_id: str, reason: str, **kwargs):
        nonlocal claim_observed
        claim_observed = requested_call_id in service._watchdog_claims
        service._note_call_activity(requested_call_id)
        return await original_terminate(requested_call_id, reason, **kwargs)

    monkeypatch.setattr(service, "terminate_call", terminate_after_late_activity)

    await service._watchdog_once()
    await wait_background()

    assert claim_observed is True
    assert (await service.db.get_call(call_id))["state"] == CallState.TIMED_OUT.value
    assert call_id not in service._latest_call_activity
    assert call_id not in service._dirty_call_activity
    assert call_id not in service._watchdog_claims
    assert call_id in service._activity_tombstones


@pytest.mark.asyncio
async def test_terminal_call_clears_all_activity_tracking(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._note_call_activity(call_id)
    service._watchdog_claims.add(call_id)
    service._active_response_ids[call_id] = "resp_live"
    service._sip_output_playing.add(call_id)
    service._audio_drain_terminations[call_id] = ("resp_live", "voice_model_end_call")
    service._inflight_tools.add(call_id)

    assert await service.terminate_call(call_id, "owner_request") is True
    await wait_background()

    assert call_id not in service._latest_call_activity
    assert call_id not in service._dirty_call_activity
    assert call_id not in service._watchdog_claims
    assert call_id in service._activity_tombstones
    assert call_id not in service._active_response_ids
    assert call_id not in service._sip_output_playing
    assert call_id not in service._audio_drain_terminations
    assert call_id not in service._inflight_tools


@pytest.mark.asyncio
async def test_live_assistant_response_prevents_false_stale_timeout(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await _make_call_stale(service, call_id)
    service._active_response_ids[call_id] = "resp_live"

    await service._watchdog_once()

    assert (await service.db.get_call(call_id))["state"] == CallState.ACTIVE.value
    assert call_id in service._latest_call_activity
    assert call_id not in service._watchdog_claims


@pytest.mark.asyncio
async def test_sip_output_events_track_live_playback(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)

    await service.handle_realtime_event(call_id, {"type": "output_audio_buffer.started"})
    assert call_id in service._sip_output_playing

    await service.handle_realtime_event(call_id, {"type": "output_audio_buffer.stopped"})
    assert call_id not in service._sip_output_playing


@pytest.mark.asyncio
async def test_late_twilio_callbacks_do_not_reinsert_terminal_activity(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.COMPLETED)
    before = await service.db.get_call(call_id)
    service._note_call_activity(call_id)

    await service.handle_amd(call_id, "human")
    await service.handle_conference_event(call_id, {"StatusCallbackEvent": "conference-start"})
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "ringing"})

    after = await service.db.get_call(call_id)
    assert after["last_event_at"] == before["last_event_at"]
    assert after["amd_result"] is None
    assert call_id not in service._latest_call_activity
    assert call_id not in service._dirty_call_activity
    assert call_id not in service._watchdog_claims
    assert call_id in service._activity_tombstones


@pytest.mark.asyncio
async def test_callback_stale_read_cannot_reinsert_after_termination_claim(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    original_get_call = service.db.get_call
    inject_termination = True

    async def get_call_with_stale_snapshot(requested_call_id: str):
        nonlocal inject_termination
        snapshot = await original_get_call(requested_call_id)
        if requested_call_id == call_id and inject_termination:
            inject_termination = False
            assert await service.terminate_call(call_id, "owner_request") is True
        return snapshot

    monkeypatch.setattr(service.db, "get_call", get_call_with_stale_snapshot)

    await service.handle_amd(call_id, "human")
    await wait_background()

    call = await original_get_call(call_id)
    assert call["state"] == CallState.COMPLETED.value
    assert call["amd_result"] is None
    assert call_id in service._activity_tombstones
    assert call_id not in service._latest_call_activity
    assert call_id not in service._dirty_call_activity


@pytest.mark.asyncio
async def test_twilio_arrival_blocked_on_read_prevents_watchdog_timeout(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet)
    await _make_call_stale(service, call_id)
    original_get_call = service.db.get_call
    read_started = asyncio.Event()
    release_read = asyncio.Event()

    async def block_callback_read(requested_call_id: str):
        if asyncio.current_task().get_name() == "blocked-twilio-callback":
            read_started.set()
            await release_read.wait()
        return await original_get_call(requested_call_id)

    monkeypatch.setattr(service.db, "get_call", block_callback_read)
    callback = asyncio.create_task(
        service.handle_amd(call_id, "human"),
        name="blocked-twilio-callback",
    )
    await asyncio.wait_for(read_started.wait(), timeout=1)

    try:
        await service._watchdog_once()
        assert (await original_get_call(call_id))["state"] == CallState.PREWARMING.value
        assert call_id not in service._watchdog_claims
    finally:
        release_read.set()
    await callback


@pytest.mark.asyncio
async def test_activity_tombstones_are_bounded_expire_and_safely_reform(
    service, packet, monkeypatch
):
    monkeypatch.setattr("app.call_activity.CALL_ACTIVITY_TOMBSTONE_MAX", 2)
    monkeypatch.setattr("app.call_activity.CALL_ACTIVITY_TOMBSTONE_TTL_SECONDS", 1)
    clock = [100]
    monkeypatch.setattr("app.call_activity.monotonic_ns", lambda: clock[0])
    call_ids = [f"call_tombstone_{index}" for index in range(3)]

    for index, call_id in enumerate(call_ids):
        await seed_call(
            service.db,
            packet,
            call_id=call_id,
            state=CallState.COMPLETED,
            openai_call_id=f"rtc_tombstone_{index}",
        )
        service._tombstone_call_activity(call_id)
        clock[0] += 1

    assert list(service._activity_tombstones) == call_ids[-2:]
    assert len(service._activity_tombstones) == 2

    # The oldest tombstone was evicted, but a late callback still consults the
    # durable terminal row, reforms the tombstone, and clears transient activity.
    evicted = call_ids[0]
    await service.handle_amd(evicted, "human")
    assert evicted in service._activity_tombstones
    assert evicted not in service._latest_call_activity
    assert len(service._activity_tombstones) == 2

    clock[0] += 1_000_000_001
    assert service._note_call_activity("call_after_expiry") is True
    assert service._activity_tombstones == {}
    service._clear_call_activity("call_after_expiry")


@pytest.mark.asyncio
async def test_twilio_callbacks_for_missing_call_leave_no_activity(service):
    call_id = "call_missing"
    service._note_call_activity(call_id)

    await service.handle_amd(call_id, "human")
    await service.handle_conference_event(call_id, {"StatusCallbackEvent": "conference-start"})
    await service.handle_participant_status(call_id, "callee", {"CallStatus": "ringing"})

    assert call_id not in service._latest_call_activity
    assert call_id not in service._dirty_call_activity
    assert call_id not in service._watchdog_claims
    assert call_id not in service._activity_tombstones
