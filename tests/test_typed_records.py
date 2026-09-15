"""R06: narrow typed records must agree with the historical dict API.

These tests pin the two things the refactor could silently break: an adapter that
disagrees with the raw row it wraps, and a record that refuses to load a call the
old dict path handled (nullable provider IDs, columns added after the row was
written). The wire format is asserted separately by the frozen contract snapshot.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.errors import (
    CALL_ENDING_PARTICIPANT_MESSAGE,
    ERROR_MESSAGES,
    CallRefusal,
    ErrorCode,
    error_payload,
)
from app.models import (
    AnswerCallQuestionRequest,
    CallState,
    ContextPacket,
    QuestionResolution,
    QuestionSource,
    QuestionStatus,
)
from app.records import (
    AnswerCommand,
    LifecycleRecord,
    PlanRecord,
    QuestionRecord,
    ToolExecutionResult,
)
from tests.conftest import seed_call

DEADLINE = "2030-01-01T00:00:00+00:00"


async def test_plan_dict_and_record_adapters_agree(database, packet: ContextPacket) -> None:
    plan_id = "plan_typed"
    expires = datetime.now(UTC) + timedelta(minutes=5)
    await database.create_plan(
        plan_id,
        packet.model_dump(mode="json"),
        "Owner explicitly requested this call",
        expires,
    )

    row = await database.get_plan(plan_id)
    record = await database.get_plan_record(plan_id)

    assert row is not None and record is not None
    assert record.plan_id == row["plan_id"]
    assert record.state == row["state"]
    assert record.authority_basis == row["authority_basis"]
    assert record.call_id == row["call_id"]
    assert record.confirmation_text == row["confirmation_text"]
    assert record.created_at == row["created_at"]
    assert record.expires_at == row["expires_at"]
    assert record.context == ContextPacket.model_validate(row["context"])
    assert await database.get_plan_record("plan_missing") is None


async def test_lifecycle_dict_and_record_adapters_agree(database, packet: ContextPacket) -> None:
    call_id = await seed_call(database, packet, state=CallState.ACTIVE)

    row = await database.get_call(call_id)
    record = await database.get_lifecycle_record(call_id)

    assert row is not None and record is not None
    assert record.call_id == row["call_id"]
    assert record.plan_id == row["plan_id"]
    assert record.state is CallState(row["state"])
    assert record.termination_claimed is bool(row["termination_claimed"])
    assert record.termination_reason == row["termination_reason"]
    assert record.transfer_outcome == row["transfer_outcome"]
    assert record.started_at == row["started_at"]
    assert record.ended_at == row["ended_at"]
    assert record.duration_seconds == row["duration_seconds"]
    assert record.is_closed is False
    assert await database.get_lifecycle_record("call_missing") is None


async def test_lifecycle_record_matches_termination_claim(database, packet: ContextPacket) -> None:
    call_id = await seed_call(database, packet, state=CallState.ACTIVE)

    assert await database.claim_termination(call_id, "owner_request") is not None
    record = await database.get_lifecycle_record(call_id)

    assert record is not None
    assert record.claimed_terminating("owner_request")
    assert not record.claimed_terminating("watchdog_stale")
    assert record.is_closed and not record.is_terminal
    assert not record.is_finished_termination(
        state=CallState.COMPLETED, reason="owner_request", transfer_outcome=None
    )

    assert await database.finish_claimed_termination(
        call_id,
        expected_reason="owner_request",
        terminal_state=CallState.COMPLETED,
        ended_at=datetime.now(UTC).isoformat(),
        duration_seconds=12,
    )
    finished = await database.get_lifecycle_record(call_id)
    assert finished is not None
    assert finished.is_finished_termination(
        state=CallState.COMPLETED, reason="owner_request", transfer_outcome=None
    )
    assert finished.is_terminal and finished.is_closed
    assert not finished.claimed_terminating("owner_request")


async def test_question_adapters_agree_with_dict_reads(database, packet: ContextPacket) -> None:
    call_id = await seed_call(database, packet, state=CallState.ACTIVE)

    record, error = await database.create_question_record(
        call_id,
        tool_call_id="tc_typed",
        question="What is the reference number?",
        reason="callee asked",
        deadline_at=DEADLINE,
        max_questions=5,
    )
    assert error is None and record is not None
    assert record.status is QuestionStatus.PENDING
    assert record.answer is None and record.resolved_at is None

    row = await database.get_question(record.question_id)
    assert row is not None
    assert record.to_event() == {
        "sequence": row["sequence_number"],
        "type": "question",
        "question_id": row["question_id"],
        "question": row["question"],
        "reason": row["reason"],
        "status": row["status"],
        "asked_at": row["asked_at"],
        "deadline_at": row["deadline_at"],
    }
    assert await database.get_question_records_after(call_id, 0) == [record]

    answered = await database.claim_question_answer_record(
        call_id, record.question_id, "Reference 42"
    )
    assert answered is not None
    assert answered.status is QuestionStatus.ANSWERED
    assert answered.answer == "Reference 42"
    assert answered.resolved_at is not None
    # The frozen claim still wins exactly once.
    assert await database.claim_question_answer_record(call_id, record.question_id, "again") is None


async def test_question_expiry_and_cancel_adapters(database, packet: ContextPacket) -> None:
    call_id = await seed_call(database, packet, state=CallState.ACTIVE)
    created, _ = await database.create_question_record(
        call_id,
        tool_call_id="tc_expire",
        question="Is the gate open?",
        reason=None,
        deadline_at=DEADLINE,
        max_questions=5,
    )
    assert created is not None
    expired = await database.claim_question_expiry_record(created.question_id)
    assert expired is not None and expired.status is QuestionStatus.EXPIRED

    other_call = await seed_call(
        database,
        packet,
        call_id="call_typed_cancel",
        openai_call_id="rtc_typed_cancel",
        state=CallState.ACTIVE,
    )
    pending, _ = await database.create_question_record(
        other_call,
        tool_call_id="tc_cancel",
        question="Is the door unlocked?",
        reason=None,
        deadline_at=DEADLINE,
        max_questions=5,
    )
    assert pending is not None
    cancelled = await database.cancel_pending_question_records(other_call)
    assert [item.question_id for item in cancelled] == [pending.question_id]
    assert cancelled[0].status is QuestionStatus.CANCELLED


def test_record_adapters_tolerate_missing_historical_fields(packet: ContextPacket) -> None:
    # A row written before a column existed simply omits the key; the adapter fills
    # the historical default instead of failing the read.
    plan = PlanRecord.from_row(
        {
            "plan_id": "plan_legacy",
            "state": "prepared",
            "context": packet.model_dump(mode="json"),
            "created_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2026-01-01T00:05:00+00:00",
        }
    )
    assert plan.authority_basis is None
    assert plan.call_id is None
    assert plan.confirmation_text is None
    assert plan.context == packet

    lifecycle = LifecycleRecord.from_row({"call_id": "call_legacy", "state": "prewarming"})
    assert lifecycle.plan_id is None
    assert lifecycle.state is CallState.PREWARMING
    assert lifecycle.termination_claimed is False
    assert lifecycle.started_at is None
    assert lifecycle.duration_seconds is None
    assert not lifecycle.is_closed


async def test_call_with_null_provider_ids_still_adapts(database, packet: ContextPacket) -> None:
    call_id = await seed_call(database, packet, state=CallState.PREWARMING)
    await database.update_call(
        call_id,
        openai_call_id=None,
        twilio_ai_call_sid=None,
        twilio_callee_call_sid=None,
    )

    row = await database.get_call(call_id)
    record = await database.get_lifecycle_record(call_id)

    assert row is not None and record is not None
    assert row["openai_call_id"] is None
    assert row["twilio_ai_call_sid"] is None
    assert row["twilio_callee_call_sid"] is None
    assert record.state is CallState.PREWARMING
    assert record.started_at is not None


async def test_named_promote_transitions_replace_generic_cas(
    database, packet: ContextPacket
) -> None:
    call_id = await seed_call(database, packet, state=CallState.PREWARMING)

    assert await database.promote_to_ready_to_activate(call_id) is True
    assert await database.promote_to_ready_to_activate(call_id) is False
    assert await database.promote_to_activating(call_id) is True
    assert await database.promote_to_activating(call_id) is False
    assert await database.promote_to_active(call_id) is True
    assert await database.promote_to_active(call_id) is False

    row = await database.get_call(call_id)
    assert row is not None
    assert row["state"] == CallState.ACTIVE.value


def test_enum_serialization_matches_the_wire_values() -> None:
    assert CallState.READY_TO_ACTIVATE.value == "ready_to_activate"
    assert CallState.TRANSFERRED.value == "transferred"
    assert QuestionStatus.PENDING.value == "pending"
    assert QuestionStatus.ANSWERED.value == "answered"
    assert QuestionStatus.EXPIRED.value == "expired"
    assert QuestionStatus.CANCELLED.value == "cancelled"
    assert QuestionResolution.FOUND.value == "found"
    assert QuestionResolution.NOT_FOUND.value == "not_found"
    assert QuestionSource.AGENT_MEMORY.value == "agent_memory"
    assert QuestionSource.CONVERSATION_HISTORY.value == "conversation_history"


def test_answer_command_from_request_preserves_attestation() -> None:
    request = AnswerCallQuestionRequest(
        call_id="call_1",
        question_id="q_1",
        answer="  Reference 42.  ",
        resolution=QuestionResolution.NOT_FOUND,
        sources_checked=[
            QuestionSource.AGENT_MEMORY,
            QuestionSource.CONVERSATION_HISTORY,
            QuestionSource.EMAIL,
        ],
    )

    command = AnswerCommand.from_request(request)

    assert command == AnswerCommand(
        call_id="call_1",
        question_id="q_1",
        answer="Reference 42.",
        resolution=QuestionResolution.NOT_FOUND,
        sources_checked=(
            QuestionSource.AGENT_MEMORY,
            QuestionSource.CONVERSATION_HISTORY,
            QuestionSource.EMAIL,
        ),
    )


def test_tool_execution_result_shapes_are_stable() -> None:
    assert ToolExecutionResult.rejected("unknown tool").to_payload() == {
        "accepted": False,
        "error": "unknown tool",
    }
    assert ToolExecutionResult.ask_agent_rejected("question_pending").to_payload() == {
        "status": "error",
        "error": "question_pending",
    }
    assert ToolExecutionResult.ask_agent_answered("Reference 42").to_payload() == {
        "status": "answered",
        "answer": "Reference 42",
    }
    assert ToolExecutionResult.ask_agent_timed_out("no answer").to_payload() == {
        "status": "timeout",
        "error": "no_answer_from_agent",
        "guidance": "no answer",
    }

    result = ToolExecutionResult({"ok": True})
    mutated = result.to_payload()
    mutated["ok"] = False
    assert result.output == {"ok": True}


def test_error_registry_keeps_the_transport_shape() -> None:
    assert error_payload(ErrorCode.CALL_BUSY) == {
        "code": "call_busy",
        "message": ERROR_MESSAGES[ErrorCode.CALL_BUSY],
    }

    refusal = CallRefusal(ErrorCode.PLAN_UNAVAILABLE)
    assert isinstance(refusal, ValueError)
    assert json.loads(str(refusal)) == {
        "code": "plan_unavailable",
        "message": ERROR_MESSAGES[ErrorCode.PLAN_UNAVAILABLE],
    }

    # Policy codes are not part of the stable registry; they ride through unchanged
    # with their own details payload.
    policy = CallRefusal("invalid_e164", "Destination must be valid E.164", details={})
    assert policy.payload == {
        "code": "invalid_e164",
        "message": "Destination must be valid E.164",
        "details": {},
    }

    # One wire code can carry two operator-facing messages.
    participant = CallRefusal(ErrorCode.CALL_ENDING, CALL_ENDING_PARTICIPANT_MESSAGE)
    assert json.loads(str(participant))["message"] == CALL_ENDING_PARTICIPANT_MESSAGE
    assert CALL_ENDING_PARTICIPANT_MESSAGE != ERROR_MESSAGES[ErrorCode.CALL_ENDING]

    # A non-registry code with no explicit message is a programming error, not a
    # silent pass-through.
    with pytest.raises(ValueError, match="no default message"):
        error_payload("not_registered")


@pytest.mark.parametrize("missing", ["question_id", "call_id", "sequence_number"])
def test_question_record_rejects_rows_missing_required_columns(missing: str) -> None:
    row = {
        "question_id": "q_1",
        "call_id": "call_1",
        "tool_call_id": "tc_1",
        "sequence_number": 1,
        "question": "Where?",
        "status": "pending",
        "asked_at": DEADLINE,
        "deadline_at": DEADLINE,
    }
    row.pop(missing)
    with pytest.raises(ValueError, match=missing):
        QuestionRecord.from_row(row)
