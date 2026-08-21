from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.grok_oauth.constants import GROK_OAUTH_SCOPE
from app.grok_oauth.provider import grok_mcp_resource
from tests.conftest import GROK_OAUTH_OWNER_SECRET

EXPECTED_TOOLS = {
    "prepare_phone_call",
    "start_phone_call",
    "get_call_result",
    "end_phone_call",
    "get_phone_call",
    "wait_for_call_event",
    "answer_call_question",
}

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
TX_RE = re.compile(r'name="tx" value="([^"]+)"')


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def parse_mcp_payload(response) -> dict[str, Any]:
    body = response.text.strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                return json.loads(payload)
    raise AssertionError(f"MCP response was not JSON: {body[:500]}")


def register_test_client(
    client: TestClient,
    *,
    redirect_uri: str = "https://grok.example/callback",
    client_name: str = "Grok Test Connector",
    scope: str = GROK_OAUTH_SCOPE,
) -> dict[str, Any]:
    response = client.post(
        "/register",
        json={
            "redirect_uris": [redirect_uri],
            "client_name": client_name,
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": scope,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def start_authorization(
    client: TestClient,
    *,
    client_id: str,
    redirect_uri: str,
    challenge: str,
    resource: str,
    state: str = "state-1",
    scope: str = GROK_OAUTH_SCOPE,
    extra: dict[str, str] | None = None,
):
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": scope,
        "resource": resource,
        "state": state,
    }
    if extra:
        params.update(extra)
    return client.get("/authorize", params=params, follow_redirects=False)


def submit_consent(
    client: TestClient,
    *,
    html: str,
    action: str = "approve",
    owner_secret: str = GROK_OAUTH_OWNER_SECRET,
    csrf_token: str | None = None,
    tx: str | None = None,
    headers: dict[str, str] | None = None,
):
    csrf = csrf_token or (CSRF_RE.search(html).group(1) if CSRF_RE.search(html) else "")
    transaction_id = tx or (TX_RE.search(html).group(1) if TX_RE.search(html) else "")
    return client.post(
        "/grok/oauth/consent",
        data={
            "tx": transaction_id,
            "csrf_token": csrf,
            "owner_secret": owner_secret,
            "action": action,
        },
        headers=headers,
        follow_redirects=False,
    )


def complete_owner_login(
    client: TestClient,
    *,
    registered: dict[str, Any],
    settings,
    verifier: str | None = None,
    challenge: str | None = None,
    redirect_uri: str = "https://grok.example/callback",
    resource: str | None = None,
) -> dict[str, Any]:
    if verifier is None or challenge is None:
        verifier, challenge = pkce_pair()
    resource = resource or grok_mcp_resource(settings.public_base_url or "")
    authorize = start_authorization(
        client,
        client_id=registered["client_id"],
        redirect_uri=redirect_uri,
        challenge=challenge,
        resource=resource,
    )
    assert authorize.status_code == 302, authorize.text
    consent_path = authorize.headers["location"]
    page = client.get(consent_path)
    assert page.status_code == 200, page.text
    approved = submit_consent(client, html=page.text)
    assert approved.status_code == 302, approved.text
    location = urlparse(approved.headers["location"])
    query = parse_qs(location.query)
    assert "code" in query
    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": redirect_uri,
            "client_id": registered["client_id"],
            "client_secret": registered["client_secret"],
            "code_verifier": verifier,
            "resource": resource,
        },
    )
    assert token.status_code == 200, token.text
    payload = token.json()
    payload["code"] = query["code"][0]
    payload["verifier"] = verifier
    payload["challenge"] = challenge
    payload["resource"] = resource
    payload["redirect_uri"] = redirect_uri
    payload["state"] = query.get("state", [None])[0]
    return payload


def mcp_headers(access_token: str, session_id: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    return headers
