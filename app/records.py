"""Narrow, use-case-scoped records for database exits and service entry points (R06).

Each record is a small read model for *one* use case -- planning, call lifecycle,
mid-call questions, answer submission, live tool output -- rather than a universal
wrapper around a whole row. Adapters live next to the records so the raw SQL and
transaction boundaries stay in ``app/db``; callers that still need the historical
``dict`` API keep using it unchanged.

Historical tolerance is deliberate: rows written before a column existed (for
example a plan without ``confirmation_text``, or a call without provider IDs yet)
load with a ``None``/empty default instead of raising, so an old database is never
made unreadable by this layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.models import (
    TERMINAL_STATES,
    AnswerCallQuestionRequest,
    CallState,
    ContextPacket,
    QuestionResolution,
    QuestionSource,
    QuestionStatus,
)


def _required_text(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"row is missing required column {key!r}")
    return value


def _optional_text(row: Mapping[str, Any], key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class PlanRecord:
    """Typed view of a ``plans`` row for the prepare/start use case.

    ``context`` is validated once at this boundary, so callers no longer re-parse
    ``plan["context"]`` at each use site.
    """

    plan_id: str
    state: str
    context: ContextPacket
    authority_basis: str | None
    call_id: str | None
    confirmation_text: str | None
    created_at: str
    expires_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> PlanRecord:
        return cls(
            plan_id=_required_text(row, "plan_id"),
            state=_required_text(row, "state"),
            context=ContextPacket.model_validate(row.get("context")),
            authority_basis=_optional_text(row, "authority_basis"),
            call_id=_optional_text(row, "call_id"),
            confirmation_text=_optional_text(row, "confirmation_text"),
            created_at=_required_text(row, "created_at"),
            expires_at=_required_text(row, "expires_at"),
        )


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    """Typed state-machine verdict for one call, used by lifecycle decisions.

    Deliberately narrow: it carries identity, the termination claim, and the timing
    milestones needed to decide a transition. Provider handles and usage columns are
    not part of this record; they belong to the transport/teardown paths.
    """

    call_id: str
    plan_id: str | None
    state: CallState
    termination_claimed: bool
    termination_reason: str | None
    transfer_outcome: str | None
    started_at: str | None
    ended_at: str | None
    duration_seconds: int | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> LifecycleRecord:
        duration = row.get("duration_seconds")
        return cls(
            call_id=_required_text(row, "call_id"),
            plan_id=_optional_text(row, "plan_id"),
            state=CallState(_required_text(row, "state")),
            termination_claimed=bool(row.get("termination_claimed")),
            termination_reason=_optional_text(row, "termination_reason"),
            transfer_outcome=_optional_text(row, "transfer_outcome"),
            started_at=_optional_text(row, "started_at"),
            ended_at=_optional_text(row, "ended_at"),
            duration_seconds=int(duration) if isinstance(duration, (int, float)) else None,
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_closed(self) -> bool:
        """True once termination owns the call or a terminal state is durable."""

        return self.state is CallState.TERMINATING or self.is_terminal

    def claimed_terminating(self, reason: str) -> bool:
        """Whether this row is the in-flight termination claim for ``reason``."""

        return (
            self.state is CallState.TERMINATING
            and self.termination_claimed
            and self.termination_reason == reason
        )

    def is_finished_termination(
        self,
        *,
        state: CallState,
        reason: str,
        transfer_outcome: str | None,
    ) -> bool:
        """Whether this row is the durable terminal result of the claim for ``reason``."""

        return (
            self.state is state
            and self.termination_claimed
            and self.termination_reason == reason
            and self.transfer_outcome == transfer_outcome
        )


@dataclass(frozen=True, slots=True)
class QuestionRecord:
    """Typed view of one ``call_questions`` row."""

    question_id: str
    call_id: str
    tool_call_id: str
    sequence_number: int
    question: str
    reason: str | None
    status: QuestionStatus
    answer: str | None
    asked_at: str
    deadline_at: str
    resolved_at: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> QuestionRecord:
        try:
            status = QuestionStatus(_required_text(row, "status"))
        except ValueError as exc:  # pragma: no cover - defensive against a corrupt row
            raise ValueError("question row has an unknown status") from exc
        sequence = row.get("sequence_number")
        if not isinstance(sequence, int):
            raise ValueError("row is missing required column 'sequence_number'")
        return cls(
            question_id=_required_text(row, "question_id"),
            call_id=_required_text(row, "call_id"),
            tool_call_id=_required_text(row, "tool_call_id"),
            sequence_number=sequence,
            question=_required_text(row, "question"),
            reason=_optional_text(row, "reason"),
            status=status,
            answer=_optional_text(row, "answer"),
            asked_at=_required_text(row, "asked_at"),
            deadline_at=_required_text(row, "deadline_at"),
            resolved_at=_optional_text(row, "resolved_at"),
        )

    def to_event(self) -> dict[str, Any]:
        """Serialize into the ``wait_for_call_event`` question event shape."""

        return {
            "sequence": self.sequence_number,
            "type": "question",
            "question_id": self.question_id,
            "question": self.question,
            "reason": self.reason,
            "status": self.status.value,
            "asked_at": self.asked_at,
            "deadline_at": self.deadline_at,
        }


@dataclass(frozen=True, slots=True)
class AnswerCommand:
    """Validated mid-call answer submission carried from the MCP entry point.

    The attestation rule for ``not_found`` (both owner-specific sources checked) is
    owned by :class:`app.models.AnswerCallQuestionRequest`, which is the transport
    adapter; this record only carries the decision into the core.
    """

    call_id: str
    question_id: str
    answer: str
    resolution: QuestionResolution = QuestionResolution.FOUND
    sources_checked: tuple[QuestionSource, ...] = (QuestionSource.AGENT_MEMORY,)

    @classmethod
    def from_request(cls, request: AnswerCallQuestionRequest) -> AnswerCommand:
        return cls(
            call_id=request.call_id,
            question_id=request.question_id,
            answer=request.answer,
            resolution=request.resolution,
            sources_checked=tuple(request.sources_checked),
        )


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """One live function-tool outcome, ready to send back to the model.

    ``output`` is the exact JSON body the model receives. The named constructors keep
    the historically distinct shapes in one place so a caller cannot silently drift
    the agent-facing contract.
    """

    output: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return dict(self.output)

    @classmethod
    def rejected(cls, error: str) -> ToolExecutionResult:
        """``accepted``-style rejection used by the non-ask_agent tools."""

        return cls({"accepted": False, "error": error})

    @classmethod
    def ask_agent_rejected(cls, error: str) -> ToolExecutionResult:
        return cls({"status": "error", "error": error})

    @classmethod
    def ask_agent_answered(cls, answer: str) -> ToolExecutionResult:
        return cls({"status": "answered", "answer": answer})

    @classmethod
    def ask_agent_timed_out(cls, guidance: str) -> ToolExecutionResult:
        return cls(
            {
                "status": "timeout",
                "error": "no_answer_from_agent",
                "guidance": guidance,
            }
        )
