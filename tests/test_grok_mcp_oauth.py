from __future__ import annotations

import respx
from fastapi.testclient import TestClient

from app.grok_oauth.constants import GROK_MCP_PATH
from app.main import create_app
from tests.oauth_helpers import (
    EXPECTED_TOOLS,
    complete_owner_login,
    mcp_headers,
    parse_mcp_payload,
    register_test_client,
)
from tests.test_grok_bot_mcp import _prepare_arguments, _tool_payload


@respx.mock
def test_oauth_mcp_lists_seven_tools_and_prepares_without_provider_calls(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        registered = register_test_client(client)
        tokens = complete_owner_login(client, registered=registered, settings=oauth_settings)
        headers = mcp_headers(tokens["access_token"])
        initialize = client.post(
            GROK_MCP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "grok-bot-oauth", "version": "0"},
                },
            },
            headers=headers,
        )
        assert initialize.status_code == 200, initialize.text
        init_payload = parse_mcp_payload(initialize)
        assert "serverInfo" in init_payload["result"]
        session_headers = mcp_headers(
            tokens["access_token"], initialize.headers.get("mcp-session-id")
        )
        client.post(
            GROK_MCP_PATH,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session_headers,
        )
        listed = client.post(
            GROK_MCP_PATH,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=session_headers,
        )
        assert listed.status_code == 200, listed.text
        tools = parse_mcp_payload(listed)["result"]["tools"]
        assert {tool["name"] for tool in tools} == EXPECTED_TOOLS

        prepared = client.post(
            GROK_MCP_PATH,
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
        assert prepared.status_code == 200, prepared.text
        payload = _tool_payload(prepared)
        assert payload.get("plan_id", "").startswith("plan_")
        assert payload.get("confirmation_summary")
    assert respx.calls.call_count == 0


def test_legacy_mcp_still_requires_dual_headers_when_oauth_is_enabled(oauth_settings):
    app = create_app(oauth_settings)
    payload = {"jsonrpc": "2.0", "id": 9, "method": "tools/list"}
    with TestClient(app) as client:
        ok = client.post(
            "/mcp/",
            json=payload,
            headers={
                "Authorization": "Bearer mcp-test",
                "X-Agent-User-Id": "agent-user-1",
                "Accept": "application/json, text/event-stream",
            },
        )
        missing_user = client.post(
            "/mcp/",
            json=payload,
            headers={
                "Authorization": "Bearer mcp-test",
                "Accept": "application/json, text/event-stream",
            },
        )
        oauth_on_legacy = client.post(
            "/mcp/",
            json=payload,
            headers={
                "Authorization": "Bearer unused",
                "Accept": "application/json, text/event-stream",
            },
        )
    assert ok.status_code == 200
    assert {tool["name"] for tool in ok.json()["result"]["tools"]} == EXPECTED_TOOLS
    assert missing_user.status_code == 401
    assert oauth_on_legacy.status_code == 401
