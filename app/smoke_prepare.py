"""Prepare-only MCP smoke: initialize, list tools, prepare a plan, never start."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.evaluation import (
    EVALUATION_ALLOWED_AGENT_USER_ID,
    EVALUATION_MCP_BEARER,
    EVALUATION_OWNER_PHONE,
    EVALUATION_TARGET_PHONE,
    evaluation_prepare_arguments,
)
from app.settings import is_loopback_host

EXPECTED_TOOLS = frozenset(
    {
        "prepare_phone_call",
        "start_phone_call",
        "wait_for_call_event",
        "get_phone_call",
        "answer_call_question",
        "end_phone_call",
        "get_call_result",
    }
)

JsonObject = dict[str, Any]
McpPoster = Callable[..., Any]


class SmokeTargetError(ValueError):
    """Rejected smoke-prepare target; safe to print (no secrets)."""


def credentials_from_environ(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    source = os.environ if environ is None else environ
    bearer = source.get("MCP_BEARER_TOKEN") or EVALUATION_MCP_BEARER
    user_id = source.get("ALLOWED_AGENT_USER_ID") or EVALUATION_ALLOWED_AGENT_USER_ID
    return bearer, user_id


def validate_smoke_target(base_url: str, mcp_path: str) -> tuple[str, str]:
    """Return `(origin, mcp_path)` after rejecting credential-leaking targets.

    Call this before reading environment credentials or opening a client.
    """
    return _validate_base_url(base_url), _validate_mcp_path(mcp_path)


def _validate_base_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise SmokeTargetError("--base-url must be a valid HTTP(S) origin")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise SmokeTargetError("--base-url must be a valid HTTP(S) origin")
    if parsed.username is not None or parsed.password is not None:
        raise SmokeTargetError("--base-url must not include credentials")
    if not parsed.netloc or parsed.hostname is None:
        raise SmokeTargetError("--base-url must be a valid HTTP(S) origin")
    if parsed.path not in {"", "/"}:
        raise SmokeTargetError("--base-url must not include a path")
    if parsed.query:
        raise SmokeTargetError("--base-url must not include a query")
    if parsed.fragment:
        raise SmokeTargetError("--base-url must not include a fragment")
    if parsed.scheme == "http" and not is_loopback_host(parsed.hostname):
        raise SmokeTargetError("--base-url must use HTTPS for non-loopback hosts")
    return f"{parsed.scheme}://{parsed.netloc}"


def _validate_mcp_path(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise SmokeTargetError("--mcp-path must be an absolute path beginning with one slash")
    if raw.startswith("//") or "://" in raw:
        raise SmokeTargetError("--mcp-path must be a same-origin path, not a URL")
    if "\\" in raw or raw.startswith("/\\"):
        raise SmokeTargetError("--mcp-path must be a same-origin path, not a URL")
    if not raw.startswith("/") or raw.startswith("//"):
        raise SmokeTargetError("--mcp-path must be an absolute path beginning with one slash")
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        raise SmokeTargetError("--mcp-path must not include an authority")
    if parsed.username is not None or parsed.password is not None:
        raise SmokeTargetError("--mcp-path must not include credentials")
    if parsed.query:
        raise SmokeTargetError("--mcp-path must not include a query")
    if parsed.fragment:
        raise SmokeTargetError("--mcp-path must not include a fragment")
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        raise SmokeTargetError("--mcp-path must be an absolute path beginning with one slash")
    return parsed.path


@dataclass(slots=True)
class SmokeResult:
    ok: bool
    plan_id: str | None = None
    confirmation_summary: str | None = None
    session_id: str | None = None
    tools: tuple[str, ...] = ()
    invoked_tools: tuple[str, ...] = ()
    detail: str = ""
    requests: list[str] = field(default_factory=list)


def parse_mcp_payload(body: str) -> JsonObject:
    text = body.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                loaded = json.loads(payload)
                if isinstance(loaded, dict):
                    return loaded
    raise ValueError("MCP response was not JSON")


def tool_payload(result: Mapping[str, Any]) -> JsonObject:
    if result.get("isError") is True:
        raise ValueError(f"prepare_phone_call error: {result}")
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and (
        structured.get("plan_id") or "confirmation_summary" in structured
    ):
        return structured
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            parsed = json.loads(first["text"])
            if isinstance(parsed, dict):
                inner = parsed.get("result", parsed)
                if isinstance(inner, dict):
                    return inner
    raise ValueError("prepare_phone_call returned no structured plan")


def run_prepare_only_smoke(
    post: McpPoster,
    *,
    bearer: str = EVALUATION_MCP_BEARER,
    user_id: str = EVALUATION_ALLOWED_AGENT_USER_ID,
    owner_phone: str = EVALUATION_OWNER_PHONE,
    target_phone: str = EVALUATION_TARGET_PHONE,
    mcp_path: str = "/mcp/",
) -> SmokeResult:
    """Drive initialize → tools/list → prepare_phone_call. Never call start."""
    invoked: list[str] = []
    requests: list[str] = []
    headers: dict[str, str] = {
        "Authorization": f"Bearer {bearer}",
        "X-Agent-User-Id": user_id,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    def call(payload: JsonObject) -> tuple[Any, JsonObject]:
        method = str(payload.get("method") or "")
        requests.append(method)
        if method == "tools/call":
            params = payload.get("params")
            if isinstance(params, dict):
                invoked.append(str(params.get("name") or ""))
        response = post(mcp_path, json=payload, headers=headers)
        body = response.text
        if not body.strip():
            return response, {}
        parsed = parse_mcp_payload(body)
        session = response.headers.get("mcp-session-id")
        if session:
            headers["mcp-session-id"] = session
        return response, parsed

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "agent-call-smoke-prepare", "version": "0"},
        },
    }
    init_response, init_payload = call(initialize)
    if init_response.status_code != 200:
        return SmokeResult(ok=False, detail="initialize HTTP failed", requests=requests)
    if "serverInfo" not in (init_payload.get("result") or {}):
        return SmokeResult(ok=False, detail="initialize missing serverInfo", requests=requests)

    call({"jsonrpc": "2.0", "method": "notifications/initialized"})
    listed_response, listed_payload = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    if listed_response.status_code != 200:
        return SmokeResult(ok=False, detail="tools/list HTTP failed", requests=requests)
    tools_raw = (listed_payload.get("result") or {}).get("tools")
    if not isinstance(tools_raw, list):
        return SmokeResult(ok=False, detail="tools/list missing tools", requests=requests)
    names = tuple(sorted(str(tool.get("name")) for tool in tools_raw if isinstance(tool, dict)))
    if set(names) != EXPECTED_TOOLS:
        return SmokeResult(
            ok=False,
            tools=names,
            detail=f"expected {sorted(EXPECTED_TOOLS)}, got {list(names)}",
            requests=requests,
            invoked_tools=tuple(invoked),
        )

    prepare_payload = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "prepare_phone_call",
            "arguments": evaluation_prepare_arguments(
                owner_phone=owner_phone, target_phone=target_phone
            ),
        },
    }
    prepare_response, prepare_body = call(prepare_payload)
    if prepare_response.status_code != 200:
        return SmokeResult(
            ok=False,
            tools=names,
            invoked_tools=tuple(invoked),
            detail="prepare_phone_call HTTP failed",
            requests=requests,
        )
    try:
        prepared = tool_payload(prepare_body.get("result") or {})
    except ValueError as exc:
        return SmokeResult(
            ok=False,
            tools=names,
            invoked_tools=tuple(invoked),
            detail=str(exc),
            requests=requests,
        )
    plan_id = prepared.get("plan_id")
    confirmation = prepared.get("confirmation_summary")
    if not isinstance(plan_id, str) or not plan_id.startswith("plan_"):
        return SmokeResult(
            ok=False,
            tools=names,
            invoked_tools=tuple(invoked),
            detail="prepare_phone_call returned no persisted plan_id",
            requests=requests,
        )
    if "start_phone_call" in invoked:
        return SmokeResult(
            ok=False,
            plan_id=plan_id,
            tools=names,
            invoked_tools=tuple(invoked),
            detail="start_phone_call was invoked",
            requests=requests,
        )
    summary = confirmation if isinstance(confirmation, str) else None
    return SmokeResult(
        ok=True,
        plan_id=plan_id,
        confirmation_summary=summary,
        session_id=headers.get("mcp-session-id"),
        tools=names,
        invoked_tools=tuple(invoked),
        detail="prepare-only smoke passed",
        requests=requests,
    )
