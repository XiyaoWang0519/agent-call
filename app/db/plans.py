from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.protocols import DatabaseAccess

import json
from datetime import datetime
from typing import Any

from app.db.deployment import DeploymentLockedError, _lock_is_active
from app.db.engine import _iso_now
from app.models import CallState


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
    ) -> bool:
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
                return False
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
            return True
