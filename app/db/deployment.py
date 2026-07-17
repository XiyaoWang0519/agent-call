from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.protocols import DatabaseAccess

from datetime import UTC, datetime, timedelta

from app.db.engine import _iso_now
from app.models import TERMINAL_STATES

DEPLOYMENT_LOCK_TTL = timedelta(minutes=15)


class DeploymentLockedError(RuntimeError):
    """Raised when a deployment lease temporarily blocks new phone calls."""


def _lock_is_active(locked_at: str | None) -> bool:
    """Whether a deployment lock timestamp is still within the TTL window."""

    if not locked_at:
        return False
    return datetime.now(UTC) - datetime.fromisoformat(locked_at) < DEPLOYMENT_LOCK_TTL


class DeploymentMixin:
    async def acquire_deployment_lock(self: DatabaseAccess) -> int:
        """Atomically block new calls if no call is currently nonterminal.

        Returns the number of active calls that prevented acquisition. Zero means
        the deployment lock was acquired.
        """

        placeholders, params = self._in_clause(state.value for state in TERMINAL_STATES)
        async with self._write_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                f"SELECT COUNT(*) FROM calls WHERE state NOT IN ({placeholders})",  # noqa: S608
                params,
            )
            count_row = await cursor.fetchone()
            active_calls = int(count_row[0]) if count_row is not None else 0
            if active_calls:
                await conn.rollback()
                return active_calls
            await conn.execute(
                """UPDATE deployment_control
                   SET locked=1, locked_at=?
                   WHERE singleton=1""",
                (_iso_now(),),
            )
            await conn.commit()
            return 0

    async def release_deployment_lock(self: DatabaseAccess) -> None:
        await self.execute(
            "UPDATE deployment_control SET locked=0, locked_at=NULL WHERE singleton=1"
        )

    async def deployment_lock_is_active(self: DatabaseAccess) -> bool:
        row = await self.fetch_one(
            "SELECT locked, locked_at FROM deployment_control WHERE singleton=1"
        )
        if not row or not row["locked"]:
            return False
        return _lock_is_active(row["locked_at"])
