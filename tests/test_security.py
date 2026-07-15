from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app.main import create_app
from app.settings import Settings
from tests.conftest import seed_call


def _openai_headers(settings, body: bytes, webhook_id: str = "wh_test") -> dict[str, str]:
    timestamp = str(int(time.time()))
    secret = Settings.reveal(settings.openai_webhook_secret)
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{webhook_id}.{timestamp}.".encode() + body
    signature = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {
        "webhook-id": webhook_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": f"v1,{signature}",
        "content-type": "application/json",
    }


def test_mcp_rejects_bad_bearer_and_wrong_user(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        bad_bearer = client.post(
            "/mcp/",
            json=payload,
            headers={"Authorization": "Bearer wrong", "X-Poke-User-Id": "poke-user-1"},
        )
        wrong_user = client.post(
            "/mcp/",
            json=payload,
            headers={"Authorization": "Bearer mcp-test", "X-Poke-User-Id": "wrong"},
        )
    assert bad_bearer.status_code == 401
    assert wrong_user.status_code == 401


def test_authorized_mcp_endpoint_exposes_exact_tool_set(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Authorization": "Bearer mcp-test",
                "X-Poke-User-Id": "poke-user-1",
                "Accept": "application/json, text/event-stream",
            },
        )
    assert response.status_code == 200
    assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
        "prepare_phone_call",
        "start_phone_call",
        "get_call_result",
        "end_phone_call",
        "get_phone_call",
    }


def test_mcp_allows_poke_followup_without_user_header(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Authorization": "Bearer mcp-test",
                "Accept": "application/json, text/event-stream",
            },
        )
    assert response.status_code == 200


def test_blocked_destination_is_structured_mcp_error_without_call(settings, packet):
    app = create_app(settings)
    context = packet.model_dump(mode="json")
    context["target"]["phone"] = "+19005550123"
    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "prepare_phone_call",
                    "arguments": {
                        "context": context,
                        "authority_basis": "Owner requested the call",
                        "requested_by_owner": True,
                    },
                },
            },
            headers={
                "Authorization": "Bearer mcp-test",
                "X-Poke-User-Id": "poke-user-1",
                "Accept": "application/json, text/event-stream",
            },
        )
        calls = client.portal.call(app.state.call_service.db.list_calls)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    error = json.loads(result["content"][0]["text"])
    assert error["code"] == "premium_rate"
    assert calls == []


def test_debug_routes_require_token(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/calls").status_code == 401
        assert (
            client.get("/calls", headers={"Authorization": "Bearer debug-test"}).status_code == 200
        )


def test_deployment_lock_requires_its_narrow_token(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        unauthorized = client.post("/internal/deployment-lock")
        wrong_token = client.post(
            "/internal/deployment-lock",
            headers={"Authorization": "Bearer debug-test"},
        )
        acquired = client.post(
            "/internal/deployment-lock",
            headers={"Authorization": "Bearer deploy-guard-test"},
        )
        released = client.delete(
            "/internal/deployment-lock",
            headers={"Authorization": "Bearer deploy-guard-test"},
        )

    assert unauthorized.status_code == 401
    assert wrong_token.status_code == 401
    assert acquired.status_code == 200
    assert acquired.json() == {"ready": True, "active_calls": 0}
    assert released.status_code == 200
    assert released.json() == {"released": True}


def test_unsigned_and_invalid_openai_webhooks_rejected(settings):
    app = create_app(settings)
    body = json.dumps(
        {
            "object": "event",
            "id": "evt_test",
            "type": "response.completed",
            "created_at": int(time.time()),
            "data": {"id": "resp_test"},
        }
    ).encode()
    with TestClient(app) as client:
        unsigned = client.post("/webhooks/openai", content=body)
        invalid = client.post(
            "/webhooks/openai",
            content=body,
            headers={
                "webhook-id": "wh_invalid",
                "webhook-timestamp": str(int(time.time())),
                "webhook-signature": "v1,invalid",
                "content-type": "application/json",
            },
        )
    assert unsigned.status_code == 400
    assert invalid.status_code == 400


def test_openai_webhook_replay_is_rejected(settings):
    app = create_app(settings)
    body = json.dumps(
        {
            "object": "event",
            "id": "evt_completed",
            "type": "response.completed",
            "created_at": int(time.time()),
            "data": {"id": "resp_test"},
        },
        separators=(",", ":"),
    ).encode()
    headers = _openai_headers(settings, body, "wh_replay")
    with TestClient(app) as client:
        first = client.post("/webhooks/openai", content=body, headers=headers)
        second = client.post("/webhooks/openai", content=body, headers=headers)
    assert first.status_code == 204
    assert second.status_code == 400


def test_valid_openai_incoming_webhook_reaches_call_service(settings):
    app = create_app(settings)
    body = json.dumps(
        {
            "object": "event",
            "id": "evt_incoming",
            "type": "realtime.call.incoming",
            "created_at": int(time.time()),
            "data": {
                "call_id": "rtc_incoming",
                "sip_headers": [
                    {"name": "X-Plan-Id", "value": "plan_1"},
                    {"name": "X-Bridge-Call-Id", "value": "call_1"},
                ],
            },
        },
        separators=(",", ":"),
    ).encode()
    with TestClient(app) as client:
        handler = AsyncMock(return_value="call_1")
        app.state.call_service.handle_openai_incoming = handler
        response = client.post(
            "/webhooks/openai",
            content=body,
            headers=_openai_headers(settings, body, "wh_incoming"),
        )
    assert response.status_code == 200
    handler.assert_awaited_once_with(
        "rtc_incoming",
        [
            {"name": "X-Plan-Id", "value": "plan_1"},
            {"name": "X-Bridge-Call-Id", "value": "call_1"},
        ],
    )


def test_twilio_callbacks_reject_missing_or_invalid_signature(settings):
    app = create_app(settings)
    path = "/webhooks/twilio/amd?call_id=call_none&plan_id=plan_none"
    with TestClient(app) as client:
        missing = client.post(path, data={"AnsweredBy": "human"})
        invalid = client.post(
            path,
            data={"AnsweredBy": "human"},
            headers={"X-Twilio-Signature": "invalid"},
        )
    assert missing.status_code == 403
    assert invalid.status_code == 403


def test_signed_twilio_callback_requires_matching_plan_mapping(settings, packet):
    app = create_app(settings)
    params = {"AnsweredBy": "human"}
    validator = RequestValidator(Settings.reveal(settings.twilio_auth_token))
    with TestClient(app) as client:
        client.portal.call(seed_call, app.state.call_service.db, packet)
        valid_path = "/webhooks/twilio/amd?call_id=call_test&plan_id=plan_call_test"
        valid_signature = validator.compute_signature(
            f"{settings.public_base_url}{valid_path}", params
        )
        valid = client.post(
            valid_path,
            data=params,
            headers={"X-Twilio-Signature": valid_signature},
        )

        wrong_path = "/webhooks/twilio/amd?call_id=call_test&plan_id=plan_wrong"
        wrong_signature = validator.compute_signature(
            f"{settings.public_base_url}{wrong_path}", params
        )
        wrong = client.post(
            wrong_path,
            data=params,
            headers={"X-Twilio-Signature": wrong_signature},
        )
    assert valid.status_code == 204
    assert wrong.status_code == 400


async def test_exactly_five_mcp_tools(settings):
    app = create_app(settings)
    tools = await app.state.mcp.list_tools()
    assert {tool.name for tool in tools} == {
        "prepare_phone_call",
        "start_phone_call",
        "get_call_result",
        "end_phone_call",
        "get_phone_call",
    }
