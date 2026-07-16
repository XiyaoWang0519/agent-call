from __future__ import annotations

from typing import Any

from app.db.engine import _decode_json_columns, _iso_now
from app.models import CallState

# Transfers are legal once the callee has joined, even while async AMD/activation is
# still converging toward 'active'. The promote CAS moves state to terminating
# regardless of which live state it started from.
TRANSFER_ELIGIBLE_STATES = ("prewarming", "ready_to_activate", "activating", "active")


class TransfersMixin:
    async def claim_transfer_joining(self, call_id: str, reason: str) -> bool:
        """Durably allow at most one owner-transfer attempt for a live call.

        Eligible once the callee has joined, even before activation completes.
        """

        placeholders, states = self._in_clause(TRANSFER_ELIGIBLE_STATES)
        return await self._execute_cas(
            f"""UPDATE calls SET transfer_outcome=?, last_event_at=?
               WHERE call_id=?
                 AND transfer_outcome IS NULL
                 AND termination_claimed=0
                 AND callee_joined=1
                 AND state IN ({placeholders})""",  # noqa: S608
            (f"joining:{reason}", _iso_now(), call_id, *states),
        )

    async def record_transfer_owner_sid(
        self, call_id: str, expected: str, owner_call_sid: str
    ) -> bool:
        """Persist the owner leg before transfer promotion can become recoverable."""

        placeholders, states = self._in_clause(TRANSFER_ELIGIBLE_STATES)
        return await self._execute_cas(
            f"""UPDATE calls SET twilio_owner_call_sid=?, last_event_at=?
               WHERE call_id=?
                 AND transfer_outcome=?
                 AND termination_claimed=0
                 AND state IN ({placeholders})
                 AND (twilio_owner_call_sid IS NULL OR twilio_owner_call_sid=?)""",  # noqa: S608
            (
                owner_call_sid,
                _iso_now(),
                call_id,
                expected,
                *states,
                owner_call_sid,
            ),
        )

    async def promote_transfer(self, call_id: str, reason: str) -> dict[str, Any] | None:
        """Claim teardown ownership while promoting a joined owner transfer."""

        placeholders, states = self._in_clause(TRANSFER_ELIGIBLE_STATES)
        async with self._immediate_transaction() as conn:
            cursor = await conn.execute(
                f"""UPDATE calls
                   SET transfer_outcome=?,
                       termination_claimed=1,
                       state=?,
                       termination_reason='transfer_completed',
                       last_event_at=?
                   WHERE call_id=?
                     AND transfer_outcome=?
                     AND termination_claimed=0
                     AND state IN ({placeholders})
                   RETURNING *""",  # noqa: S608
                (
                    f"in_progress:{reason}",
                    CallState.TERMINATING.value,
                    _iso_now(),
                    call_id,
                    f"joining:{reason}",
                    *states,
                ),
            )
            row = await cursor.fetchone()
        return _decode_json_columns(dict(row)) if row else None

    async def fail_joining_transfer(self, call_id: str, expected: str, failure: str) -> bool:
        placeholders, states = self._in_clause(TRANSFER_ELIGIBLE_STATES)
        return await self._execute_cas(
            f"""UPDATE calls SET transfer_outcome=?, last_event_at=?
               WHERE call_id=?
                 AND transfer_outcome=?
                 AND termination_claimed=0
                 AND state IN ({placeholders})""",  # noqa: S608
            (failure, _iso_now(), call_id, expected, *states),
        )

    async def complete_promoted_transfer(self, call_id: str, expected: str, completed: str) -> bool:
        return await self._execute_cas(
            """UPDATE calls SET transfer_outcome=?, last_event_at=?
               WHERE call_id=?
                 AND transfer_outcome=?
                 AND termination_claimed=1
                 AND state='terminating'
                 AND termination_reason='transfer_completed'""",
            (completed, _iso_now(), call_id, expected),
        )

    async def fail_promoted_transfer(
        self,
        call_id: str,
        expected: str,
        failure: str,
        failure_reason: str,
    ) -> bool:
        return await self._execute_cas(
            """UPDATE calls
               SET transfer_outcome=?, termination_reason=?, last_event_at=?
               WHERE call_id=?
                 AND transfer_outcome=?
                 AND termination_claimed=1
                 AND state='terminating'
                 AND termination_reason='transfer_completed'""",
            (failure, failure_reason, _iso_now(), call_id, expected),
        )
