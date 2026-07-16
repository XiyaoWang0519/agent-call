from __future__ import annotations

import asyncio

import pytest

import app.call_state as call_state_module
import app.owner_transfer as owner_transfer_module
from app.exa_search import ExaSearchError
from app.models import CallState
from app.twilio_bridge import ParticipantInfo
from tests.conftest import seed_call, wait_background


def _tool_event(
    tool_call_id: str,
    name: str,
    arguments: str,
) -> dict[str, str]:
    return {
        "type": "response.function_call_arguments.done",
        "event_id": f"evt_{tool_call_id}",
        "call_id": tool_call_id,
        "name": name,
        "arguments": arguments,
    }


@pytest.mark.asyncio
async def test_search_web_returns_compact_evidence_and_records_latency(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)

    await service.handle_realtime_event(
        call_id,
        _tool_event(
            "tool_search",
            "search_web",
            '{"query":"  latest   Example Clinic hours  "}',
        ),
    )
    await wait_background()

    assert service._test_exa.queries == ["latest Example Clinic hours"]
    assert service._test_realtime.tool_results[-1] == (
        call_id,
        "tool_search",
        service._test_exa.result.output,
    )
    call = await service.db.get_call(call_id)
    assert call["tool_call_count"] == 1
    latency = await service.db.get_latency_events(call_id)
    assert {event["stage"] for event in latency} >= {
        "tool_call_received",
        "exa_search_started",
        "exa_search_completed",
    }


@pytest.mark.asyncio
async def test_search_web_failure_continues_call_with_safe_error(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._test_exa.error = ExaSearchError("search_rate_limited")

    await service.handle_realtime_event(
        call_id,
        _tool_event("tool_search", "search_web", '{"query":"current opening hours"}'),
    )

    assert service._test_realtime.tool_results[-1][2] == {
        "ok": False,
        "error": "search_rate_limited",
    }
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.ACTIVE.value
    assert call["tool_call_count"] == 1


@pytest.mark.asyncio
async def test_search_web_rejects_model_control_of_provider_parameters(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)

    await service.handle_realtime_event(
        call_id,
        _tool_event(
            "tool_search",
            "search_web",
            '{"query":"current opening hours","type":"deep","numResults":100}',
        ),
    )

    assert service._test_exa.queries == []
    assert service._test_realtime.tool_results[-1][2] == {
        "ok": False,
        "error": "invalid_search_request",
    }


@pytest.mark.asyncio
async def test_invalid_tool_result_is_sent_before_single_fused_write_finishes(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    original = service.db.record_tool_call
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    writes = 0

    async def blocked_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        write_started.set()
        await release_write.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(service.db, "record_tool_call", blocked_write)
    handling = asyncio.create_task(
        service.handle_realtime_event(
            call_id,
            _tool_event(
                "tool_fast",
                "future_tool",
                "{}",
            ),
        )
    )

    await asyncio.wait_for(write_started.wait(), timeout=2)
    await asyncio.sleep(0)
    assert service._test_realtime.tool_results[-1][1] == "tool_fast"
    assert not handling.done()

    release_write.set()
    await asyncio.wait_for(handling, timeout=2)
    assert writes == 1
    call = await service.db.get_call(call_id)
    assert call["tool_call_count"] == 1
    assert call.get("advisory_outcome") is None


@pytest.mark.asyncio
async def test_valid_advisory_is_durable_before_accepted_output(service, packet, monkeypatch):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    original = service.db.record_tool_call
    write_started = asyncio.Event()
    release_write = asyncio.Event()

    async def blocked_write(*args, **kwargs):
        write_started.set()
        await release_write.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(service.db, "record_tool_call", blocked_write)
    handling = asyncio.create_task(
        service.handle_realtime_event(
            call_id,
            _tool_event(
                "tool_durable",
                "record_call_outcome",
                '{"status":"completed","summary":"Done","commitments":[],"followUps":[]}',
            ),
        )
    )

    await asyncio.wait_for(write_started.wait(), timeout=2)
    await asyncio.sleep(0)
    assert service._test_realtime.tool_results == []

    release_write.set()
    await asyncio.wait_for(handling, timeout=2)
    call = await service.db.get_call(call_id)
    assert call["advisory_outcome"]["summary"] == "Done"
    assert service._test_realtime.tool_results[-1][2] == {"accepted": True}


@pytest.mark.asyncio
async def test_invalid_and_unknown_tools_count_without_overwriting_valid_advisory(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    events = [
        _tool_event(
            "tool_valid",
            "record_call_outcome",
            '{"status":"completed","summary":"Keep me","commitments":[],"followUps":[]}',
        ),
        _tool_event("tool_invalid", "record_call_outcome", "{}"),
        _tool_event("tool_unknown", "future_tool", "{}"),
    ]
    for event in events:
        await service.handle_realtime_event(call_id, event)

    call = await service.db.get_call(call_id)
    assert call["tool_call_count"] == 3
    assert call["advisory_outcome"]["summary"] == "Keep me"
    latency_keys = {
        event["event_key"]
        for event in await service.db.get_latency_events(call_id)
        if event["stage"] == "tool_call_received"
    }
    assert latency_keys == {"tool_valid", "tool_invalid", "tool_unknown"}


@pytest.mark.asyncio
async def test_nontransfer_tool_result_send_race_does_not_propagate(service, packet, monkeypatch):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)

    async def teardown_race(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("sideband is not open")

    monkeypatch.setattr(service.realtime, "send_tool_result", teardown_race)

    # Must not raise: a benign teardown race sending the tool result cannot escalate
    # into a fatal error, but the durable write still has to land.
    await service.handle_realtime_event(
        call_id,
        _tool_event("tool_race", "future_tool", "{}"),
    )

    call = await service.db.get_call(call_id)
    assert call["tool_call_count"] == 1


@pytest.mark.asyncio
async def test_advisory_persistence_failure_sends_rejection_and_propagates(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)

    async def failing_write(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(service.db, "record_tool_call", failing_write)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.handle_realtime_event(
            call_id,
            _tool_event(
                "tool_advisory_fail",
                "record_call_outcome",
                '{"status":"completed","summary":"Done","commitments":[],"followUps":[]}',
            ),
        )

    # The model must still receive a tool result even though its outcome was lost.
    assert service._test_realtime.tool_results[-1] == (
        call_id,
        "tool_advisory_fail",
        {"accepted": False, "error": "outcome could not be persisted"},
    )


@pytest.mark.asyncio
async def test_response_created_marks_continuation_without_reading_call(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service.handle_realtime_event(
        call_id,
        _tool_event("tool_1", "future_tool", "{}"),
    )

    async def forbidden_read(_call_id: str):
        raise AssertionError("response.created must use one conditional UPDATE")

    monkeypatch.setattr(service.db, "get_call", forbidden_read)
    await service.handle_realtime_event(
        call_id,
        {"type": "response.created", "response": {"id": "resp_after_tool"}},
    )
    row = await service.db.fetch_one(
        "SELECT tool_continuation_observed FROM calls WHERE call_id=?", (call_id,)
    )
    assert row["tool_continuation_observed"] == 1


@pytest.mark.asyncio
async def test_ordinary_response_created_performs_no_tool_continuation_write(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    writes = 0

    async def forbidden_write(_call_id: str):
        nonlocal writes
        writes += 1
        raise AssertionError("ordinary responses must not write tool continuation state")

    monkeypatch.setattr(service.db, "mark_tool_continuation_observed", forbidden_write)
    await service.handle_realtime_event(
        call_id,
        {"type": "response.created", "response": {"id": "resp_ordinary"}},
    )

    assert writes == 0
    assert service._active_response_ids[call_id] == "resp_ordinary"


@pytest.mark.asyncio
async def test_valid_end_call_arms_fallback_before_persistence_failure(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    fallback_started = asyncio.Event()
    release_fallback = asyncio.Event()

    async def blocked_fallback(_call_id: str, _tool_call_id: str):
        fallback_started.set()
        await release_fallback.wait()

    async def failed_write(*args, **kwargs):
        del args, kwargs
        await fallback_started.wait()
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(service, "_voice_end_fallback", blocked_fallback)
    monkeypatch.setattr(service.db, "record_tool_call", failed_write)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.handle_realtime_event(
            call_id,
            _tool_event("tool_end", "end_call", '{"reason":"objective_completed"}'),
        )

    assert service._voice_end_pending[call_id][0] == "tool_end"
    assert fallback_started.is_set()
    release_fallback.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_transfer_runs_in_background_and_duplicate_creates_one_owner(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    create_started = asyncio.Event()
    release_create = asyncio.Event()

    async def blocked_create(**kwargs):
        del kwargs
        service._test_twilio.owner_creates += 1
        create_started.set()
        await release_create.wait()
        return ParticipantInfo("CA" + "c" * 32, "CF" + "a" * 32)

    monkeypatch.setattr(service._test_twilio, "create_owner_participant", blocked_create)
    await service._handle_tool_call(
        call_id,
        _tool_event("tool_transfer_1", "transfer_to_owner", '{"reason":"owner needed"}'),
    )
    await asyncio.wait_for(create_started.wait(), timeout=2)
    transfer_task = service._owner_transfer_tasks[call_id]
    assert not transfer_task.done()

    await service._handle_tool_call(
        call_id,
        _tool_event("tool_transfer_2", "transfer_to_owner", '{"reason":"again"}'),
    )
    assert service._test_twilio.owner_creates == 1
    assert service._test_realtime.tool_results[-1][2]["accepted"] is False

    service._owner_join_events[call_id].set()
    release_create.set()
    assert (await asyncio.wait_for(asyncio.shield(transfer_task), timeout=2))["accepted"] is True
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.TRANSFERRED.value
    assert call["tool_call_count"] == 2


@pytest.mark.asyncio
async def test_cancel_during_joining_claim_still_registers_transfer_worker(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    original_claim = service.db.claim_transfer_joining
    claim_committed = asyncio.Event()
    release_claim = asyncio.Event()

    async def committed_then_blocked(*args, **kwargs):
        claimed = await original_claim(*args, **kwargs)
        claim_committed.set()
        await release_claim.wait()
        return claimed

    monkeypatch.setattr(service.db, "claim_transfer_joining", committed_then_blocked)
    starting = asyncio.create_task(
        service._start_owner_transfer(call_id, "owner needed", tool_call_id=None)
    )
    await asyncio.wait_for(claim_committed.wait(), timeout=2)
    starting.cancel()
    await asyncio.sleep(0)
    assert not starting.done()

    release_claim.set()
    transfer_task, error = await asyncio.wait_for(starting, timeout=2)
    assert error is None
    assert transfer_task is service._owner_transfer_tasks[call_id]
    service._owner_join_events[call_id].set()
    assert (await asyncio.wait_for(transfer_task, timeout=2))["accepted"] is True


@pytest.mark.asyncio
async def test_cancel_after_termination_claim_still_finishes_media_and_terminal_state(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    hangup_started = asyncio.Event()
    release_hangup = asyncio.Event()

    async def blocked_hangup(openai_call_id):
        service._test_realtime.hangups.append(openai_call_id)
        hangup_started.set()
        await release_hangup.wait()

    monkeypatch.setattr(service._test_realtime, "hangup", blocked_hangup)
    terminating = asyncio.create_task(service.terminate_call(call_id, "owner_request"))
    await asyncio.wait_for(hangup_started.wait(), timeout=2)
    claimed = await service.db.get_call(call_id)
    assert claimed["state"] == CallState.TERMINATING.value
    assert claimed["termination_claimed"] == 1

    terminating.cancel()
    await asyncio.sleep(0)
    assert not terminating.done()
    release_hangup.set()
    assert await asyncio.wait_for(terminating, timeout=2) is True

    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.COMPLETED.value
    assert service._test_twilio.completed == ["CF" + "a" * 32]
    assert service._test_realtime.closed == [call_id]


@pytest.mark.asyncio
async def test_termination_claim_commit_then_raise_is_reconciled(service, packet, monkeypatch):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    original_claim = service.db.claim_termination

    async def commit_then_raise(*args, **kwargs):
        assert await original_claim(*args, **kwargs) is not None
        raise RuntimeError("connection lost after commit")

    monkeypatch.setattr(service.db, "claim_termination", commit_then_raise)

    assert await service.terminate_call(call_id, "owner_request") is True
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.COMPLETED.value
    assert call["termination_reason"] == "owner_request"
    assert service._test_twilio.completed == ["CF" + "a" * 32]


@pytest.mark.asyncio
async def test_joining_claim_commit_then_raise_spawns_one_transfer_worker(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._owner_join_events.setdefault(call_id, asyncio.Event()).set()
    original_claim = service.db.claim_transfer_joining

    async def commit_then_raise(*args, **kwargs):
        assert await original_claim(*args, **kwargs) is True
        raise RuntimeError("connection lost after commit")

    monkeypatch.setattr(service.db, "claim_transfer_joining", commit_then_raise)
    result = await service.transfer_to_owner(call_id, "owner needed")

    assert result["accepted"] is True
    assert service._test_twilio.owner_creates == 1
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.TRANSFERRED.value
    assert call["twilio_owner_call_sid"] == "CA" + "c" * 32


@pytest.mark.asyncio
async def test_termination_waits_for_late_owner_create_and_cleans_sid_once(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    create_started = asyncio.Event()
    release_create = asyncio.Event()

    async def late_create(**kwargs):
        del kwargs
        service._test_twilio.owner_creates += 1
        create_started.set()
        await release_create.wait()
        return ParticipantInfo("CA" + "c" * 32, "CF" + "a" * 32)

    monkeypatch.setattr(service._test_twilio, "create_owner_participant", late_create)
    await service._handle_tool_call(
        call_id,
        _tool_event("tool_transfer", "transfer_to_owner", '{"reason":"owner needed"}'),
    )
    await asyncio.wait_for(create_started.wait(), timeout=2)

    terminating = asyncio.create_task(service.terminate_call(call_id, "owner_request"))
    await asyncio.sleep(0)
    assert not terminating.done()
    release_create.set()
    assert await asyncio.wait_for(terminating, timeout=2) is True

    owner_sid = "CA" + "c" * 32
    conference = "CF" + "a" * 32
    assert service._test_twilio.removed.count((conference, owner_sid)) == 1
    assert service._test_twilio.completed == [conference]
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.COMPLETED.value
    assert call["transfer_outcome"] == "failed:termination_won"
    assert call_id not in service._owner_transfer_tasks


@pytest.mark.asyncio
async def test_owner_join_timeout_cleanup_failure_completes_conference(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    attempts: list[str | None] = []

    async def failed_remove(conference, participant_call_sid):
        del conference
        attempts.append(participant_call_sid)
        raise RuntimeError("Twilio cleanup failed")

    monkeypatch.setattr(owner_transfer_module, "OWNER_JOIN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(service._test_twilio, "remove_participant", failed_remove)
    result = await service.transfer_to_owner(call_id, "owner needed")

    assert result["accepted"] is False
    assert attempts == ["CA" + "c" * 32]
    assert service._test_twilio.completed == ["CF" + "a" * 32]
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "transfer_cleanup_failed"


@pytest.mark.asyncio
async def test_required_conference_completion_failure_stays_recoverable(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    attempts = 0
    original_complete = service._test_twilio.complete_conference

    async def failed_complete(_conference):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("Twilio unavailable")

    monkeypatch.setattr(call_state_module, "TERMINATION_MEDIA_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(service._test_twilio, "complete_conference", failed_complete)
    assert await service.terminate_call(call_id, "owner_request") is False

    stranded = await service.db.get_call(call_id)
    assert attempts == 2
    assert stranded["state"] == CallState.TERMINATING.value
    assert stranded["termination_claimed"] == 1
    assert service._test_finalizer.states_seen == []

    monkeypatch.setattr(service._test_twilio, "complete_conference", original_complete)
    await service.recover_startup()
    recovered = await service.db.get_call(call_id)
    assert recovered["state"] == CallState.FAILED.value
    assert recovered["termination_reason"] == "startup_recovery"
    assert service._test_twilio.completed == ["CF" + "a" * 32]


@pytest.mark.asyncio
async def test_required_conference_completion_succeeds_via_live_background_retry(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    attempts = 0
    recovered = asyncio.Event()
    original_complete = service._test_twilio.complete_conference

    async def transient_complete(conference):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise RuntimeError("temporary Twilio outage")
        await original_complete(conference)
        recovered.set()

    monkeypatch.setattr(call_state_module, "TERMINATION_MEDIA_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(call_state_module, "TERMINATION_MEDIA_BACKGROUND_RETRY_BASE_SECONDS", 0)
    monkeypatch.setattr(service._test_twilio, "complete_conference", transient_complete)

    assert await service.terminate_call(call_id, "owner_request") is False
    await asyncio.wait_for(recovered.wait(), timeout=2)
    for _ in range(50):
        if (await service.db.get_call(call_id))["state"] == CallState.COMPLETED.value:
            break
        await asyncio.sleep(0.01)

    assert attempts == 3
    assert (await service.db.get_call(call_id))["state"] == CallState.COMPLETED.value
    assert service._test_twilio.completed == ["CF" + "a" * 32]


@pytest.mark.asyncio
async def test_shutdown_cancels_failed_background_retry_after_current_attempt(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    attempts = 0
    background_attempt_started = asyncio.Event()
    release_background_attempt = asyncio.Event()

    async def fail_with_blocked_background(_conference):
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            background_attempt_started.set()
            await release_background_attempt.wait()
        raise RuntimeError("Twilio unavailable")

    monkeypatch.setattr(call_state_module, "TERMINATION_MEDIA_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(call_state_module, "TERMINATION_MEDIA_BACKGROUND_RETRY_BASE_SECONDS", 0)
    monkeypatch.setattr(service._test_twilio, "complete_conference", fail_with_blocked_background)

    assert await service.terminate_call(call_id, "owner_request") is False
    await asyncio.wait_for(background_attempt_started.wait(), timeout=2)
    stopping = asyncio.create_task(service.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release_background_attempt.set()
    await asyncio.wait_for(stopping, timeout=2)

    # Shutdown may make one final idempotent teardown attempt, but cancellation
    # must stop the unbounded retry loop promptly.
    assert 3 <= attempts < 10
    assert service._conference_retry_tasks == {}
    assert (await service.db.get_call(call_id))["state"] == CallState.TERMINATING.value


@pytest.mark.asyncio
async def test_startup_recovery_continues_other_rows_during_sustained_twilio_failure(
    service, packet, monkeypatch
):
    failing_call = await seed_call(
        service.db,
        packet,
        call_id="call_recovery_failing",
        state=CallState.ACTIVE,
        openai_call_id="rtc_recovery_failing",
    )
    healthy_call = await seed_call(
        service.db,
        packet,
        call_id="call_recovery_healthy",
        state=CallState.ACTIVE,
        openai_call_id="rtc_recovery_healthy",
    )
    failing_conference = "CF" + "f" * 32
    healthy_conference = "CF" + "h" * 32
    await service.db.update_call(failing_call, conference_sid=failing_conference)
    await service.db.update_call(healthy_call, conference_sid=healthy_conference)
    original_complete = service._test_twilio.complete_conference

    async def selectively_fail(conference):
        if conference == failing_conference:
            raise RuntimeError("sustained Twilio outage")
        await original_complete(conference)

    service.settings.max_call_seconds = 0.05
    monkeypatch.setattr(call_state_module, "TERMINATION_MEDIA_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(call_state_module, "TERMINATION_MEDIA_BACKGROUND_RETRY_BASE_SECONDS", 0.01)
    monkeypatch.setattr(call_state_module, "TERMINATION_MEDIA_BACKGROUND_RETRY_MAX_SECONDS", 0.01)
    monkeypatch.setattr(service._test_twilio, "complete_conference", selectively_fail)

    await asyncio.wait_for(service.recover_startup(), timeout=2)

    assert (await service.db.get_call(failing_call))["state"] == CallState.TERMINATING.value
    assert (await service.db.get_call(healthy_call))["state"] == CallState.FAILED.value
    assert healthy_conference in service._test_twilio.completed


@pytest.mark.asyncio
async def test_db_outage_after_owner_create_cleans_owner_before_conference(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._owner_join_events.setdefault(call_id, asyncio.Event()).set()
    order: list[str] = []
    original_remove = service._test_twilio.remove_participant
    original_complete = service._test_twilio.complete_conference

    async def tracked_remove(conference, participant_call_sid):
        order.append("remove_owner")
        await original_remove(conference, participant_call_sid)

    async def tracked_complete(conference):
        order.append("complete_conference")
        await original_complete(conference)

    async def db_unavailable(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(service._test_twilio, "remove_participant", tracked_remove)
    monkeypatch.setattr(service._test_twilio, "complete_conference", tracked_complete)
    monkeypatch.setattr(service.db, "promote_transfer", db_unavailable)
    monkeypatch.setattr(service.db, "fail_joining_transfer", db_unavailable)

    result = await service.transfer_to_owner(call_id, "owner needed")

    assert result["accepted"] is False
    assert order == ["remove_owner", "complete_conference"]
    assert service._test_twilio.removed == [("CF" + "a" * 32, "CA" + "c" * 32)]
    assert service._test_twilio.completed == ["CF" + "a" * 32]


@pytest.mark.asyncio
async def test_failed_direct_compensation_is_owned_by_background_retry(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._owner_join_events.setdefault(call_id, asyncio.Event()).set()
    attempts = 0
    compensated = asyncio.Event()
    original_complete = service._test_twilio.complete_conference

    async def db_unavailable(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("database unavailable")

    async def transient_complete(conference):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Twilio temporarily unavailable")
        await original_complete(conference)
        compensated.set()

    monkeypatch.setattr(call_state_module, "TERMINATION_MEDIA_BACKGROUND_RETRY_BASE_SECONDS", 0)
    monkeypatch.setattr(service.db, "promote_transfer", db_unavailable)
    monkeypatch.setattr(service.db, "fail_joining_transfer", db_unavailable)
    monkeypatch.setattr(service.db, "fail_promoted_transfer", db_unavailable)
    monkeypatch.setattr(service._test_twilio, "complete_conference", transient_complete)

    result = await service.transfer_to_owner(call_id, "owner needed")
    await asyncio.wait_for(compensated.wait(), timeout=2)

    assert result["accepted"] is False
    assert attempts == 2
    assert service._test_twilio.completed == ["CF" + "a" * 32]


@pytest.mark.asyncio
async def test_promotion_commit_then_exception_is_adopted_and_torn_down(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._owner_join_events.setdefault(call_id, asyncio.Event()).set()
    original_promote = service.db.promote_transfer

    async def commit_then_raise(*args, **kwargs):
        assert await original_promote(*args, **kwargs) is not None
        raise RuntimeError("connection lost after commit")

    monkeypatch.setattr(service.db, "promote_transfer", commit_then_raise)
    result = await service.transfer_to_owner(call_id, "owner needed")

    assert result["accepted"] is False
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.FAILED.value
    assert call["transfer_outcome"] == "failed:RuntimeError"
    assert service._test_twilio.removed == [("CF" + "a" * 32, "CA" + "c" * 32)]
    assert service._test_twilio.completed == ["CF" + "a" * 32]


@pytest.mark.asyncio
async def test_completion_commit_then_exception_cannot_recover_as_transferred(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._owner_join_events.setdefault(call_id, asyncio.Event()).set()
    original_complete = service.db.complete_promoted_transfer

    async def commit_then_raise(*args, **kwargs):
        assert await original_complete(*args, **kwargs) is True
        raise RuntimeError("connection lost after commit")

    monkeypatch.setattr(service.db, "complete_promoted_transfer", commit_then_raise)
    result = await service.transfer_to_owner(call_id, "owner needed")

    assert result["accepted"] is False
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.FAILED.value
    assert call["transfer_outcome"] == "failed:RuntimeError"
    assert call["termination_reason"] == "transfer_failed:RuntimeError"
    assert service._test_twilio.completed == ["CF" + "a" * 32]


@pytest.mark.asyncio
async def test_terminal_cas_failure_never_returns_success_and_ends_conference(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._owner_join_events.setdefault(call_id, asyncio.Event()).set()

    async def lost_terminal_cas(*args, **kwargs):
        del args, kwargs
        return False

    monkeypatch.setattr(service.db, "finish_claimed_termination", lost_terminal_cas)
    result = await service.transfer_to_owner(call_id, "owner needed")

    assert result["accepted"] is False
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.TERMINATING.value
    assert call["transfer_outcome"] == "failed:terminal_cas"
    assert call["termination_reason"] == "transfer_failed:terminal_cas"
    assert service._test_twilio.removed == [
        ("CF" + "a" * 32, "CA" + "a" * 32),
        ("CF" + "a" * 32, "CA" + "c" * 32),
    ]
    assert service._test_twilio.completed
    assert service._test_finalizer.states_seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous_result", ["raise", "false"])
async def test_transferred_terminal_commit_ambiguity_preserves_handoff(
    service, packet, monkeypatch, ambiguous_result
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._owner_join_events.setdefault(call_id, asyncio.Event()).set()
    original_finish = service.db.finish_claimed_termination

    async def commit_then_ambiguous(*args, **kwargs):
        assert await original_finish(*args, **kwargs) is True
        if ambiguous_result == "raise":
            raise RuntimeError("connection lost after commit")
        return False

    monkeypatch.setattr(service.db, "finish_claimed_termination", commit_then_ambiguous)
    result = await service.transfer_to_owner(call_id, "owner needed")

    assert result["accepted"] is True
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.TRANSFERRED.value
    assert call["termination_reason"] == "transfer_completed"
    assert call["transfer_outcome"] == "completed:owner needed"
    assert service._test_twilio.completed == []
    assert service._test_twilio.end_on_exit == [("CF" + "a" * 32, "CA" + "c" * 32)]


@pytest.mark.asyncio
async def test_terminal_transcript_failure_cannot_undo_transferred_call(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._owner_join_events.setdefault(call_id, asyncio.Event()).set()

    async def failed_transcript(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("transcript database unavailable")

    monkeypatch.setattr(service.db, "add_transcript_turn", failed_transcript)
    result = await service.transfer_to_owner(call_id, "owner needed")

    assert result["accepted"] is True
    assert (await service.db.get_call(call_id))["state"] == CallState.TRANSFERRED.value
    assert service._test_twilio.completed == []
    assert service._test_twilio.end_on_exit == [("CF" + "a" * 32, "CA" + "c" * 32)]


@pytest.mark.asyncio
async def test_shutdown_cannot_cancel_transfer_after_terminal_commit(service, packet, monkeypatch):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._owner_join_events.setdefault(call_id, asyncio.Event()).set()
    bookkeeping_started = asyncio.Event()
    release_bookkeeping = asyncio.Event()

    async def blocked_terminal_bookkeeping(*args, **kwargs):
        del args, kwargs
        bookkeeping_started.set()
        await release_bookkeeping.wait()

    monkeypatch.setattr(service.db, "add_transcript_turn", blocked_terminal_bookkeeping)
    transferring = asyncio.create_task(service.transfer_to_owner(call_id, "owner needed"))
    await asyncio.wait_for(bookkeeping_started.wait(), timeout=2)
    assert (await service.db.get_call(call_id))["state"] == CallState.TRANSFERRED.value

    stopping = asyncio.create_task(service.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release_bookkeeping.set()
    await asyncio.wait_for(stopping, timeout=2)

    assert (await asyncio.wait_for(transferring, timeout=2))["accepted"] is True
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.TRANSFERRED.value
    assert call["transfer_outcome"] == "completed:owner needed"
    assert service._test_twilio.completed == []
    assert ("CF" + "a" * 32, "CA" + "c" * 32) not in service._test_twilio.removed


@pytest.mark.asyncio
async def test_owner_leave_after_join_aborts_before_ai_handoff(service, packet, monkeypatch):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    original_promote = service.db.promote_transfer
    promotion_committed = asyncio.Event()
    release_promotion = asyncio.Event()

    async def committed_then_blocked(*args, **kwargs):
        promoted = await original_promote(*args, **kwargs)
        promotion_committed.set()
        await release_promotion.wait()
        return promoted

    monkeypatch.setattr(service.db, "promote_transfer", committed_then_blocked)
    transferring = asyncio.create_task(service.transfer_to_owner(call_id, "owner needed"))
    for _ in range(50):
        if call_id in service._owner_expected_sids:
            break
        await asyncio.sleep(0.01)
    owner_sid = "CA" + "c" * 32
    await service.handle_conference_event(
        call_id,
        {
            "StatusCallbackEvent": "participant-join",
            "ParticipantLabel": "owner",
            "CallSid": owner_sid,
        },
    )
    await asyncio.wait_for(promotion_committed.wait(), timeout=2)
    await service.handle_conference_event(
        call_id,
        {
            "StatusCallbackEvent": "participant-leave",
            "ParticipantLabel": "owner",
            "CallSid": owner_sid,
        },
    )
    release_promotion.set()

    result = await asyncio.wait_for(transferring, timeout=2)
    assert result["accepted"] is False
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.FAILED.value
    assert call["transfer_outcome"] == "failed:OwnerTransferDeparted"
    assert ("CF" + "a" * 32, "CA" + "a" * 32) not in service._test_twilio.removed
    assert service._test_twilio.completed == ["CF" + "a" * 32]


@pytest.mark.asyncio
async def test_ai_removal_failure_rolls_back_owner_and_fails_call(service, packet, monkeypatch):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._owner_join_events.setdefault(call_id, asyncio.Event()).set()
    original_remove = service._test_twilio.remove_participant
    ai_sid = "CA" + "a" * 32

    async def fail_ai_only(conference, participant_call_sid):
        if participant_call_sid == ai_sid:
            raise RuntimeError("AI removal failed")
        await original_remove(conference, participant_call_sid)

    monkeypatch.setattr(service._test_twilio, "remove_participant", fail_ai_only)
    result = await service.transfer_to_owner(call_id, "owner needed")

    assert result["accepted"] is False
    conference = "CF" + "a" * 32
    assert service._test_twilio.removed == [(conference, "CA" + "c" * 32)]
    assert service._test_twilio.completed == [conference]
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.FAILED.value
    assert call["transfer_outcome"] == "failed:RuntimeError"
    assert call["termination_reason"] == "transfer_failed:RuntimeError"

    retry = await service.transfer_to_owner(call_id, "retry")
    assert retry["accepted"] is False
    assert service._test_twilio.owner_creates == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transfer_outcome", "expected_state", "conference_completed"),
    [
        ("joining:owner needed", CallState.FAILED, True),
        ("in_progress:owner needed", CallState.FAILED, True),
        ("completed:owner needed", CallState.TRANSFERRED, False),
    ],
)
async def test_startup_recovery_is_conservative_until_transfer_completed(
    service,
    packet,
    transfer_outcome,
    expected_state,
    conference_completed,
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    promoted = transfer_outcome.startswith(("in_progress:", "completed:"))
    await service.db.update_call(
        call_id,
        state=CallState.TERMINATING.value if promoted else CallState.ACTIVE.value,
        transfer_outcome=transfer_outcome,
        twilio_owner_call_sid=("CA" + "c" * 32)
        if transfer_outcome.startswith("completed:")
        else None,
        termination_claimed=int(promoted),
        termination_reason="transfer_completed" if promoted else None,
    )

    await service.recover_startup()

    call = await service.db.get_call(call_id)
    assert call["state"] == expected_state.value
    assert bool(service._test_twilio.completed) is conference_completed
    if expected_state == CallState.FAILED:
        assert call["transfer_outcome"] == "failed:startup_recovery"
    else:
        assert call["transfer_outcome"] == transfer_outcome
        assert service._test_twilio.end_on_exit == [("CF" + "a" * 32, "CA" + "c" * 32)]


@pytest.mark.asyncio
async def test_completed_transfer_without_persisted_owner_sid_fails_closed_on_restart(
    service, packet
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service.db.update_call(
        call_id,
        state=CallState.TERMINATING.value,
        transfer_outcome="completed:owner needed",
        termination_claimed=1,
        termination_reason="transfer_completed",
        twilio_owner_call_sid=None,
    )

    await service.recover_startup()

    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.FAILED.value
    assert call["transfer_outcome"] == "failed:owner_exit_unarmed"
    assert call["termination_reason"] == "transfer_failed:owner_exit_unarmed"
    assert service._test_twilio.completed == ["CF" + "a" * 32]


@pytest.mark.asyncio
async def test_transferred_owner_is_armed_to_end_conference_after_process_state_clears(
    service, packet
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._owner_join_events.setdefault(call_id, asyncio.Event()).set()
    result = await service.transfer_to_owner(call_id, "owner needed")

    assert result["accepted"] is True
    assert service._test_twilio.end_on_exit == [("CF" + "a" * 32, "CA" + "c" * 32)]
    assert call_id not in service._owner_join_events
    await service.handle_participant_status(
        call_id,
        "owner",
        {"CallStatus": "completed", "CallSid": "CA" + "c" * 32},
    )
    assert (await service.db.get_call(call_id))["state"] == CallState.TRANSFERRED.value


@pytest.mark.asyncio
async def test_shutdown_cancellation_cannot_interrupt_transfer_compensation(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    failure_write_started = asyncio.Event()
    release_failure_write = asyncio.Event()
    original_fail = service.db.fail_joining_transfer

    async def failed_owner_remove(_conference, _participant_call_sid):
        raise RuntimeError("owner delete failed")

    async def blocked_failure_write(*args, **kwargs):
        failure_write_started.set()
        await release_failure_write.wait()
        return await original_fail(*args, **kwargs)

    monkeypatch.setattr(owner_transfer_module, "OWNER_JOIN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(service._test_twilio, "remove_participant", failed_owner_remove)
    monkeypatch.setattr(service.db, "fail_joining_transfer", blocked_failure_write)

    transferring = asyncio.create_task(service.transfer_to_owner(call_id, "owner needed"))
    await asyncio.wait_for(failure_write_started.wait(), timeout=2)
    stopping = asyncio.create_task(service.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release_failure_write.set()
    await asyncio.wait_for(stopping, timeout=2)

    assert (await asyncio.wait_for(transferring, timeout=2))["accepted"] is False
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "service_shutdown"
    assert service._test_twilio.completed
    assert set(service._test_twilio.completed) == {"CF" + "a" * 32}


@pytest.mark.asyncio
async def test_stop_drains_joining_claim_committed_after_shutdown_snapshot(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    claim_committed = asyncio.Event()
    release_claim = asyncio.Event()
    original_claim = service.db.claim_transfer_joining

    async def committed_then_blocked(*args, **kwargs):
        claimed = await original_claim(*args, **kwargs)
        claim_committed.set()
        await release_claim.wait()
        return claimed

    monkeypatch.setattr(service.db, "claim_transfer_joining", committed_then_blocked)
    starting = asyncio.create_task(
        service._start_owner_transfer(call_id, "owner needed", tool_call_id=None)
    )
    await asyncio.wait_for(claim_committed.wait(), timeout=2)
    stopping = asyncio.create_task(service.stop())
    await asyncio.sleep(0)
    release_claim.set()

    await asyncio.wait_for(stopping, timeout=2)
    transfer_task, error = await asyncio.wait_for(starting, timeout=2)
    assert transfer_task is None
    assert error == "service is stopping"
    assert service._test_twilio.owner_creates == 0
    call = await service.db.get_call(call_id)
    assert call["transfer_outcome"] == "failed:service_stopping"
    assert service._background == set()


@pytest.mark.asyncio
async def test_stop_fails_closed_for_an_unexpected_active_call(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)

    await asyncio.wait_for(service.stop(), timeout=2)

    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.FAILED.value
    assert call["termination_reason"] == "service_shutdown"
    assert service._test_realtime.hangups == ["rtc_test"]
    assert service._test_twilio.completed == ["CF" + "a" * 32]
    assert service._test_realtime.close_all_calls == 1


@pytest.mark.asyncio
async def test_shutdown_cleans_owner_created_after_transfer_task_cancellation(
    service, packet, monkeypatch
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    create_started = asyncio.Event()
    release_create = asyncio.Event()

    async def late_create(**kwargs):
        del kwargs
        create_started.set()
        await release_create.wait()
        return ParticipantInfo("CA" + "c" * 32, "CF" + "a" * 32)

    monkeypatch.setattr(service._test_twilio, "create_owner_participant", late_create)
    await service._handle_tool_call(
        call_id,
        _tool_event("tool_transfer", "transfer_to_owner", '{"reason":"owner needed"}'),
    )
    await asyncio.wait_for(create_started.wait(), timeout=2)

    stopping = asyncio.create_task(service.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release_create.set()
    await asyncio.wait_for(stopping, timeout=2)

    assert service._test_twilio.removed == [("CF" + "a" * 32, "CA" + "c" * 32)]
    assert call_id not in service._owner_transfer_tasks


@pytest.mark.asyncio
async def test_ordinary_termination_atomically_aborts_joining_before_promotion(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    assert await service.db.claim_transfer_joining(call_id, "owner needed")

    claimed = await service.db.claim_termination(call_id, "owner_request")

    assert claimed is not None
    assert claimed["state"] == CallState.TERMINATING.value
    assert claimed["termination_claimed"] == 1
    assert claimed["termination_reason"] == "owner_request"
    assert claimed["transfer_outcome"] == "failed:termination_won"
    assert await service.db.promote_transfer(call_id, "owner needed") is None


@pytest.mark.asyncio
async def test_transfer_claim_during_prewarming_with_callee_joined_completes(service, packet):
    # The opening turn starts as soon as the callee answers, while the durable state is
    # still prewarming (AMD/activation converge asynchronously). A transfer requested in
    # that window must still be able to claim and complete.
    call_id = await seed_call(service.db, packet, state=CallState.PREWARMING)
    await service.db.update_call(call_id, callee_joined=1)
    service._owner_join_events.setdefault(call_id, asyncio.Event()).set()

    result = await service.transfer_to_owner(call_id, "owner needed")

    assert result["accepted"] is True
    call = await service.db.get_call(call_id)
    assert call["state"] == CallState.TRANSFERRED.value
    assert call["transfer_outcome"] == "completed:owner needed"


@pytest.mark.asyncio
async def test_transfer_claim_before_callee_joined_is_rejected(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.PREWARMING)

    result = await service.transfer_to_owner(call_id, "owner needed")

    assert result["accepted"] is False
    assert result["error"] == "owner transfer already attempted or call is ending"
    call = await service.db.get_call(call_id)
    assert call["transfer_outcome"] is None
    assert service._test_twilio.owner_creates == 0


@pytest.mark.asyncio
async def test_spawned_task_failure_is_logged(service, caplog):
    async def boom() -> None:
        raise RuntimeError("background task exploded")

    with caplog.at_level("ERROR", logger="app.call_state"):
        task = service._spawn(boom(), name="test-boom-task")
        with pytest.raises(RuntimeError, match="background task exploded"):
            await task

    assert any(
        record.levelname == "ERROR" and "test-boom-task" in record.getMessage()
        for record in caplog.records
    )
