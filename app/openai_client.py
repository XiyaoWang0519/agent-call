from __future__ import annotations

import httpx2
from openai import AsyncOpenAI, DefaultAsyncHttpx2Client

from app.settings import Settings


def create_openai_client(settings: Settings) -> AsyncOpenAI:
    """Build the shared OpenAI client with bounded control-plane requests.

    The SDK owns and closes a custom HTTPX2 client passed through ``http_client``.
    Use the configured keepalive expiry (60 seconds by default) so sporadic calls
    can reuse a measured-warm TLS connection. ``None`` retains the SDK pool policy.
    """
    timeout = httpx2.Timeout(
        settings.openai_http_timeout_seconds,
        connect=settings.openai_connect_timeout_seconds,
    )
    http_client: httpx2.AsyncClient | None = None
    if settings.openai_keepalive_expiry_seconds is not None:
        http_client = DefaultAsyncHttpx2Client(
            timeout=timeout,
            follow_redirects=True,
            limits=httpx2.Limits(
                max_connections=1000,
                max_keepalive_connections=100,
                keepalive_expiry=settings.openai_keepalive_expiry_seconds,
            ),
        )
    if http_client is None:
        return AsyncOpenAI(
            api_key=Settings.reveal(settings.openai_api_key),
            webhook_secret=Settings.reveal(settings.openai_webhook_secret),
            timeout=timeout,
            max_retries=0,
        )
    return AsyncOpenAI(
        api_key=Settings.reveal(settings.openai_api_key),
        webhook_secret=Settings.reveal(settings.openai_webhook_secret),
        timeout=timeout,
        max_retries=0,
        http_client=http_client,
    )
