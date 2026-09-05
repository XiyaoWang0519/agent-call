from __future__ import annotations

from typing import Any

import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.smoke_prepare import EXPECTED_TOOLS, parse_mcp_payload, tool_payload

GROK_MCP_HEADERS = {
    "Authorization": "Bearer mcp-test",
    "X-Agent-User-Id": "agent-user-1",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _parse_mcp_payload(response) -> dict[str, Any]:
    try:
        return parse_mcp_payload(response.text)
    except ValueError as exc:
        raise AssertionError(str(exc)) from exc


def _mcp_session_headers(response) -> dict[str, str]:
    headers = dict(GROK_MCP_HEADERS)
    session_id = response.headers.get("mcp-session-id")
    if session_id:
        headers["mcp-session-id"] = session_id
    return headers


def _prepare_arguments() -> dict[str, Any]:
    return {
        "context": {
            "owner": {
                "display_name": "the owner",
                "timezone": "America/Los_Angeles",
                "callback_number": "+14155550101",
            },
            "target": {"name": "Grok Bot Target", "phone": "+14155550100"},
            "objective": "Ask the callee to say nonce AGENT-4821 and acknowledge it.",
            "escalation": {"mode": "end_call", "owner_phone": "+14155550101"},
        },
        "authority_basis": "Owner requested this Grok Bot connection test",
        "requested_by_owner": True,
    }


def _tool_payload(response) -> dict[str, Any]:
    result = _parse_mcp_payload(response).get("result") or {}
    try:
        return tool_payload(result)
    except ValueError as exc:
        raise AssertionError(str(exc)) from exc


@respx.mock
def test_grok_compatible_mcp_client_prepares_without_provider_calls(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        initialize = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "grok-bot-compatible", "version": "0"},
                },
            },
            headers=GROK_MCP_HEADERS,
        )
        assert initialize.status_code == 200
        init_payload = _parse_mcp_payload(initialize)
        assert "serverInfo" in init_payload["result"]

        session_headers = _mcp_session_headers(initialize)
        client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session_headers,
        )

        listed = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=session_headers,
        )
        assert listed.status_code == 200
        tools = _parse_mcp_payload(listed)["result"]["tools"]
        assert {tool["name"] for tool in tools} == EXPECTED_TOOLS

        prepared = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "prepare_phone_call",
                    "arguments": _prepare_arguments(),
                },
            },
            headers=session_headers,
        )
        assert prepared.status_code == 200
        payload = _tool_payload(prepared)
        assert payload.get("plan_id")
        assert payload["plan_id"].startswith("plan_")
        assert payload.get("confirmation_summary")
        assert payload.get("missing_fields") in (None, [])

    assert respx.calls.call_count == 0


@respx.mock
def test_grok_compatible_mcp_client_rejects_either_missing_credential(settings):
    app = create_app(settings)
    payload = {"jsonrpc": "2.0", "id": 9, "method": "tools/list"}
    with TestClient(app) as client:
        missing_bearer = client.post(
            "/mcp/",
            json=payload,
            headers={
                "X-Agent-User-Id": "agent-user-1",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        missing_user = client.post(
            "/mcp/",
            json=payload,
            headers={
                "Authorization": "Bearer mcp-test",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
    assert missing_bearer.status_code == 401
    assert missing_user.status_code == 401
    assert respx.calls.call_count == 0
