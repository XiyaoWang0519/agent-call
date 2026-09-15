from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.protocols import DatabaseAccess

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.db.deployment import DeploymentLockedError, _lock_is_active
from app.db.engine import _iso_now
from app.models import TERMINAL_STATES, CallState


class ClaimOutcome(StrEnum):
    """Why a confirmed plan was or was not turned into a call.

    ``busy``/``call_ending`` are distinct from ``plan_unavailable`` so callers can
    return a stable error code instead of reporting an unconsumed plan as expired.
    """

    CLAIMED = "claimed"
    PLAN_UNAVAILABLE = "plan_unavailable"
    BUSY = "call_busy"
    CALL_ENDING = "call_ending"


@dataclass(frozen=True, slots=True)
class ClaimResult:
    outcome: ClaimOutcome

    @property
    def claimed(self) -> bool:
        return self.outcome is ClaimOutcome.CLAIMED

    def __bool__(self) -> bool:
        return self.claimed


class PlansMixin:
    async def create_plan(
        self: DatabaseAccess,
        plan_id: str,
        context: dict[str, Any],
        authority_basis: str | None,
        expires_at: datetime,
    ) -> None:
        now = _iso_now()
        await self.execute(
            """INSERT INTO plans
               (plan_id, state, context_json, authority_basis, created_at, expires_at)
               VALUES (?, 'prepared', ?, ?, ?, ?)""",
            (plan_id, json.dumps(context), authority_basis, now, expires_at.isoformat()),
        )

    async def get_plan(self: DatabaseAccess, plan_id: str) -> dict[str, Any] | None:
        return await self.fetch_one("SELECT * FROM plans WHERE plan_id = ?", (plan_id,))

    async def claim_plan_and_create_call(
        self: DatabaseAccess,
        *,
        plan_id: str,
        call_id: str,
        conference_name: str,
        confirmation_text: str,
        enforce_single_call_capacity: bool = True,
    ) -> ClaimResult:
        """Atomically consume a plan and create its call, or explain why not.

        Deployment-lock check, single-use plan consumption, single-live-call
        capacity, and the ``calls`` INSERT all run in one ``BEGIN IMMEDIATE``
        transaction. A rejected claim leaves the plan ``prepared`` (rollback), so a
        transient ``busy``/``call_ending`` does not burn the confirmation.

        Capacity is checked only when ``enforce_single_call_capacity`` is true; the
        production entry point leaves it on, while multi-call historical fixtures
        that seed stranded rows explicitly opt out. This claim is a local admission
        decision, not a distributed exactly-once guarantee: a remote participant is
        not created until the caller observes ``CLAIMED``.
        """
        now = _iso_now()
        async with self._write_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT locked, locked_at FROM deployment_control WHERE singleton=1"
            )
            lock = await cursor.fetchone()
            if lock and lock["locked"]:
                if _lock_is_active(lock["locked_at"]):
                    await conn.rollback()
                    raise DeploymentLockedError("deployment is in progress")
                await conn.execute(
                    "UPDATE deployment_control SET locked=0, locked_at=NULL WHERE singleton=1"
                )
            cursor = await conn.execute(
                """UPDATE plans SET state='started', call_id=?, confirmation_text=?
                   WHERE plan_id=? AND state='prepared' AND expires_at>?""",
                (call_id, confirmation_text, plan_id, now),
            )
            if cursor.rowcount != 1:
                await conn.rollback()
                return ClaimResult(ClaimOutcome.PLAN_UNAVAILABLE)
            if enforce_single_call_capacity:
                placeholders, terminal_params = self._in_clause(
                    state.value for state in TERMINAL_STATES
                )
                cursor = await conn.execute(
                    f"SELECT state FROM calls WHERE state NOT IN ({placeholders}) LIMIT 1",  # noqa: S608
                    terminal_params,
                )
                active = await cursor.fetchone()
                if active is not None:
                    await conn.rollback()
                    if CallState(active["state"]) is CallState.TERMINATING:
                        return ClaimResult(ClaimOutcome.CALL_ENDING)
                    return ClaimResult(ClaimOutcome.BUSY)
            await conn.execute(
                """INSERT INTO calls
                   (call_id, plan_id, state, conference_name, last_event_at, created_at, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    call_id,
                    plan_id,
                    CallState.PREWARMING.value,
                    conference_name,
                    now,
                    now,
                    now,
                ),
            )
            await conn.commit()
            return ClaimResult(ClaimOutcome.CLAIMED)
