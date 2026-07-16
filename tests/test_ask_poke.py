from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.call_state import WATCHDOG_QUESTION_GRACE_SECONDS, PendingQuestion
from app.models import CallState, StartPhoneCallOutput
from app.openai_realtime import RealtimeBridge
from tests.conftest import seed_call, wait_background


def _tool_event(tool_call_id: str, name: str, arguments: str) -> dict[str, str]:
    return {
        "type": "response.function_call_arguments.done",
        "event_id": f"evt_{tool_call_id}",
        "call_id": tool_call_id,
        "name": name,
        "arguments": arguments,
    }


async def _ask(
    service,
    call_id: str,
    tool_call_id: str = "tc_1",
    question: str = "What is the owner's preferred pharmacy?",
    reason: str | None = "callee asked",
) -> None:
    payload = {"question": question}
    if reason is not None:
        payload["reason"] = reason
    import json

    await service.handle_realtime_event(
        call_id,
        _tool_event(tool_call_id, "ask_poke", json.dumps(payload)),
    )


def test_start_phone_call_output_routes_monitoring_through_event_poll():
    output = StartPhoneCallOutput(call_id="call_1", state=CallState.PREWARMING)

    assert "wait_for_call_event" in output.next_action
    assert "answer_call_question" in output.next_action
    assert "get_call_result" in output.next_action


@pytest.fixture
async def ask_service(service):
    service.settings.ask_poke_enabled = True
    return service


@pytest.mark.asyncio
async def test_ask_persists_without_immediate_tool_output(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)

    await _ask(ask_service, call_id, tool_call_id="tc_1")
    await wait_background()

    rows = await ask_service.db.get_questions_after(call_id, 0)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["sequence_number"] == 1
    assert rows[0]["tool_call_id"] == "tc_1"
    assert rows[0]["question"] == "What is the owner's preferred pharmacy?"
    assert not any(result[1] == "tc_1" for result in ask_service._test_realtime.tool_results)
    assert call_id in ask_service._pending_questions
    call = await ask_service.db.get_call(call_id)
    assert call["tool_call_count"] == 1


@pytest.mark.asyncio
async def test_answer_delivers_correlated_output_and_continuation(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_1")
    rows = await ask_service.db.get_questions_after(call_id, 0)
    question_id = rows[0]["question_id"]

    result = await ask_service.answer_call_question(call_id, question_id, "CVS on Market Street")
    await wait_background()

    assert result == {"status": "accepted", "question_id": question_id}
    assert ask_service._test_realtime.tool_results[-1] == (
        call_id,
        "tc_1",
        {"status": "answered", "answer": "CVS on Market Street"},
    )
    text = ask_service._test_realtime.tool_result_continuation_texts[-1]
    assert text is not None
    assert "Relay the relevant part" in text
    row = await ask_service.db.get_question(question_id)
    assert row["status"] == "answered"
    assert call_id not in ask_service._pending_questions


@pytest.mark.asyncio
async def test_timeout_exactly_once_then_answer_is_expired(ask_service, packet):
    ask_service.settings.ask_poke_answer_timeout_seconds = 0.05
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_timeout")
    rows = await ask_service.db.get_questions_after(call_id, 0)
    question_id = rows[0]["question_id"]

    await asyncio.sleep(0.12)
    await wait_background()

    timeout_results = [r for r in ask_service._test_realtime.tool_results if r[1] == "tc_timeout"]
    assert len(timeout_results) == 1
    assert timeout_results[0][2]["status"] == "timeout"
    assert timeout_results[0][2]["error"] == "no_answer_from_poke"
    row = await ask_service.db.get_question(question_id)
    assert row["status"] == "expired"

    late = await ask_service.answer_call_question(call_id, question_id, "too late")
    await wait_background()
    assert late["status"] == "expired"
    assert len([r for r in ask_service._test_realtime.tool_results if r[1] == "tc_timeout"]) == 1


@pytest.mark.asyncio
async def test_answer_timeout_race_exactly_one_output(ask_service, packet):
    ask_service.settings.ask_poke_answer_timeout_seconds = 0.05
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_race")
    rows = await ask_service.db.get_questions_after(call_id, 0)
    question_id = rows[0]["question_id"]

    answer_task = asyncio.create_task(
        ask_service.answer_call_question(call_id, question_id, "race winner answer")
    )
    await asyncio.sleep(0.12)
    await answer_task
    await wait_background()

    correlated = [r for r in ask_service._test_realtime.tool_results if r[1] == "tc_race"]
    assert len(correlated) == 1
    assert correlated[0][2]["status"] in {"answered", "timeout"}
    final = await ask_service.db.get_question(question_id)
    assert final["status"] in {"answered", "expired"}


@pytest.mark.asyncio
async def test_duplicate_answer_idempotent_and_unknown_question_errors(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id)
    rows = await ask_service.db.get_questions_after(call_id, 0)
    question_id = rows[0]["question_id"]

    first = await ask_service.answer_call_question(call_id, question_id, "first")
    second = await ask_service.answer_call_question(call_id, question_id, "second")
    await wait_background()
    assert first["status"] == "accepted"
    assert second["status"] == "already_answered"
    assert len([r for r in ask_service._test_realtime.tool_results if r[1] == "tc_1"]) == 1

    with pytest.raises(LookupError, match="unknown question"):
        await ask_service.answer_call_question(call_id, "missing_qid", "nope")
    with pytest.raises(LookupError, match="unknown question"):
        await ask_service.answer_call_question("other_call", question_id, "nope")


@pytest.mark.asyncio
async def test_second_ask_while_pending_returns_error_and_keeps_call_active(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_a")
    await _ask(
        ask_service,
        call_id,
        tool_call_id="tc_b",
        question="A different pending question text",
    )
    await wait_background()

    second = [r for r in ask_service._test_realtime.tool_results if r[1] == "tc_b"]
    assert len(second) == 1
    assert second[0][2] == {"status": "error", "error": "question_pending"}
    assert not any(r[1] == "tc_a" for r in ask_service._test_realtime.tool_results)
    call = await ask_service.db.get_call(call_id)
    assert call["state"] == CallState.ACTIVE.value
    assert await ask_service.db.count_call_questions(call_id) == 1


@pytest.mark.asyncio
async def test_question_limit_returns_error(ask_service, packet):
    ask_service.settings.ask_poke_max_questions_per_call = 1
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_limit_1")
    rows = await ask_service.db.get_questions_after(call_id, 0)
    await ask_service.answer_call_question(call_id, rows[0]["question_id"], "done")
    await wait_background()

    await _ask(
        ask_service,
        call_id,
        tool_call_id="tc_limit_2",
        question="Another allowed-length question here",
    )
    await wait_background()
    limited = [r for r in ask_service._test_realtime.tool_results if r[1] == "tc_limit_2"]
    assert limited[-1][2] == {"status": "error", "error": "question_limit_reached"}


@pytest.mark.asyncio
async def test_termination_cancels_pending_and_late_answer_is_call_ended(ask_service, packet):
    ask_service.settings.ask_poke_answer_timeout_seconds = 30.0
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_term")
    rows = await ask_service.db.get_questions_after(call_id, 0)
    question_id = rows[0]["question_id"]
    before = len(ask_service._test_realtime.tool_results)

    await ask_service.terminate_call(call_id, "owner_request")
    await wait_background()

    row = await ask_service.db.get_question(question_id)
    assert row["status"] == "cancelled"
    assert call_id not in ask_service._pending_questions
    assert len(ask_service._test_realtime.tool_results) == before

    late = await ask_service.answer_call_question(call_id, question_id, "after end")
    assert late["status"] == "call_ended"


@pytest.mark.asyncio
async def test_recovery_cancels_pending_questions(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_recover")
    rows = await ask_service.db.get_questions_after(call_id, 0)
    question_id = rows[0]["question_id"]

    await ask_service.recover_startup()
    await wait_background()

    row = await ask_service.db.get_question(question_id)
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_watchdog_carve_out_skips_stale_while_question_pending(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    stale = datetime.now(UTC) - timedelta(minutes=1)
    await ask_service.db.execute(
        "UPDATE calls SET last_event_at=? WHERE call_id=?",
        (stale.isoformat(), call_id),
    )
    ask_service._pending_questions[call_id] = PendingQuestion(
        question_id="q_watch",
        tool_call_id="tc_watch",
        deadline_monotonic=time.monotonic() + 30.0,
    )

    await ask_service._watchdog_once()
    assert (await ask_service.db.get_call(call_id))["state"] == CallState.ACTIVE.value

    ask_service._pending_questions[call_id] = PendingQuestion(
        question_id="q_watch",
        tool_call_id="tc_watch",
        deadline_monotonic=time.monotonic() - WATCHDOG_QUESTION_GRACE_SECONDS - 1.0,
    )
    await ask_service._watchdog_once()
    await wait_background()
    assert (await ask_service.db.get_call(call_id))["state"] == CallState.TIMED_OUT.value


@pytest.mark.asyncio
async def test_watchdog_carve_out_skips_while_question_delivering(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    stale = datetime.now(UTC) - timedelta(minutes=1)
    await ask_service.db.execute(
        "UPDATE calls SET last_event_at=? WHERE call_id=?",
        (stale.isoformat(), call_id),
    )
    ask_service._pending_questions[call_id] = PendingQuestion(
        question_id="q_deliver",
        tool_call_id="tc_deliver",
        deadline_monotonic=time.monotonic() - WATCHDOG_QUESTION_GRACE_SECONDS - 1.0,
        delivering=True,
    )

    await ask_service._watchdog_once()
    assert (await ask_service.db.get_call(call_id))["state"] == CallState.ACTIVE.value


@pytest.mark.asyncio
async def test_ask_poke_while_voice_end_pending_returns_call_ending(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    ask_service._voice_end_pending[call_id] = ("end_tool", None)

    await _ask(ask_service, call_id, tool_call_id="tc_ending")
    await wait_background()

    result = [r for r in ask_service._test_realtime.tool_results if r[1] == "tc_ending"]
    assert result[-1][2] == {"status": "error", "error": "call_ending"}


@pytest.mark.asyncio
async def test_ask_poke_disabled_returns_error_and_tool_absent_from_payload(
    service, packet, settings
):
    assert service.settings.ask_poke_enabled is False
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await _ask(service, call_id, tool_call_id="tc_disabled")
    await wait_background()
    result = [r for r in service._test_realtime.tool_results if r[1] == "tc_disabled"]
    assert result[-1][2] == {"status": "error", "error": "ask_poke_disabled"}

    async def _noop(*args, **kwargs) -> None:
        return None

    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=_noop,
        on_fatal=_noop,
    )
    names = [tool.name for tool in bridge.build_accept_payload(packet).tools]
    assert "ask_poke" not in names


@pytest.mark.asyncio
async def test_wait_for_call_event_immediate_events_and_cursor(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id)
    rows = await ask_service.db.get_questions_after(call_id, 0)

    result = await ask_service.wait_for_call_event(call_id, after_sequence=0, timeout_seconds=0.1)
    assert len(result["events"]) == 1
    assert result["events"][0]["question_id"] == rows[0]["question_id"]
    assert result["events"][0]["status"] == "pending"
    assert result["next_after_sequence"] == 1
    assert result["terminal"] is False

    empty = await ask_service.wait_for_call_event(call_id, after_sequence=1, timeout_seconds=0.05)
    assert empty["events"] == []
    assert empty["next_after_sequence"] == 1


@pytest.mark.asyncio
async def test_wait_for_call_event_wakes_on_new_question(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)

    async def ask_soon() -> None:
        await asyncio.sleep(0.05)
        await _ask(ask_service, call_id, tool_call_id="tc_wake")

    task = asyncio.create_task(ask_soon())
    started = time.monotonic()
    result = await ask_service.wait_for_call_event(call_id, after_sequence=0, timeout_seconds=2.0)
    await task
    elapsed = time.monotonic() - started
    assert elapsed < 1.5
    assert len(result["events"]) == 1
    assert result["events"][0]["status"] == "pending"
    assert result["events"][0]["question_id"]


@pytest.mark.asyncio
async def test_wait_for_call_event_wakes_on_terminal(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)

    async def terminate_soon() -> None:
        await asyncio.sleep(0.05)
        await ask_service.terminate_call(call_id, "owner_request")

    task = asyncio.create_task(terminate_soon())
    result = await ask_service.wait_for_call_event(call_id, after_sequence=0, timeout_seconds=2.0)
    await task
    await wait_background()
    assert result["terminal"] is True
    assert "get_call_result" in result["next_action"]
    assert call_id not in ask_service._event_notifiers


@pytest.mark.asyncio
async def test_wait_for_call_event_terminating_is_not_terminal(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.TERMINATING)

    result = await ask_service.wait_for_call_event(call_id, after_sequence=0, timeout_seconds=0.05)

    assert result["terminal"] is False
    assert result["state"] == CallState.TERMINATING.value
    assert "get_call_result" not in result["next_action"]
    assert "re-enter wait_for_call_event" in result["next_action"]


@pytest.mark.asyncio
async def test_answer_rejected_while_call_terminating(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_term_race")
    rows = await ask_service.db.get_questions_after(call_id, 0)
    question_id = rows[0]["question_id"]

    await ask_service.db.execute(
        "UPDATE calls SET state=?, termination_claimed=1 WHERE call_id=?",
        (CallState.TERMINATING.value, call_id),
    )

    result = await ask_service.answer_call_question(call_id, question_id, "too late")
    await wait_background()

    assert result["status"] == "call_ended"
    row = await ask_service.db.get_question(question_id)
    assert row["status"] == "pending"
    assert not any(r[1] == "tc_term_race" for r in ask_service._test_realtime.tool_results)


@pytest.mark.asyncio
async def test_wait_for_call_event_timeout_returns_empty_events(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    started = time.monotonic()
    result = await ask_service.wait_for_call_event(call_id, after_sequence=0, timeout_seconds=0.08)
    elapsed = time.monotonic() - started
    assert elapsed >= 0.05
    assert result["events"] == []
    assert result["terminal"] is False
    assert result["state"] == CallState.ACTIVE.value


@pytest.mark.asyncio
async def test_wait_for_call_event_clamps_timeout(ask_service, packet):
    ask_service.settings.wait_for_call_event_max_seconds = 0.05
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    started = time.monotonic()
    result = await ask_service.wait_for_call_event(call_id, after_sequence=0, timeout_seconds=30.0)
    elapsed = time.monotonic() - started
    assert elapsed < 0.5
    assert result["events"] == []


@pytest.mark.asyncio
async def test_wait_for_call_event_unknown_call_raises(ask_service):
    with pytest.raises(LookupError):
        await ask_service.wait_for_call_event("missing_call", timeout_seconds=0.01)
    assert "missing_call" not in ask_service._event_notifiers


@pytest.mark.asyncio
async def test_wait_for_call_event_terminal_does_not_retain_notifier(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.COMPLETED)

    result = await ask_service.wait_for_call_event(call_id, timeout_seconds=0.01)

    assert result["terminal"] is True
    assert call_id not in ask_service._event_notifiers


@pytest.mark.asyncio
async def test_answer_delivery_cancels_active_response(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_cancel")
    rows = await ask_service.db.get_questions_after(call_id, 0)
    ask_service._active_response_ids[call_id] = "resp_active"

    await ask_service.answer_call_question(call_id, rows[0]["question_id"], "the answer")
    await wait_background()

    assert ("cancel_response", call_id) in ask_service._test_realtime.events
    assert call_id not in ask_service._active_response_ids
    delivered = [r for r in ask_service._test_realtime.tool_results if r[1] == "tc_cancel"]
    assert len(delivered) == 1
    assert delivered[0][2]["status"] == "answered"


@pytest.mark.asyncio
async def test_end_call_cancels_pending_question(ask_service, packet):
    ask_service.settings.ask_poke_answer_timeout_seconds = 30.0
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_pending")
    rows = await ask_service.db.get_questions_after(call_id, 0)
    question_id = rows[0]["question_id"]

    await ask_service.handle_realtime_event(
        call_id,
        _tool_event("tc_end", "end_call", '{"reason": "objective_completed"}'),
    )
    await wait_background()

    row = await ask_service.db.get_question(question_id)
    assert row["status"] == "cancelled"
    assert call_id not in ask_service._pending_questions
    assert not any(r[1] == "tc_pending" for r in ask_service._test_realtime.tool_results)

    late = await ask_service.answer_call_question(call_id, question_id, "too late")
    await wait_background()
    assert late["status"] == "call_ended"
    assert not any(r[1] == "tc_pending" for r in ask_service._test_realtime.tool_results)


@pytest.mark.asyncio
async def test_wait_for_call_event_clamps_huge_after_sequence(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    result = await ask_service.wait_for_call_event(
        call_id, after_sequence=10**30, timeout_seconds=0.01
    )
    assert result["events"] == []
    assert result["terminal"] is False


@pytest.mark.asyncio
async def test_duplicate_tool_call_id_reports_duplicate_error(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_dup")
    rows = await ask_service.db.get_questions_after(call_id, 0)
    await ask_service.answer_call_question(call_id, rows[0]["question_id"], "first answer")
    await wait_background()

    await _ask(
        ask_service,
        call_id,
        tool_call_id="tc_dup",
        question="A second question reusing the tool id",
    )
    await wait_background()

    results = [r for r in ask_service._test_realtime.tool_results if r[1] == "tc_dup"]
    assert results[-1][2] == {"status": "error", "error": "duplicate_tool_call"}
    assert await ask_service.db.count_call_questions(call_id) == 1


@pytest.mark.asyncio
async def test_redelivered_pending_ask_is_idempotent(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_redeliver")
    await wait_background()
    rows = await ask_service.db.get_questions_after(call_id, 0)
    question_id = rows[0]["question_id"]
    before_results = list(ask_service._test_realtime.tool_results)

    await _ask(
        ask_service,
        call_id,
        tool_call_id="tc_redeliver",
        question="What is the owner's preferred pharmacy?",
    )
    await wait_background()

    assert ask_service._test_realtime.tool_results == before_results
    assert await ask_service.db.count_call_questions(call_id) == 1
    assert call_id in ask_service._pending_questions
    assert ask_service._pending_questions[call_id].question_id == question_id

    accepted = await ask_service.answer_call_question(call_id, question_id, "Walgreens")
    await wait_background()
    assert accepted["status"] == "accepted"
    delivered = [r for r in ask_service._test_realtime.tool_results if r[1] == "tc_redeliver"]
    assert len(delivered) == 1
    assert delivered[0][2] == {"status": "answered", "answer": "Walgreens"}


@pytest.mark.asyncio
async def test_redelivered_pending_ask_at_question_limit_is_idempotent(ask_service, packet):
    ask_service.settings.ask_poke_max_questions_per_call = 1
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_quota_redeliver")
    await wait_background()
    rows = await ask_service.db.get_questions_after(call_id, 0)
    question_id = rows[0]["question_id"]
    before_results = list(ask_service._test_realtime.tool_results)

    await _ask(
        ask_service,
        call_id,
        tool_call_id="tc_quota_redeliver",
        question="What is the owner's preferred pharmacy?",
    )
    await wait_background()

    assert ask_service._test_realtime.tool_results == before_results
    assert await ask_service.db.count_call_questions(call_id) == 1
    row = await ask_service.db.get_question(question_id)
    assert row["status"] == "pending"

    accepted = await ask_service.answer_call_question(call_id, question_id, "Walgreens")
    await wait_background()
    assert accepted["status"] == "accepted"
    delivered = [r for r in ask_service._test_realtime.tool_results if r[1] == "tc_quota_redeliver"]
    assert len(delivered) == 1


@pytest.mark.asyncio
async def test_expiry_does_not_claim_once_call_is_terminating(ask_service, packet):
    ask_service.settings.ask_poke_answer_timeout_seconds = 0.05
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_expire_term")
    rows = await ask_service.db.get_questions_after(call_id, 0)
    question_id = rows[0]["question_id"]

    await ask_service.db.execute(
        "UPDATE calls SET state=?, termination_claimed=1 WHERE call_id=?",
        (CallState.TERMINATING.value, call_id),
    )

    await asyncio.sleep(0.12)
    await wait_background()

    # Termination owns the call: the deadline must not inject a timeout tool result,
    # and the row stays pending for cancel_pending_questions to claim.
    assert not any(r[1] == "tc_expire_term" for r in ask_service._test_realtime.tool_results)
    row = await ask_service.db.get_question(question_id)
    assert row["status"] == "pending"
    assert call_id not in ask_service._pending_questions

    cancelled = await ask_service.db.cancel_pending_questions(call_id)
    assert [q["question_id"] for q in cancelled] == [question_id]
    late = await ask_service.answer_call_question(call_id, question_id, "too late")
    assert late["status"] == "call_ended"


@pytest.mark.asyncio
async def test_failed_answer_delivery_is_retryable(ask_service, packet):
    call_id = await seed_call(ask_service.db, packet, state=CallState.ACTIVE)
    await _ask(ask_service, call_id, tool_call_id="tc_retry")
    rows = await ask_service.db.get_questions_after(call_id, 0)
    question_id = rows[0]["question_id"]

    ask_service._test_realtime.tool_result_failures_remaining = 1
    first = await ask_service.answer_call_question(call_id, question_id, "CVS on Market")
    await wait_background()

    assert first["status"] == "accepted"
    assert not any(r[1] == "tc_retry" for r in ask_service._test_realtime.tool_results)
    row = await ask_service.db.get_question(question_id)
    assert row["status"] == "answered"

    retry = await ask_service.answer_call_question(call_id, question_id, "ignored retry text")
    await wait_background()

    assert retry == {"status": "accepted", "question_id": question_id}
    delivered = [r for r in ask_service._test_realtime.tool_results if r[1] == "tc_retry"]
    assert len(delivered) == 1
    # The originally claimed answer wins; the retry only re-attempts delivery.
    assert delivered[0][2] == {"status": "answered", "answer": "CVS on Market"}
    assert call_id not in ask_service._pending_questions

    third = await ask_service.answer_call_question(call_id, question_id, "again")
    await wait_background()
    assert third["status"] == "already_answered"
    assert len([r for r in ask_service._test_realtime.tool_results if r[1] == "tc_retry"]) == 1
