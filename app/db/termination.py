from __future__ import annotations

from typing import Any

from app.db.engine import _decode_json_columns, _iso_now
from app.models import TERMINAL_STATES, CallState


class TerminationMixin:
    async def claim_termination(self, call_id: str, reason: str) -> dict[str, Any] | None:
        """Atomically claim and enter termination, returning the claimed current row."""

        placeholders, terminal_states = self._in_clause(state.value for state in TERMINAL_STATES)
        async with self._immediate_transaction() as conn:
            cursor = await conn.execute(
                f"""UPDATE calls
                   SET termination_claimed=1,
                       state=?,
                       termination_reason=?,
                       transfer_outcome=CASE
                           WHEN transfer_outcome LIKE 'joining:%'
                           THEN 'failed:termination_won'
                           ELSE transfer_outcome
                       END,
                       last_event_at=?
                   WHERE call_id=?
                     AND termination_claimed=0
                     AND state NOT IN ({placeholders})
                     AND COALESCE(transfer_outcome, '') NOT LIKE 'in_progress:%'
                     AND COALESCE(transfer_outcome, '') NOT LIKE 'completed:%'
                   RETURNING *""",  # noqa: S608
                (CallState.TERMINATING.value, reason, _iso_now(), call_id, *terminal_states),
            )
            row = await cursor.fetchone()
        return _decode_json_columns(dict(row)) if row else None

    async def claim_startup_recovery(
        self,
        call_id: str,
        *,
        expected_transfer_outcome: str | None,
        completed_transfer: bool,
    ) -> dict[str, Any] | None:
        """Adopt a stranded nonterminal call without a reset/reclaim race."""

        reason = "transfer_completed" if completed_transfer else "startup_recovery"
        replacement = (
            expected_transfer_outcome
            if completed_transfer
            else ("failed:startup_recovery" if expected_transfer_outcome is not None else None)
        )
        placeholders, terminal_states = self._in_clause(state.value for state in TERMINAL_STATES)
        async with self._immediate_transaction() as conn:
            if expected_transfer_outcome is None:
                cursor = await conn.execute(
                    f"""UPDATE calls
                       SET termination_claimed=1,
                           state='terminating',
                           termination_reason=?,
                           last_event_at=?
                       WHERE call_id=?
                         AND transfer_outcome IS NULL
                         AND state NOT IN ({placeholders})
                       RETURNING *""",  # noqa: S608
                    (reason, _iso_now(), call_id, *terminal_states),
                )
            else:
                cursor = await conn.execute(
                    f"""UPDATE calls
                       SET termination_claimed=1,
                           state='terminating',
                           termination_reason=?,
                           transfer_outcome=?,
                           last_event_at=?
                       WHERE call_id=?
                         AND transfer_outcome=?
                         AND state NOT IN ({placeholders})
                       RETURNING *""",  # noqa: S608
                    (
                        reason,
                        replacement,
                        _iso_now(),
                        call_id,
                        expected_transfer_outcome,
                        *terminal_states,
                    ),
                )
            row = await cursor.fetchone()
        return _decode_json_columns(dict(row)) if row else None

    async def finish_claimed_termination(
        self,
        call_id: str,
        *,
        expected_reason: str,
        terminal_state: CallState,
        ended_at: str,
        duration_seconds: int,
        expected_transfer_outcome: str | None = None,
    ) -> bool:
        params: list[Any] = [
            terminal_state.value,
            ended_at,
            duration_seconds,
            _iso_now(),
            call_id,
            expected_reason,
        ]
        if expected_transfer_outcome is None:
            transfer_predicate = " AND transfer_outcome IS NULL"
        else:
            transfer_predicate = " AND transfer_outcome=?"
            params.append(expected_transfer_outcome)
        return await self._execute_cas(
            f"""UPDATE calls
                SET state=?, ended_at=?, duration_seconds=?, last_event_at=?
                WHERE call_id=?
                  AND state='terminating'
                  AND termination_claimed=1
                  AND termination_reason=?{transfer_predicate}""",  # noqa: S608
            params,
        )

    async def reset_termination_claim(self, call_id: str) -> None:
        placeholders, terminal_states = self._in_clause(state.value for state in TERMINAL_STATES)
        await self.execute(
            f"""UPDATE calls SET termination_claimed=0
               WHERE call_id=? AND state NOT IN ({placeholders})""",  # noqa: S608
            (call_id, *terminal_states),
        )
