from __future__ import annotations

import httpx
from openai import AsyncOpenAI

from app.settings import Settings


def create_openai_client(settings: Settings) -> AsyncOpenAI:
    """Build the shared OpenAI client with bounded control-plane requests.

    The SDK owns and closes a custom HTTPX client passed through ``http_client``.
    Keep the SDK's normal connection-pool policy unless an explicit keepalive
    expiry is configured for a measured deployment experiment.
    """
    timeout = httpx.Timeout(
        settings.openai_http_timeout_seconds,
        connect=settings.openai_connect_timeout_seconds,
    )
    options: dict[str, object] = {
        "api_key": Settings.reveal(settings.openai_api_key),
        "webhook_secret": Settings.reveal(settings.openai_webhook_secret),
        "timeout": timeout,
        "max_retries": 0,
    }
    if settings.openai_keepalive_expiry_seconds is not None:
        options["http_client"] = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=1000,
                max_keepalive_connections=100,
                keepalive_expiry=settings.openai_keepalive_expiry_seconds,
            ),
        )
    return AsyncOpenAI(**options)
