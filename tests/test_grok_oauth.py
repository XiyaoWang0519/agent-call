from __future__ import annotations

import json
import runpy
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.grok_oauth.constants import GROK_MCP_PATH, GROK_OAUTH_CONSENT_PATH, GROK_OAUTH_SCOPE
from app.grok_oauth.provider import grok_mcp_resource
from app.grok_oauth.registration import is_valid_pkce_s256_challenge
from app.main import create_app
from app.settings import Settings
from tests.conftest import GROK_OAUTH_OWNER_SECRET, GROK_OAUTH_OWNER_SECRET_HASH
from tests.oauth_helpers import (
    complete_owner_login,
    pkce_pair,
    register_test_client,
    start_authorization,
    submit_consent,
)


def test_oauth_disabled_does_not_require_oauth_settings(settings):
    settings.require_runtime_configuration()
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.post("/grok/mcp/").status_code == 404
        assert client.get("/.well-known/oauth-authorization-server").status_code == 404
        assert client.get("/.well-known/oauth-protected-resource/grok/mcp/").status_code == 404
        assert client.get("/authorize").status_code == 404


def test_enabled_oauth_missing_settings_fail_startup(settings):
    values = settings.model_dump()
    values["grok_mcp_oauth_enabled"] = True
    with pytest.raises(ValidationError, match="GROK_MCP_OAUTH_ENABLED requires"):
        Settings(**values)


def test_invalid_owner_hash_fails_startup(oauth_settings):
    values = oauth_settings.model_dump()
    values["grok_mcp_oauth_owner_secret_hash"] = SecretStr("not-a-real-hash")
    with pytest.raises(ValidationError, match="Argon2id"):
        Settings(**values)


def test_unsafe_ttl_values_fail_startup(oauth_settings):
    values = oauth_settings.model_dump()
    values["grok_mcp_oauth_access_token_ttl_seconds"] = 86400
    with pytest.raises(ValidationError, match="ACCESS_TOKEN_TTL"):
        Settings(**values)
    values = oauth_settings.model_dump()
    values["grok_mcp_oauth_refresh_token_ttl_days"] = 365
    with pytest.raises(ValidationError, match="REFRESH_TOKEN_TTL"):
        Settings(**values)
    values = oauth_settings.model_dump()
    values["grok_mcp_oauth_auth_code_ttl_seconds"] = 3600
    with pytest.raises(ValidationError, match="AUTH_CODE_TTL"):
        Settings(**values)


def test_oauth_secrets_are_redacted_from_repr_and_validation_errors(oauth_settings):
    secret = "super-secret-signing-key-value-not-for-logs"
    values = oauth_settings.model_dump()
    values["grok_mcp_oauth_signing_key"] = SecretStr("short")
    with pytest.raises(ValidationError) as exc:
        Settings(**values)
    error_text = str(exc.value)
    assert "GROK_MCP_OAUTH_SIGNING_KEY" in error_text
    assert secret not in error_text
    assert GROK_OAUTH_OWNER_SECRET not in error_text
    assert GROK_OAUTH_OWNER_SECRET_HASH not in error_text
    values = oauth_settings.model_dump()
    values["grok_mcp_oauth_signing_key"] = SecretStr(secret)
    updated = Settings(**values)
    assert secret not in repr(updated)
    assert GROK_OAUTH_OWNER_SECRET not in repr(updated)
    assert GROK_OAUTH_OWNER_SECRET_HASH not in repr(updated)


def test_unauthorized_grok_mcp_returns_401_with_resource_metadata(oauth_settings):
    app = create_app(oauth_settings)
    resource = grok_mcp_resource(oauth_settings.public_base_url or "")
    with TestClient(app) as client:
        response = client.post(
            GROK_MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert response.status_code == 401
        www = response.headers["www-authenticate"]
        assert "Bearer" in www
        assert "resource_metadata=" in www
        assert "/.well-known/oauth-protected-resource/grok/mcp/" in www
        metadata = client.get("/.well-known/oauth-protected-resource/grok/mcp/")
        assert metadata.status_code == 200
        body = metadata.json()
        assert body["resource"].rstrip("/") == resource.rstrip("/")
        as_meta = client.get("/.well-known/oauth-authorization-server")
        assert as_meta.status_code == 200
        advertised = as_meta.json()
        assert advertised["issuer"].rstrip("/") == oauth_settings.public_base_url
        assert advertised["code_challenge_methods_supported"] == ["S256"]
        assert "authorization_code" in advertised["grant_types_supported"]
        assert "refresh_token" in advertised["grant_types_supported"]
        assert advertised["registration_endpoint"].endswith("/register")
        assert advertised["revocation_endpoint"].endswith("/revoke")


def test_pkce_s256_challenge_format():
    _, challenge = pkce_pair()
    assert is_valid_pkce_s256_challenge(challenge)
    assert is_valid_pkce_s256_challenge("A" * 43)
    assert not is_valid_pkce_s256_challenge("")
    assert not is_valid_pkce_s256_challenge("abc")
    assert not is_valid_pkce_s256_challenge("A" * 42)
    assert not is_valid_pkce_s256_challenge("+" * 43)
    assert not is_valid_pkce_s256_challenge(None)


def _assert_pkce_rejected(client: TestClient, response) -> None:
    location = response.headers.get("location", "")
    body = response.text
    assert GROK_OAUTH_CONSENT_PATH not in location
    assert GROK_OAUTH_CONSENT_PATH not in body
    assert 'name="owner_secret"' not in body
    if response.status_code == 302:
        assert "error=" in location
        assert "error=invalid_request" in location or "error=unsupported_response_type" in location
        if location.startswith("/"):
            followed = client.get(location, follow_redirects=False)
            assert followed.status_code != 200 or 'name="owner_secret"' not in followed.text
            assert GROK_OAUTH_CONSENT_PATH not in followed.headers.get("location", "")
        return
    assert response.status_code == 400
    payload = response.json()
    assert payload["error"] in {"invalid_request", "unsupported_response_type"}


def test_pkce_s256_is_required_and_plain_fails(oauth_settings):
    app = create_app(oauth_settings)
    resource = grok_mcp_resource(oauth_settings.public_base_url or "")
    with TestClient(app) as client:
        registered = register_test_client(client)
        missing = start_authorization(
            client,
            client_id=registered["client_id"],
            redirect_uri="https://grok.example/callback",
            challenge="abc",
            resource=resource,
            extra={"code_challenge": ""},
        )
        _assert_pkce_rejected(client, missing)

        omitted = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": registered["client_id"],
                "redirect_uri": "https://grok.example/callback",
                "code_challenge_method": "S256",
                "scope": GROK_OAUTH_SCOPE,
                "resource": resource,
            },
            follow_redirects=False,
        )
        _assert_pkce_rejected(client, omitted)

        too_short = start_authorization(
            client,
            client_id=registered["client_id"],
            redirect_uri="https://grok.example/callback",
            challenge="abc",
            resource=resource,
        )
        _assert_pkce_rejected(client, too_short)

        plain = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": registered["client_id"],
                "redirect_uri": "https://grok.example/callback",
                "code_challenge": "abc",
                "code_challenge_method": "plain",
                "scope": GROK_OAUTH_SCOPE,
                "resource": resource,
            },
            follow_redirects=False,
        )
        _assert_pkce_rejected(client, plain)


def test_incorrect_verifier_and_one_time_code(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        registered = register_test_client(client)
        verifier, challenge = pkce_pair()
        tokens = complete_owner_login(
            client,
            registered=registered,
            settings=oauth_settings,
            verifier=verifier,
            challenge=challenge,
        )
        replay = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": tokens["code"],
                "redirect_uri": tokens["redirect_uri"],
                "client_id": registered["client_id"],
                "client_secret": registered["client_secret"],
                "code_verifier": verifier,
                "resource": tokens["resource"],
            },
        )
        assert replay.status_code == 401
        assert replay.json()["error"] == "invalid_grant"

        verifier2, challenge2 = pkce_pair()
        authorize = start_authorization(
            client,
            client_id=registered["client_id"],
            redirect_uri="https://grok.example/callback",
            challenge=challenge2,
            resource=tokens["resource"],
        )
        page = client.get(authorize.headers["location"])
        approved = submit_consent(client, html=page.text)
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        wrong = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://grok.example/callback",
                "client_id": registered["client_id"],
                "client_secret": registered["client_secret"],
                "code_verifier": "wrong-verifier-value-that-will-not-match",
                "resource": tokens["resource"],
            },
        )
        assert wrong.status_code == 401
        assert wrong.json()["error"] == "invalid_grant"


def test_wrong_client_redirect_and_resource_fail(oauth_settings):
    app = create_app(oauth_settings)
    resource = grok_mcp_resource(oauth_settings.public_base_url or "")
    with TestClient(app) as client:
        registered = register_test_client(client)
        other = register_test_client(client, redirect_uri="https://other.example/callback")
        tokens = complete_owner_login(client, registered=registered, settings=oauth_settings)
        stolen = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": tokens["code"],
                "redirect_uri": tokens["redirect_uri"],
                "client_id": other["client_id"],
                "client_secret": other["client_secret"],
                "code_verifier": tokens["verifier"],
                "resource": resource,
            },
        )
        assert stolen.status_code == 401

        mismatch = start_authorization(
            client,
            client_id=registered["client_id"],
            redirect_uri="https://evil.example/callback",
            challenge=pkce_pair()[1],
            resource=resource,
        )
        assert mismatch.status_code in {302, 400}

        wrong_resource = start_authorization(
            client,
            client_id=registered["client_id"],
            redirect_uri="https://grok.example/callback",
            challenge=pkce_pair()[1],
            resource="https://example.test/mcp/",
        )
        assert wrong_resource.status_code == 302
        location = wrong_resource.headers["location"]
        parsed_location = urlparse(location)
        if (parsed_location.scheme, parsed_location.netloc) == ("https", "grok.example"):
            assert "error=" in location
        else:
            page = client.get(location)
            assert page.status_code == 200


def test_refresh_rotates_and_reuse_revokes_family(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        registered = register_test_client(client)
        tokens = complete_owner_login(client, registered=registered, settings=oauth_settings)
        first_refresh = tokens["refresh_token"]
        refreshed = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first_refresh,
                "client_id": registered["client_id"],
                "client_secret": registered["client_secret"],
                "scope": GROK_OAUTH_SCOPE,
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        rotated = refreshed.json()
        assert rotated["access_token"]
        assert rotated["refresh_token"]
        assert rotated["refresh_token"] != first_refresh

        old = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first_refresh,
                "client_id": registered["client_id"],
                "client_secret": registered["client_secret"],
            },
        )
        assert old.status_code == 401
        successor = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": rotated["refresh_token"],
                "client_id": registered["client_id"],
                "client_secret": registered["client_secret"],
            },
        )
        assert successor.status_code == 401

        mcp = client.post(
            GROK_MCP_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {rotated['access_token']}"},
        )
        assert mcp.status_code == 401


def test_revoked_tokens_fail(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        registered = register_test_client(client)
        tokens = complete_owner_login(client, registered=registered, settings=oauth_settings)
        revoked = client.post(
            "/revoke",
            data={
                "token": tokens["refresh_token"],
                "token_type_hint": "refresh_token",
                "client_id": registered["client_id"],
                "client_secret": registered["client_secret"],
            },
        )
        assert revoked.status_code == 200
        refresh = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": registered["client_id"],
                "client_secret": registered["client_secret"],
            },
        )
        assert refresh.status_code == 401
        mcp = client.post(
            GROK_MCP_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert mcp.status_code == 401


def test_invalid_expired_wrong_audience_and_scope_tokens_fail(oauth_settings):
    from app.grok_oauth.tokens import AccessTokenIssuer

    app = create_app(oauth_settings)
    with TestClient(app) as client:
        registered = register_test_client(client)
        complete_owner_login(client, registered=registered, settings=oauth_settings)
        bogus = client.post(
            GROK_MCP_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        assert bogus.status_code == 401

        provider = app.state.grok_oauth
        other = AccessTokenIssuer(
            issuer=provider._issuer.issuer,
            audience="https://example.test/other/",
            signing_key=provider._issuer.signing_key,
        )
        bad_aud = other.issue(
            client_id=registered["client_id"],
            scopes=[GROK_OAUTH_SCOPE],
            jti="jti-wrong-aud",
            family_id="family-wrong",
            expires_in=60,
        )
        wrong = client.post(
            GROK_MCP_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {bad_aud}"},
        )
        assert wrong.status_code == 401

        no_scope = provider._issuer.issue(
            client_id=registered["client_id"],
            scopes=["openid"],
            jti="jti-noscope",
            family_id="family-noscope",
            expires_in=60,
        )
        insufficient = client.post(
            GROK_MCP_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {no_scope}"},
        )
        assert insufficient.status_code in {401, 403}

        expired = provider._issuer.issue(
            client_id=registered["client_id"],
            scopes=[GROK_OAUTH_SCOPE],
            jti="jti-expired",
            family_id="family-expired",
            expires_in=-10,
        )
        timed_out = client.post(
            GROK_MCP_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {expired}"},
        )
        assert timed_out.status_code == 401


def test_registrations_and_refresh_survive_restart(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        registered = register_test_client(client)
        tokens = complete_owner_login(client, registered=registered, settings=oauth_settings)
        client_id = registered["client_id"]
        client_secret = registered["client_secret"]
        refresh_token = tokens["refresh_token"]

    restarted = create_app(oauth_settings)
    with TestClient(restarted) as client:
        looked_up = restarted.state.grok_oauth
        stored = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        assert stored.status_code == 200, stored.text
        assert stored.json()["access_token"]
        assert looked_up is restarted.state.grok_oauth


@pytest.mark.asyncio
async def test_expired_and_revoked_state_remain_invalid_after_restart(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        registered = register_test_client(client)
        tokens = complete_owner_login(client, registered=registered, settings=oauth_settings)
        provider = app.state.grok_oauth
        db = provider._store()
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        await db.execute(
            "UPDATE oauth_refresh_tokens SET expires_at = ?",
            (past,),
        )
        refresh_token = tokens["refresh_token"]
        client_id = registered["client_id"]
        client_secret = registered["client_secret"]

    restarted = create_app(oauth_settings)
    with TestClient(restarted) as client:
        expired = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        assert expired.status_code == 401


def test_oauth_logs_do_not_contain_secrets(oauth_settings, caplog):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        with caplog.at_level("INFO"):
            registered = register_test_client(client)
            tokens = complete_owner_login(client, registered=registered, settings=oauth_settings)
    combined = caplog.text
    assert GROK_OAUTH_OWNER_SECRET not in combined
    assert tokens["access_token"] not in combined
    assert tokens["refresh_token"] not in combined
    assert tokens["code"] not in combined
    assert registered["client_secret"] not in combined


def test_public_base_url_change_keeps_encrypted_oauth_storage(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        registered = register_test_client(client)
    values = oauth_settings.model_dump()
    values["public_base_url"] = "https://new-tunnel.example.test"
    rotated = Settings(**values)
    restarted = create_app(rotated)
    with TestClient(restarted) as client:
        assert client.get("/healthz").status_code == 200
        _, challenge = pkce_pair()
        authorize = start_authorization(
            client,
            client_id=registered["client_id"],
            redirect_uri="https://grok.example/callback",
            challenge=challenge,
            resource=grok_mcp_resource(rotated.public_base_url or ""),
        )
        assert authorize.status_code == 302
        assert GROK_OAUTH_CONSENT_PATH in authorize.headers["location"]


def test_wrong_storage_key_fails_closed(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        register_test_client(client)
    poisoned_values = oauth_settings.model_dump()
    poisoned_values["grok_mcp_oauth_storage_encryption_key"] = SecretStr("z" * 64)
    poisoned = Settings(**poisoned_values)
    broken = create_app(poisoned)
    with pytest.raises(RuntimeError, match="STORAGE_ENCRYPTION_KEY"):
        with TestClient(broken):
            pass


def test_stored_oauth_rows_are_hashed_or_encrypted(oauth_settings):
    import sqlite3

    app = create_app(oauth_settings)
    with TestClient(app) as client:
        registered = register_test_client(client)
        tokens = complete_owner_login(client, registered=registered, settings=oauth_settings)

    conn = sqlite3.connect(oauth_settings.database_path)
    conn.row_factory = sqlite3.Row
    try:
        for table in (
            "oauth_clients",
            "oauth_auth_transactions",
            "oauth_authorization_codes",
            "oauth_refresh_tokens",
            "oauth_access_jtis",
            "oauth_audit",
        ):
            rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            serialized = json.dumps(rows, default=str)
            assert GROK_OAUTH_OWNER_SECRET not in serialized
            assert tokens["access_token"] not in serialized
            assert tokens["refresh_token"] not in serialized
            assert tokens["code"] not in serialized
            assert registered["client_secret"] not in serialized
        clients = list(conn.execute("SELECT ciphertext FROM oauth_clients"))
        assert clients
        assert "client_secret" not in clients[0][0]
    finally:
        conn.close()


def _client_count(database_path: Path) -> int:
    conn = sqlite3.connect(database_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0])
    finally:
        conn.close()


def _client_ids(database_path: Path) -> set[str]:
    conn = sqlite3.connect(database_path)
    try:
        return {row[0] for row in conn.execute("SELECT client_id FROM oauth_clients")}
    finally:
        conn.close()


def test_hash_script_imports_app_without_repo_root_on_path():
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "hash_grok_oauth_owner_secret.py"
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    saved_path = list(sys.path)
    try:
        for name in list(saved_modules):
            sys.modules.pop(name, None)
        sys.path[:] = [
            entry
            for entry in sys.path
            if Path(entry).resolve() != repo and Path(entry).resolve() != script.parent
        ]
        sys.path.insert(0, str(script.parent))
        with pytest.raises(ModuleNotFoundError):
            import app  # noqa: F401
        namespace = runpy.run_path(str(script), run_name="hash_script_regression")
        assert callable(namespace["main"])
        assert callable(namespace["hash_owner_secret"])
    finally:
        sys.path[:] = saved_path
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


def test_registration_rejects_oversized_body_and_metadata_before_persist(oauth_settings):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        huge = client.post(
            "/register",
            content=b'{"redirect_uris":["https://grok.example/callback"],"pad":"'
            + b"x" * (20 * 1024)
            + b'"}',
            headers={"content-type": "application/json"},
        )
        assert huge.status_code in {400, 413}
        payload = huge.json()
        assert payload["error"] in {"invalid_client_metadata", "invalid_request"}
        assert _client_count(oauth_settings.database_path) == 0

        oversized_name = client.post(
            "/register",
            json={
                "redirect_uris": ["https://grok.example/callback"],
                "client_name": "A" * 4096,
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": GROK_OAUTH_SCOPE,
            },
        )
        assert oversized_name.status_code == 400
        assert oversized_name.json()["error"] == "invalid_client_metadata"
        assert _client_count(oauth_settings.database_path) == 0

        too_many_uris = client.post(
            "/register",
            json={
                "redirect_uris": [f"https://client{i}.example/callback" for i in range(16)],
                "client_name": "Too Many URIs",
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": GROK_OAUTH_SCOPE,
            },
        )
        assert too_many_uris.status_code == 400
        assert too_many_uris.json()["error"] in {
            "invalid_client_metadata",
            "invalid_redirect_uri",
        }
        assert _client_count(oauth_settings.database_path) == 0


def test_registration_quota_evicts_unused_clients_and_rejects_when_full(
    oauth_settings, monkeypatch
):
    monkeypatch.setattr("app.grok_oauth.constants.OAUTH_CLIENT_MAX_COUNT", 2)
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        first = register_test_client(client, client_name="one")
        second = register_test_client(client, client_name="two")
        third = register_test_client(client, client_name="three")
        assert _client_count(oauth_settings.database_path) == 2
        remaining = _client_ids(oauth_settings.database_path)
        assert first["client_id"] not in remaining
        assert second["client_id"] in remaining
        assert third["client_id"] in remaining

        complete_owner_login(client, registered=second, settings=oauth_settings)
        complete_owner_login(client, registered=third, settings=oauth_settings)
        blocked = client.post(
            "/register",
            json={
                "redirect_uris": ["https://grok.example/callback"],
                "client_name": "quota-full",
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": GROK_OAUTH_SCOPE,
            },
        )
        assert blocked.status_code == 400
        assert blocked.json()["error"] == "invalid_client_metadata"
        assert "quota" in (blocked.json().get("error_description") or "").lower()
        assert _client_count(oauth_settings.database_path) == 2


def test_authorization_transactions_are_purged_and_bounded(oauth_settings, monkeypatch):
    monkeypatch.setattr("app.grok_oauth.constants.OAUTH_TRANSACTION_MAX_COUNT", 2)
    monkeypatch.setattr("app.grok_oauth.constants.OAUTH_TRANSACTION_MAX_PER_CLIENT", 2)
    app = create_app(oauth_settings)
    resource = grok_mcp_resource(oauth_settings.public_base_url or "")
    with TestClient(app) as client:
        registered = register_test_client(client)
        _, challenge = pkce_pair()
        first = start_authorization(
            client,
            client_id=registered["client_id"],
            redirect_uri="https://grok.example/callback",
            challenge=challenge,
            resource=resource,
        )
        second = start_authorization(
            client,
            client_id=registered["client_id"],
            redirect_uri="https://grok.example/callback",
            challenge=challenge,
            resource=resource,
        )
        assert first.status_code == 302
        assert second.status_code == 302
        blocked = start_authorization(
            client,
            client_id=registered["client_id"],
            redirect_uri="https://grok.example/callback",
            challenge=challenge,
            resource=resource,
        )
        _assert_pkce_rejected(client, blocked)
        assert _table_count(oauth_settings.database_path, "oauth_auth_transactions") == 2

        past = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        conn = sqlite3.connect(oauth_settings.database_path)
        try:
            conn.execute("UPDATE oauth_auth_transactions SET expires_at = ?", (past,))
            conn.commit()
        finally:
            conn.close()
        after_purge = start_authorization(
            client,
            client_id=registered["client_id"],
            redirect_uri="https://grok.example/callback",
            challenge=challenge,
            resource=resource,
        )
        assert after_purge.status_code == 302
        assert GROK_OAUTH_CONSENT_PATH in after_purge.headers["location"]
        assert _table_count(oauth_settings.database_path, "oauth_auth_transactions") == 1


def test_authorization_transaction_quota_is_per_client(oauth_settings, monkeypatch):
    monkeypatch.setattr("app.grok_oauth.constants.OAUTH_TRANSACTION_MAX_COUNT", 8)
    monkeypatch.setattr("app.grok_oauth.constants.OAUTH_TRANSACTION_MAX_PER_CLIENT", 1)
    app = create_app(oauth_settings)
    resource = grok_mcp_resource(oauth_settings.public_base_url or "")
    with TestClient(app) as client:
        first = register_test_client(client, client_name="one")
        second = register_test_client(client, client_name="two")
        _, challenge = pkce_pair()
        allowed = start_authorization(
            client,
            client_id=first["client_id"],
            redirect_uri="https://grok.example/callback",
            challenge=challenge,
            resource=resource,
        )
        blocked = start_authorization(
            client,
            client_id=first["client_id"],
            redirect_uri="https://grok.example/callback",
            challenge=challenge,
            resource=resource,
        )
        other = start_authorization(
            client,
            client_id=second["client_id"],
            redirect_uri="https://grok.example/callback",
            challenge=challenge,
            resource=resource,
        )
        assert allowed.status_code == 302
        _assert_pkce_rejected(client, blocked)
        assert other.status_code == 302
        assert GROK_OAUTH_CONSENT_PATH in other.headers["location"]
        assert _table_count(oauth_settings.database_path, "oauth_auth_transactions") == 2


def test_registration_retention_purges_unused_clients(oauth_settings, monkeypatch):
    monkeypatch.setattr("app.grok_oauth.constants.OAUTH_CLIENT_UNUSED_RETENTION_SECONDS", 0)
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        stale = register_test_client(client, client_name="stale")
        replacement = register_test_client(client, client_name="replacement")
        remaining = _client_ids(oauth_settings.database_path)
        assert stale["client_id"] not in remaining
        assert replacement["client_id"] in remaining
        assert _client_count(oauth_settings.database_path) == 1


def _table_count(database_path: Path, table: str) -> int:
    conn = sqlite3.connect(database_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _audit_metadata(database_path: Path) -> list[dict]:
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, event, metadata_json, created_at FROM oauth_audit ORDER BY id ASC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


async def test_oauth_audit_insert_prunes_old_rows_and_caps_newest(database, monkeypatch):
    monkeypatch.setattr("app.grok_oauth.constants.OAUTH_AUDIT_MAX_COUNT", 3)
    monkeypatch.setattr("app.grok_oauth.constants.OAUTH_AUDIT_RETENTION_SECONDS", 3600)
    stale_at = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    await database.execute(
        "INSERT INTO oauth_audit (event, metadata_json, created_at) VALUES (?, ?, ?)",
        ("stale", "{}", stale_at),
    )
    for index in range(6):
        await database.oauth_record_audit("client_registered", extra={"n": index})
    rows = await database.fetch_all("SELECT event, metadata_json FROM oauth_audit ORDER BY id ASC")
    assert len(rows) == 3
    assert all(row["event"] == "client_registered" for row in rows)
    assert [json.loads(str(row["metadata_json"]))["n"] for row in rows] == [3, 4, 5]


async def test_oauth_create_transaction_with_quota_purges_and_enforces_bounds(database):
    now = datetime.now(UTC)
    past = (now - timedelta(minutes=10)).isoformat()
    future = (now + timedelta(minutes=5)).isoformat()
    await database.oauth_create_transaction(
        transaction_id="expired-tx",
        client_id="client-1",
        csrf_hash="csrf",
        ciphertext="cipher",
        expires_at=past,
    )
    inserted = await database.oauth_create_transaction_with_quota(
        transaction_id="live-tx",
        client_id="client-1",
        csrf_hash="csrf",
        ciphertext="cipher",
        expires_at=future,
        max_transactions=1,
        max_per_client=1,
        now=now.isoformat(),
    )
    assert inserted is True
    blocked = await database.oauth_create_transaction_with_quota(
        transaction_id="overflow-tx",
        client_id="client-1",
        csrf_hash="csrf",
        ciphertext="cipher",
        expires_at=future,
        max_transactions=8,
        max_per_client=1,
        now=now.isoformat(),
    )
    assert blocked is False
    other = await database.oauth_create_transaction_with_quota(
        transaction_id="other-tx",
        client_id="client-2",
        csrf_hash="csrf",
        ciphertext="cipher",
        expires_at=future,
        max_transactions=8,
        max_per_client=1,
        now=now.isoformat(),
    )
    assert other is True
    rows = await database.fetch_all(
        "SELECT transaction_id FROM oauth_auth_transactions ORDER BY transaction_id"
    )
    assert [row["transaction_id"] for row in rows] == ["live-tx", "other-tx"]


async def test_oauth_purge_expired_removes_unconsumed_refresh_and_expired_families(database):
    now = datetime.now(UTC)
    past = (now - timedelta(days=1)).isoformat()
    future = (now + timedelta(days=30)).isoformat()
    await database.execute(
        """INSERT INTO oauth_token_families
           (family_id, client_id, revoked, created_at, expires_at)
           VALUES (?, ?, 0, ?, ?)""",
        ("expired-family", "client-1", past, past),
    )
    await database.execute(
        """INSERT INTO oauth_refresh_tokens
           (token_hash, family_id, client_id, ciphertext, expires_at, consumed, revoked, created_at)
           VALUES (?, ?, ?, ?, ?, 0, 0, ?)""",
        ("expired-refresh", "expired-family", "client-1", "cipher", past, past),
    )
    await database.execute(
        """INSERT INTO oauth_access_jtis
           (jti, family_id, expires_at, revoked, created_at)
           VALUES (?, ?, ?, 0, ?)""",
        ("expired-jti", "expired-family", past, past),
    )
    await database.execute(
        """INSERT INTO oauth_token_families
           (family_id, client_id, revoked, created_at, expires_at)
           VALUES (?, ?, 0, ?, ?)""",
        ("valid-family", "client-1", now.isoformat(), future),
    )
    await database.execute(
        """INSERT INTO oauth_refresh_tokens
           (token_hash, family_id, client_id, ciphertext, expires_at, consumed, revoked, created_at)
           VALUES (?, ?, ?, ?, ?, 0, 0, ?)""",
        ("valid-refresh", "valid-family", "client-1", "cipher", future, now.isoformat()),
    )
    await database.oauth_purge_expired(now.isoformat())
    families = {
        row["family_id"]
        for row in await database.fetch_all("SELECT family_id FROM oauth_token_families")
    }
    refresh_hashes = {
        row["token_hash"]
        for row in await database.fetch_all("SELECT token_hash FROM oauth_refresh_tokens")
    }
    jtis = {row["jti"] for row in await database.fetch_all("SELECT jti FROM oauth_access_jtis")}
    assert families == {"valid-family"}
    assert refresh_hashes == {"valid-refresh"}
    assert jtis == set()


def test_repeated_registration_cannot_grow_oauth_audit_beyond_limit(oauth_settings, monkeypatch):
    monkeypatch.setattr("app.grok_oauth.constants.OAUTH_AUDIT_MAX_COUNT", 3)
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        registered = [register_test_client(client, client_name=f"client-{i}") for i in range(8)]
    rows = _audit_metadata(oauth_settings.database_path)
    assert len(rows) == 3
    assert _table_count(oauth_settings.database_path, "oauth_audit") == 3
    remaining_ids = {json.loads(row["metadata_json"])["client_id"] for row in rows}
    assert remaining_ids == {item["client_id"] for item in registered[-3:]}
    assert all(row["event"] == "client_registered" for row in rows)


def test_token_issuance_purges_expired_unconsumed_refresh_and_keeps_valid_family(
    oauth_settings,
):
    app = create_app(oauth_settings)
    with TestClient(app) as client:
        registered = register_test_client(client)
        complete_owner_login(client, registered=registered, settings=oauth_settings)
        conn = sqlite3.connect(oauth_settings.database_path)
        try:
            valid_families = {
                row[0]
                for row in conn.execute(
                    "SELECT family_id FROM oauth_token_families WHERE revoked = 0"
                )
            }
            past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                """INSERT INTO oauth_token_families
                   (family_id, client_id, revoked, created_at, expires_at)
                   VALUES (?, ?, 0, ?, ?)""",
                ("expired-family", registered["client_id"], past, past),
            )
            conn.execute(
                """INSERT INTO oauth_refresh_tokens
                   (token_hash, family_id, client_id, ciphertext, expires_at,
                    consumed, revoked, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, 0, ?)""",
                (
                    "expired-unconsumed-refresh",
                    "expired-family",
                    registered["client_id"],
                    "cipher",
                    past,
                    past,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        complete_owner_login(client, registered=registered, settings=oauth_settings)

    conn = sqlite3.connect(oauth_settings.database_path)
    try:
        families = {row[0] for row in conn.execute("SELECT family_id FROM oauth_token_families")}
        refresh_hashes = {
            row[0] for row in conn.execute("SELECT token_hash FROM oauth_refresh_tokens")
        }
    finally:
        conn.close()
    assert "expired-family" not in families
    assert "expired-unconsumed-refresh" not in refresh_hashes
    assert valid_families <= families
    assert len(families) >= 1
