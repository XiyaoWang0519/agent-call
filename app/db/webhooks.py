from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.protocols import DatabaseAccess

import aiosqlite

from app.db.engine import _iso_now


class WebhooksMixin:
    async def record_webhook_once(self: DatabaseAccess, webhook_id: str) -> bool:
        try:
            return await self._execute_cas(
                "INSERT INTO webhook_deliveries(webhook_id, received_at) VALUES (?, ?)",
                (webhook_id, _iso_now()),
            )
        except aiosqlite.IntegrityError:
            return False
