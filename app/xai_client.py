from __future__ import annotations

import httpx
from openai import AsyncOpenAI

from app.settings import Settings


def create_xai_client(settings: Settings) -> AsyncOpenAI:
    """Build an xAI client through its OpenAI-compatible API with bounded requests.

    The SDK owns and closes a custom HTTPX client passed through ``http_client``.
    Use the configured keepalive expiry (60 seconds by default) so sporadic calls
    can reuse a measured-warm TLS connection. ``None`` retains the SDK pool policy.
    """
    timeout = httpx.Timeout(
        settings.xai_http_timeout_seconds,
        connect=settings.xai_connect_timeout_seconds,
    )
    options: dict[str, object] = {
        "api_key": Settings.reveal(settings.xai_api_key),
        "base_url": "https://api.x.ai/v1",
        "timeout": timeout,
        "max_retries": 0,
    }
    if settings.xai_keepalive_expiry_seconds is not None:
        options["http_client"] = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=1000,
                max_keepalive_connections=100,
                keepalive_expiry=settings.xai_keepalive_expiry_seconds,
            ),
        )
    return AsyncOpenAI(**options)
