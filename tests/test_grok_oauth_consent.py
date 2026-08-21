from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.grok_oauth.consent import consent_form
from app.grok_oauth.constants import FAILED_ATTEMPT_LIMIT, GROK_OAUTH_CONSENT_PATH
from app.grok_oauth.crypto import verify_owner_secret
from app.grok_oauth.provider import grok_mcp_resource
from app.main import create_app
from tests.conftest import GROK_OAUTH_OWNER_SECRET, GROK_OAUTH_OWNER_SECRET_HASH
from tests.oauth_helpers import (
    CSRF_RE,
    TX_RE,
    pkce_pair,
    register_test_client,
    start_authorization,
    submit_consent,
)

_DETAIL_RE = re.compile(r'<dt>([^<]+)</dt><dd(?: class="mono")?>(.*?)</dd>')


def _consent_details(page: str) -> dict[str, str]:
    return {html.unescape(label): html.unescape(value) for label, value in _DETAIL_RE.findall(page)}


def _begin_consent(client: TestClient, oauth_settings):
    registered = register_test_client(client)
    _, challenge = pkce_pair()
    authorize = start_authorization(
        client,
        client_id=registered["client_id"],
        redirect_uri="https://grok.example/callback",
        challenge=challenge,
        resource=grok_mcp_resource(oauth_settings.public_base_url or ""),
    )
    page = client.get(authorize.headers["location"])
    return registered, page


def test_correct_owner_secret_succeeds_and_argon2id_is_used(oauth_settings):
    assert verify_owner_secret(
        secret=GROK_OAUTH_OWNER_SECRET, secret_hash=GROK_OAUTH_OWNER_SECRET_HASH
    )
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        registered, page = _begin_consent(client, oauth_settings)
        assert page.status_code == 200
        details = _consent_details(page.text)
        assert "Unverified client" in page.text
        assert "not verified" in page.text
        assert details["Displayed name"] == "Grok Test Connector"
        assert details["Client ID"] == registered["client_id"]
        assert details["Redirect URI"] == "https://grok.example/callback"
        assert details["Redirect origin"] == "https://grok.example"
        assert details["Scope"] == "agent-call:use"
        assert details["Resource"] == grok_mcp_resource(oauth_settings.public_base_url or "")
        assert page.headers["cache-control"].startswith("no-store")
        assert page.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert GROK_OAUTH_OWNER_SECRET not in page.text
        approved = submit_consent(client, html=page.text)
        assert approved.status_code == 302
        location = approved.headers["location"]
        assert "code=" in location
        assert GROK_OAUTH_OWNER_SECRET not in location
        assert "set-cookie" not in {key.lower() for key in approved.headers}


def test_incorrect_owner_secret_fails_generically(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        _, page = _begin_consent(client, oauth_settings)
        failed = submit_consent(client, html=page.text, owner_secret="wrong-secret-value")
        assert failed.status_code == 401
        assert "Authorization failed." in failed.text
        assert "wrong-secret-value" not in failed.text


def test_csrf_expired_and_reused_transactions_are_rejected(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        _, page = _begin_consent(client, oauth_settings)
        csrf = CSRF_RE.search(page.text)
        tx = TX_RE.search(page.text)
        assert csrf and tx
        bad_csrf = submit_consent(
            client,
            html=page.text,
            csrf_token="tampered-csrf",
        )
        assert bad_csrf.status_code == 400

        approved = submit_consent(client, html=page.text)
        assert approved.status_code == 302
        reused = submit_consent(client, html=page.text)
        assert reused.status_code == 400

        registered = register_test_client(client)
        _, challenge = pkce_pair()
        authorize = start_authorization(
            client,
            client_id=registered["client_id"],
            redirect_uri="https://grok.example/callback",
            challenge=challenge,
            resource=grok_mcp_resource(oauth_settings.public_base_url or ""),
        )
        expired_page = client.get(authorize.headers["location"])
        tx_id = TX_RE.search(expired_page.text)
        assert tx_id
        import sqlite3

        past = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        conn = sqlite3.connect(oauth_settings.database_path)
        conn.execute(
            "UPDATE oauth_auth_transactions SET expires_at = ? WHERE transaction_id = ?",
            (past, tx_id.group(1)),
        )
        conn.commit()
        conn.close()
        expired = submit_consent(client, html=expired_page.text)
        assert expired.status_code == 400


def test_rate_limiting_and_request_body_size(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        _, page = _begin_consent(client, oauth_settings)
        for _ in range(FAILED_ATTEMPT_LIMIT):
            failed = submit_consent(client, html=page.text, owner_secret="nope")
            assert failed.status_code == 401
        blocked = submit_consent(client, html=page.text, owner_secret="nope")
        assert blocked.status_code == 429

        huge = client.post(
            GROK_OAUTH_CONSENT_PATH,
            content=b"x" * (9 * 1024),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert huge.status_code == 413


def test_interleaved_wrong_secrets_and_denies_cannot_reset_limiter(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        for _ in range(FAILED_ATTEMPT_LIMIT - 1):
            _, wrong_page = _begin_consent(client, oauth_settings)
            failed = submit_consent(client, html=wrong_page.text, owner_secret="nope")
            assert failed.status_code == 401
            _, deny_page = _begin_consent(client, oauth_settings)
            denied = submit_consent(client, html=deny_page.text, action="deny")
            assert denied.status_code == 302
            assert "error=access_denied" in denied.headers["location"]
        _, wrong_page = _begin_consent(client, oauth_settings)
        failed = submit_consent(client, html=wrong_page.text, owner_secret="nope")
        assert failed.status_code == 401
        _, deny_page = _begin_consent(client, oauth_settings)
        denied = submit_consent(client, html=deny_page.text, action="deny")
        assert denied.status_code == 429
        _, page = _begin_consent(client, oauth_settings)
        blocked = submit_consent(client, html=page.text, owner_secret="nope")
        assert blocked.status_code == 429


def test_invalid_transaction_posts_do_not_lock_out_owner(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        _, page = _begin_consent(client, oauth_settings)
        for _ in range(FAILED_ATTEMPT_LIMIT):
            bogus = submit_consent(
                client,
                html=page.text,
                tx="bogus-transaction",
                csrf_token="bogus-csrf",
                owner_secret="nope",
            )
            assert bogus.status_code == 400
        for _ in range(FAILED_ATTEMPT_LIMIT):
            bad_csrf = submit_consent(
                client,
                html=page.text,
                csrf_token="tampered-csrf",
                owner_secret="nope",
            )
            assert bad_csrf.status_code == 400
        approved = submit_consent(client, html=page.text)
        assert approved.status_code == 302


def test_owner_secret_limiter_is_account_wide_across_client_ips(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        for index in range(FAILED_ATTEMPT_LIMIT):
            _, page = _begin_consent(client, oauth_settings)
            failed = submit_consent(
                client,
                html=page.text,
                owner_secret="nope",
                headers={"fly-client-ip": f"203.0.113.{index + 1}"},
            )
            assert failed.status_code == 401
        _, page = _begin_consent(client, oauth_settings)
        blocked = submit_consent(
            client,
            html=page.text,
            owner_secret="nope",
            headers={"fly-client-ip": "198.51.100.9"},
        )
        assert blocked.status_code == 429


def test_consent_form_escapes_untrusted_identity_fields():
    evil_name = '<script>alert("xss")</script>'
    evil_id = "abc<>&\"'id"
    evil_redirect = 'https://attacker.example/cb?q=<script>alert(1)</script>&next=">'
    page = consent_form(
        transaction={
            "client_name": evil_name,
            "client_id": evil_id,
            "redirect_uri": evil_redirect,
            "resource": "https://example.test/grok/mcp/",
            "scopes": ["agent-call:use"],
            "csrf_token": "csrf-token",
            "transaction_id": "tx-1",
        }
    )
    details = _consent_details(page)
    assert "Unverified client" in page
    assert "<script>" not in page
    assert details["Displayed name"] == evil_name
    assert details["Client ID"] == evil_id
    assert details["Redirect URI"] == evil_redirect
    assert details["Redirect origin"] == "https://attacker.example"
    assert 'name="owner_secret"' in page


def test_consent_page_escapes_untrusted_client_identity(oauth_settings):
    evil_name = '<script>alert("xss")</script>'
    redirect_uri = "https://attacker.example/cb"
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        registered = register_test_client(
            client,
            redirect_uri=redirect_uri,
            client_name=evil_name,
        )
        _, challenge = pkce_pair()
        authorize = start_authorization(
            client,
            client_id=registered["client_id"],
            redirect_uri=redirect_uri,
            challenge=challenge,
            resource=grok_mcp_resource(oauth_settings.public_base_url or ""),
        )
        assert authorize.status_code == 302
        page = client.get(authorize.headers["location"])
        assert page.status_code == 200
        details = _consent_details(page.text)
        assert "Unverified client" in page.text
        assert details["Client ID"] == registered["client_id"]
        assert details["Redirect URI"] == redirect_uri
        assert details["Redirect origin"] == "https://attacker.example"
        assert "<script>" not in page.text
        assert html.escape(evil_name) in page.text
        assert 'name="owner_secret"' in page.text
        approved = submit_consent(client, html=page.text)
        assert approved.status_code == 302
        assert approved.headers["location"].startswith(redirect_uri)
        assert "code=" in approved.headers["location"]


def test_owner_secret_never_appears_in_redirects_cookies_or_logs(oauth_settings, caplog):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        with caplog.at_level("DEBUG"):
            _, page = _begin_consent(client, oauth_settings)
            approved = submit_consent(client, html=page.text)
        location = approved.headers.get("location", "")
        assert GROK_OAUTH_OWNER_SECRET not in location
        assert GROK_OAUTH_OWNER_SECRET not in page.text
        assert GROK_OAUTH_OWNER_SECRET not in caplog.text
        query = parse_qs(urlparse(location).query)
        assert query.get("code")
        assert GROK_OAUTH_OWNER_SECRET not in str(query)
