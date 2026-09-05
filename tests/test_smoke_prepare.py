from __future__ import annotations

import json
import os

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.cli import main
from app.main import create_app
from app.settings import Settings
from app.smoke_prepare import (
    EXPECTED_TOOLS,
    SmokeTargetError,
    parse_mcp_payload,
    run_prepare_only_smoke,
    validate_smoke_target,
)

LIVE_BEARER = "live-bearer-must-not-be-read"


def test_prepare_only_smoke_against_evaluation_app(tmp_path):
    settings = Settings.from_values(
        agent_call_profile="evaluation",
        database_url=f"sqlite:///{tmp_path / 'smoke.db'}",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        result = run_prepare_only_smoke(
            client.post,
            bearer=settings.reveal(settings.mcp_bearer_token),
            user_id=settings.allowed_agent_user_id or "",
            owner_phone=settings.owner_phone_e164 or "",
        )
    assert result.ok, result.detail
    assert result.plan_id and result.plan_id.startswith("plan_")
    assert set(result.tools) == EXPECTED_TOOLS
    assert result.invoked_tools == ("prepare_phone_call",)
    assert "start_phone_call" not in result.invoked_tools
    assert "start_phone_call" not in result.requests


@respx.mock
def test_prepare_only_smoke_makes_no_provider_http(tmp_path):
    settings = Settings.from_values(
        agent_call_profile="evaluation",
        database_url=f"sqlite:///{tmp_path / 'smoke2.db'}",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        result = run_prepare_only_smoke(
            client.post,
            bearer=settings.reveal(settings.mcp_bearer_token),
            user_id=settings.allowed_agent_user_id or "",
            owner_phone=settings.owner_phone_e164 or "",
        )
    assert result.ok, result.detail
    assert respx.calls.call_count == 0


def test_parse_mcp_payload_json_and_sse():
    assert parse_mcp_payload('{"ok": true}') == {"ok": True}
    sse = 'event: message\ndata: {"result": {"tools": []}}\n\n'
    assert parse_mcp_payload(sse)["result"]["tools"] == []
    with pytest.raises(ValueError, match="not JSON"):
        parse_mcp_payload("not-json")


def test_cli_smoke_prepare_uses_healthz_and_never_starts(tmp_path, monkeypatch):
    from app.evaluation import EVALUATION_MCP_BEARER

    settings = Settings.from_values(
        agent_call_profile="evaluation",
        database_url=f"sqlite:///{tmp_path / 'cli-smoke.db'}",
    )
    app = create_app(settings)

    class _Client:
        def __init__(self, **kwargs):
            self._inner = TestClient(app)

        def __enter__(self):
            self._ctx = self._inner.__enter__()
            return self

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

        def get(self, path):
            return self._ctx.get(path)

        def post(self, path, json=None, headers=None):
            return self._ctx.post(path, json=json, headers=headers)

    monkeypatch.setattr("httpx.Client", _Client)
    monkeypatch.setenv("MCP_BEARER_TOKEN", EVALUATION_MCP_BEARER)
    monkeypatch.setenv("ALLOWED_AGENT_USER_ID", settings.allowed_agent_user_id or "")
    assert main(["smoke-prepare"]) == 0


def test_validate_smoke_target_accepts_loopback_http_and_https_origin():
    origin, path = validate_smoke_target("http://127.0.0.1:8000", "/mcp/")
    assert origin == "http://127.0.0.1:8000"
    assert path == "/mcp/"
    origin, path = validate_smoke_target("https://calls.example", "/mcp/")
    assert origin == "https://calls.example"
    assert path == "/mcp/"


@pytest.mark.parametrize(
    ("base_url", "mcp_path"),
    [
        ("http://127.0.0.1:8000", "https://evil.example/mcp/"),
        ("http://127.0.0.1:8000", "http://evil.example/mcp/"),
        ("http://127.0.0.1:8000", "//evil.example/mcp/"),
        ("http://127.0.0.1:8000", "/mcp/#frag"),
        ("http://user:pass@127.0.0.1:8000", "/mcp/"),
        ("http://calls.example", "/mcp/"),
        ("https://calls.example/mcp/", "/mcp/"),
        ("https://calls.example?x=1", "/mcp/"),
        ("https://calls.example#frag", "/mcp/"),
    ],
)
def test_validate_smoke_target_rejects_credential_leaking_targets(base_url, mcp_path):
    with pytest.raises(SmokeTargetError):
        validate_smoke_target(base_url, mcp_path)


def _forbid_credential_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real_get = os.environ.get

    def wrapped(key, default=None):
        if key in {"MCP_BEARER_TOKEN", "ALLOWED_AGENT_USER_ID"}:
            reads.append(str(key))
        return real_get(key, default)

    monkeypatch.setenv("MCP_BEARER_TOKEN", LIVE_BEARER)
    monkeypatch.setenv("ALLOWED_AGENT_USER_ID", "live-user-must-not-be-read")
    monkeypatch.setattr(os.environ, "get", wrapped)
    monkeypatch.setattr(
        "httpx.Client",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("httpx.Client constructed")),
    )

    def boom_credentials(environ=None):
        reads.append("credentials_from_environ")
        raise AssertionError("credentials_from_environ must not run")

    monkeypatch.setattr("app.smoke_prepare.credentials_from_environ", boom_credentials)
    return reads


def test_cli_rejects_absolute_mcp_path_before_any_request(monkeypatch, capsys):
    reads = _forbid_credential_reads(monkeypatch)
    code = main(
        [
            "smoke-prepare",
            "--base-url",
            "http://127.0.0.1:8000",
            "--mcp-path",
            "https://evil.example/mcp/",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert reads == []
    combined = captured.out + captured.err
    assert LIVE_BEARER not in combined
    assert "evil.example" not in combined


def test_cli_rejects_scheme_relative_mcp_path_before_any_request(monkeypatch, capsys):
    reads = _forbid_credential_reads(monkeypatch)
    code = main(
        [
            "smoke-prepare",
            "--base-url",
            "http://127.0.0.1:8000",
            "--mcp-path",
            "//evil.example/mcp/",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert reads == []
    assert LIVE_BEARER not in captured.out + captured.err


def test_cli_rejects_remote_plaintext_http_before_any_request(monkeypatch, capsys):
    reads = _forbid_credential_reads(monkeypatch)
    code = main(
        [
            "smoke-prepare",
            "--base-url",
            "http://calls.example",
            "--mcp-path",
            "/mcp/",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert reads == []
    assert LIVE_BEARER not in captured.out + captured.err
    assert "Authorization" not in captured.out + captured.err


def _mcp_mock_handler(request: httpx.Request, seen: list[httpx.Request]) -> httpx.Response:
    seen.append(request)
    if request.method == "GET" and request.url.path == "/healthz":
        return httpx.Response(200, json={"status": "ok"})
    payload = json.loads(request.content)
    method = payload.get("method")
    if method == "initialize":
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"serverInfo": {"name": "agent-call"}},
            },
            headers={"mcp-session-id": "sess"},
        )
    if method == "notifications/initialized":
        return httpx.Response(202)
    if method == "tools/list":
        tools = [{"name": name} for name in sorted(EXPECTED_TOOLS)]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}})
    if method == "tools/call":
        assert payload["params"]["name"] == "prepare_phone_call"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "result": {
                    "structuredContent": {
                        "plan_id": "plan_test",
                        "confirmation_summary": "summary",
                    }
                },
            },
        )
    raise AssertionError(f"unexpected {request.method} {request.url} {payload}")


def _patch_mock_transport(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda request: _mcp_mock_handler(request, seen))
    real_client = httpx.Client

    def fake_client(*, base_url, timeout, trust_env):
        return real_client(
            base_url=base_url,
            timeout=timeout,
            trust_env=trust_env,
            transport=transport,
        )

    monkeypatch.setattr("httpx.Client", fake_client)
    return seen


def test_cli_loopback_http_smoke_uses_mock_transport(monkeypatch, capsys):
    seen = _patch_mock_transport(monkeypatch)
    monkeypatch.setenv("MCP_BEARER_TOKEN", "loopback-bearer")
    monkeypatch.setenv("ALLOWED_AGENT_USER_ID", "loopback-user")
    code = main(["smoke-prepare", "--base-url", "http://127.0.0.1:8000", "--mcp-path", "/mcp/"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert seen
    assert all(request.url.host == "127.0.0.1" for request in seen)
    assert all(request.url.scheme == "http" for request in seen)
    assert any(request.url.path == "/healthz" for request in seen)
    assert any(request.url.path.rstrip("/") == "/mcp" for request in seen)
    assert any(request.headers.get("authorization") == "Bearer loopback-bearer" for request in seen)


def test_cli_remote_https_relative_mcp_path_uses_mock_transport(monkeypatch, capsys):
    seen = _patch_mock_transport(monkeypatch)
    monkeypatch.setenv("MCP_BEARER_TOKEN", "https-bearer")
    monkeypatch.setenv("ALLOWED_AGENT_USER_ID", "https-user")
    code = main(["smoke-prepare", "--base-url", "https://calls.example", "--mcp-path", "/mcp/"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert seen
    assert all(str(request.url).startswith("https://calls.example/") for request in seen)
    assert all(request.url.scheme == "https" for request in seen)
    assert any(request.url.path == "/mcp/" or request.url.path == "/mcp" for request in seen)
    assert any(request.headers.get("authorization") == "Bearer https-bearer" for request in seen)
