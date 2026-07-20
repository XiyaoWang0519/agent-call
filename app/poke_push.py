from __future__ import annotations

import logging
from typing import Any

import httpx

from app.settings import Settings

logger = logging.getLogger(__name__)

POKE_INBOUND_URL = "https://poke.com/api/v1/inbound/api-message"


class _PokeHttpClient:
    """Lazily-created, process-lifetime pooled client for Poke pushes.

    Mirrors the shared-client pattern used by ``app.openai_client`` and
    ``app.exa_search`` instead of opening a fresh TCP+TLS connection per push.
    Guards against creating a new client after ``close`` has been called during
    shutdown (e.g. a push racing app teardown): once closed, pushes are
    silently skipped, consistent with this function's existing best-effort,
    failure-swallowing behavior.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    def get(self) -> httpx.AsyncClient | None:
        # Single-threaded event loop: this check-then-create is race-safe
        # because there is no `await` between the check and the assignment.
        if self._closed:
            return None
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5)
        return self._client

    async def close(self) -> None:
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()


_poke_http = _PokeHttpClient()


async def push_message_to_poke(settings: Settings, message: Any) -> None:
    """Best-effort POST to Poke's inbound API. Failures are swallowed."""

    if not settings.poke_push_enabled or settings.poke_api_key is None:
        return
    client = _poke_http.get()
    if client is None:
        return
    try:
        response = await client.post(
            POKE_INBOUND_URL,
            headers={"Authorization": f"Bearer {Settings.reveal(settings.poke_api_key)}"},
            json={"message": message},
        )
        response.raise_for_status()
    except Exception:
        logger.warning("optional Poke push failed", exc_info=True)


async def close_poke_http_client() -> None:
    """Close the shared Poke push client. Wired into the app lifespan teardown."""

    await _poke_http.close()
