"""Central registry of stable refusal codes and their transport payloads (R06).

Every code the service emits to MCP or HTTP lives in :class:`ErrorCode`. Call sites
raise :class:`CallRefusal`, whose string form is the JSON payload those transports
already forwarded verbatim, so neither the wire format nor the message text changes.
Keeping the codes in one place is what lets the frozen-contract test treat this
registry -- rather than an ad-hoc scan for ``"code": "..."`` literals -- as the
definition of the external contract.

Policy violations keep their own domain codes (``invalid_e164`` and friends) and are
carried through :class:`CallRefusal` unchanged; those codes are validated by
``app.policy`` and are deliberately not part of :class:`ErrorCode`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ErrorCode(StrEnum):
    """Stable error codes exposed on the MCP and HTTP boundary."""

    LIVE_CALLS_DISABLED = "live_calls_disabled"
    SUPERVISION_UNAVAILABLE = "supervision_unavailable"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_MISMATCH = "confirmation_mismatch"
    PLAN_NOT_FOUND = "plan_not_found"
    PLAN_UNAVAILABLE = "plan_unavailable"
    DEPLOYMENT_IN_PROGRESS = "deployment_in_progress"
    CALL_BUSY = "call_busy"
    CALL_ENDING = "call_ending"
    CALL_NOT_FOUND = "call_not_found"
    UNKNOWN_QUESTION = "unknown_question"
    INVALID_CALL_STATE = "invalid_call_state"
    INVALID_ANSWER_SUBMISSION = "invalid_answer_submission"


# ``call_ending`` has two operator-facing causes with the same wire code: a call that
# is already shutting down, and a participant leg refused while shutdown commits.
CALL_ENDING_SHUTDOWN_MESSAGE = (
    "The current call is still shutting down; retry the confirmed call shortly"
)
CALL_ENDING_PARTICIPANT_MESSAGE = (
    "The call stopped accepting a new participant leg; retry the confirmed call shortly"
)

ERROR_MESSAGES: Mapping[ErrorCode, str] = MappingProxyType(
    {
        ErrorCode.LIVE_CALLS_DISABLED: (
            "Live calls are disabled in evaluation mode. prepare_phone_call is available; "
            "start_phone_call cannot originate provider legs. Set AGENT_CALL_PROFILE=live "
            "with real credentials to place a call."
        ),
        ErrorCode.SUPERVISION_UNAVAILABLE: (
            "Call supervision is not healthy; retry after readiness recovers"
        ),
        ErrorCode.CONFIRMATION_REQUIRED: (
            "Explicit confirmation and the read-back confirmation text are required"
        ),
        ErrorCode.CONFIRMATION_MISMATCH: (
            "Confirmation text must exactly match the prepared read-back summary"
        ),
        ErrorCode.PLAN_NOT_FOUND: "Unknown plan_id",
        ErrorCode.PLAN_UNAVAILABLE: "Plan is expired or already started",
        ErrorCode.DEPLOYMENT_IN_PROGRESS: (
            "A deployment is starting; retry the confirmed call shortly"
        ),
        ErrorCode.CALL_BUSY: (
            "Another call is already in progress; wait for it to finish before starting a new call"
        ),
        ErrorCode.CALL_ENDING: CALL_ENDING_SHUTDOWN_MESSAGE,
        ErrorCode.CALL_NOT_FOUND: "The call could not be found",
        ErrorCode.UNKNOWN_QUESTION: "unknown question",
        ErrorCode.INVALID_CALL_STATE: "The call is not in a state that allows this operation",
        ErrorCode.INVALID_ANSWER_SUBMISSION: "Answer rejected; the question remains pending.",
    }
)


def error_payload(
    code: ErrorCode | str,
    message: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build the transport payload for one refusal code.

    ``extra`` keys (for example ``details``, ``issues``, ``next_action``) are merged
    after ``code``/``message`` so callers keep the exact historical shape.
    """

    resolved_code = str(code)
    if message is None:
        try:
            message = ERROR_MESSAGES[ErrorCode(resolved_code)]
        except ValueError as exc:
            raise ValueError(
                f"no default message registered for error code {resolved_code!r}"
            ) from exc
    payload: dict[str, Any] = {"code": resolved_code, "message": message}
    payload.update(extra)
    return payload


def error_json(code: ErrorCode | str, message: str | None = None, **extra: Any) -> str:
    """Serialize :func:`error_payload` for transports that expect a JSON string."""

    return json.dumps(error_payload(code, message, **extra))


class CallRefusal(ValueError):
    """Refusal carrying a stable code plus its operator-facing message.

    Subclasses ``ValueError`` because that is what the MCP tool layer already catches
    and forwards as a ``ToolError``; ``str(refusal)`` is the JSON payload, so existing
    ``json.loads(str(exc))["code"]`` assertions keep working without a wire change.
    """

    def __init__(self, code: ErrorCode | str, message: str | None = None, **extra: Any) -> None:
        self.payload = error_payload(code, message, **extra)
        self.code = self.payload["code"]
        self.message = self.payload["message"]
        super().__init__(self.payload)

    def __str__(self) -> str:
        return json.dumps(self.payload)
