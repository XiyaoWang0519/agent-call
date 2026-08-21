from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")
_NAMED_SECRET = re.compile(
    r"(?i)\b(authorization|api[_ -]?key|token|secret|owner_secret|code_verifier|"
    r"refresh_token|access_token|client_secret)\s*[:=]\s*\S+"
)
_TOKEN_LIKE_VALUE = re.compile(
    r"\b(?:sk|whsec)[-_A-Za-z0-9]{12,}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
    re.IGNORECASE,
)


def sanitize_log_text(value: object, *, max_length: int = 240) -> str | None:
    if value is None:
        return None
    text = _CONTROL_CHARACTERS.sub(" ", str(value)).strip()
    text = _NAMED_SECRET.sub(r"\1=[redacted]", text)
    text = _TOKEN_LIKE_VALUE.sub("[redacted]", text)
    if len(text) > max_length:
        return f"{text[: max_length - 3]}..."
    return text


def provider_error_fields(value: object) -> tuple[str | None, str | None, str | None]:
    if not isinstance(value, Mapping):
        return None, None, None
    nested: Any = value.get("error")
    error = nested if isinstance(nested, Mapping) else value
    return (
        sanitize_log_text(error.get("code"), max_length=80),
        sanitize_log_text(error.get("type"), max_length=80),
        sanitize_log_text(error.get("message")),
    )


def request_id_from_headers(headers: object) -> str | None:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    for name in ("x-request-id", "request-id", "openai-request-id"):
        value = getter(name)
        if value:
            return sanitize_log_text(value, max_length=100)
    return None
