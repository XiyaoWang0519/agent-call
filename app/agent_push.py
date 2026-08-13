from __future__ import annotations

import logging
from typing import Any

import httpx

from app.settings import Settings

logger = logging.getLogger(__name__)


async def push_message_to_agent(settings: Settings, message: Any) -> None:
    """Best-effort POST to an OpenClaw gateway /hooks/agent webhook. Failures are swallowed."""

    if (
        not settings.agent_push_enabled
        or settings.agent_webhook_url is None
        or settings.agent_webhook_token is None
    ):
        return
    # OpenClaw /hooks/agent expects a text message that wakes the agent.
    # Non-string payloads (e.g. structured mid-call questions) are stringified by the caller.
    text = message if isinstance(message, str) else str(message)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                settings.agent_webhook_url,
                headers={
                    "Authorization": f"Bearer {Settings.reveal(settings.agent_webhook_token)}"
                },
                json={"message": text, "name": "Agent Call", "wakeMode": "now"},
            )
            response.raise_for_status()
    except Exception:
        logger.warning("optional agent push failed", exc_info=True)
