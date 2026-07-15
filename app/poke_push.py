from __future__ import annotations

import logging
from typing import Any

import httpx

from app.settings import Settings

logger = logging.getLogger(__name__)

POKE_INBOUND_URL = "https://poke.com/api/v1/inbound/api-message"


async def push_message_to_poke(settings: Settings, message: Any) -> None:
    """Best-effort POST to Poke's inbound API. Failures are swallowed."""

    if not settings.poke_push_enabled or settings.poke_api_key is None:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                POKE_INBOUND_URL,
                headers={"Authorization": f"Bearer {Settings.reveal(settings.poke_api_key)}"},
                json={"message": message},
            )
            response.raise_for_status()
    except Exception:
        logger.warning("optional Poke push failed", exc_info=True)
