from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.call_state import CallService
from app.db import Database
from app.evaluation import LIVE_CALLS_DISABLED_CODE
from app.main import create_app
from app.models import PreparePhoneCallInput
from app.settings import Settings
from app.smoke_prepare import EXPECTED_TOOLS, run_prepare_only_smoke
from app.twilio_bridge import TwilioBridge
from tests.conftest import FakeExa, FakeTwilio


def evaluation_settings(tmp_path) -> Settings:
    return Settings.from_values(
        agent_call_profile="evaluation",
        database_url=f"sqlite:///{tmp_path / 'eval.db'}",
        owner_phone_e164="+14155550101",
        twilio_caller_id="+14155550199",
        allowed_agent_user_id="agent-user-1",
        mcp_bearer_token=SecretStr("mcp-test"),
    )


@pytest.mark.asyncio
async def test_evaluation_start_rejects_confirmed_plan_without_provider_io(tmp_path, packet):
    settings = evaluation_settings(tmp_path)
    assert settings.live_calls_enabled is False
    settings.require_runtime_configuration()
    db = Database(settings.database_path)
    await db.initialize()
    twilio = FakeTwilio()
    try:
        service = CallService(
            settings,
            db,
            twilio=twilio,
            openai=SimpleNamespace(),
            exa=FakeExa(),
        )
        for _ in range(2):
            prepared = await service.prepare(
                PreparePhoneCallInput(
                    context=packet,
                    authority_basis="Owner explicitly requested this evaluation",
                    requested_by_owner=True,
                )
            )
            assert prepared.plan_id
            with pytest.raises(ValueError, match=LIVE_CALLS_DISABLED_CODE) as exc_info:
                await service.start(
                    prepared.plan_id,
                    explicit_confirmation=True,
                    confirmation_text=prepared.confirmation_summary,
                )
            payload = json.loads(str(exc_info.value))
            assert payload["code"] == LIVE_CALLS_DISABLED_CODE
            assert twilio.agent_creates == 0
            assert twilio.callee_creates == 0
            assert twilio.owner_creates == 0
            stored = await db.get_plan(prepared.plan_id)
            assert stored is not None
            assert stored["state"] == "prepared"
    finally:
        await db.close()


@respx.mock
def test_evaluation_mcp_start_is_live_calls_disabled(tmp_path, packet, monkeypatch):
    settings = evaluation_settings(tmp_path)
    twilio_calls: list[object] = []
    openai_calls: list[object] = []

    async def capture_twilio(self, **kwargs):
        twilio_calls.append(kwargs)
        raise AssertionError("Twilio create_agent_participant must not run")

    async def capture_accept(self, **kwargs):
        openai_calls.append(kwargs)
        raise AssertionError("OpenAI accept must not run")

    monkeypatch.setattr(TwilioBridge, "create_agent_participant", capture_twilio)
    monkeypatch.setattr("app.openai_realtime.RealtimeBridge.accept_and_connect", capture_accept)

    headers = {
        "Authorization": f"Bearer {settings.reveal(settings.mcp_bearer_token)}",
        "X-Agent-User-Id": settings.allowed_agent_user_id or "",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        result = run_prepare_only_smoke(
            client.post,
            bearer=settings.reveal(settings.mcp_bearer_token),
            user_id=settings.allowed_agent_user_id or "",
            owner_phone=packet.owner.callback_number,
            target_phone=packet.target.phone,
        )
        assert result.ok, result.detail
        assert set(result.tools) == EXPECTED_TOOLS
        assert result.plan_id and result.confirmation_summary
        session_headers = dict(headers)
        if result.session_id:
            session_headers["mcp-session-id"] = result.session_id
        started = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "start_phone_call",
                    "arguments": {
                        "plan_id": result.plan_id,
                        "explicit_confirmation": True,
                        "confirmation_text": result.confirmation_summary,
                    },
                },
            },
            headers=session_headers,
        )
        body = started.text
        assert LIVE_CALLS_DISABLED_CODE in body
    assert twilio_calls == []
    assert openai_calls == []
    assert respx.calls.call_count == 0


def test_evaluation_profile_fills_missing_runtime_fields(tmp_path):
    settings = evaluation_settings(tmp_path)
    settings.require_runtime_configuration()
    assert settings.live_calls_enabled is False
    assert settings.public_base_url is not None


def test_live_profile_still_requires_runtime_fields(tmp_path):
    blank = Settings.from_values(
        agent_call_profile="live",
        database_url=f"sqlite:///{tmp_path / 'x.db'}",
    )
    with pytest.raises(RuntimeError, match="missing required environment variables"):
        blank.require_runtime_configuration()


def _isolate_serve_cwd(tmp_path, monkeypatch) -> None:
    from app.settings import CORE_RUNTIME_ENV_NAMES

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_CALL_PROFILE", raising=False)
    for name in CORE_RUNTIME_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_cli_evaluation_refuses_non_loopback_bind(tmp_path, monkeypatch):
    from app.cli import main

    _isolate_serve_cwd(tmp_path, monkeypatch)
    assert main(["serve", "--profile", "evaluation", "--host", "0.0.0.0"]) == 2


def test_cli_evaluation_from_dotenv_refuses_non_loopback_bind(tmp_path, monkeypatch):
    from app.cli import main

    _isolate_serve_cwd(tmp_path, monkeypatch)
    (tmp_path / ".env.local").write_text("AGENT_CALL_PROFILE=evaluation\n", encoding="utf-8")
    assert main(["serve", "--host", "0.0.0.0"]) == 2


def test_cli_evaluation_loopback_invokes_uvicorn(tmp_path, monkeypatch):
    from app.cli import main

    _isolate_serve_cwd(tmp_path, monkeypatch)
    called: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        called["app"] = app
        called.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    assert main(["serve", "--profile", "evaluation", "--host", "127.0.0.1", "--port", "8000"]) == 0
    assert called["app"] == "app.main:app"
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 8000


def test_cli_unsafe_bind_allows_all_interfaces(tmp_path, monkeypatch):
    from app.cli import main

    _isolate_serve_cwd(tmp_path, monkeypatch)
    called: dict[str, object] = {}
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: called.update(kwargs))
    assert main(["serve", "--profile", "evaluation", "--unsafe-bind"]) == 0
    assert called["host"] == "0.0.0.0"


def test_cli_unsafe_bind_env_is_ignored(tmp_path, monkeypatch):
    from app.cli import main

    _isolate_serve_cwd(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_CALL_UNSAFE_BIND", "true")
    assert main(["serve", "--profile", "evaluation", "--host", "0.0.0.0"]) == 2


def test_cli_serve_without_profile_does_not_force_evaluation(tmp_path, monkeypatch, capsys):
    from app.cli import main

    _isolate_serve_cwd(tmp_path, monkeypatch)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert main(["serve", "--host", "127.0.0.1"]) == 0
    assert os.environ.get("AGENT_CALL_PROFILE") != "evaluation"
    captured = capsys.readouterr()
    assert "this process can place billable calls" in captured.err


def test_cli_serve_refuses_implicit_live_when_dotenv_has_credentials(tmp_path, monkeypatch, capsys):
    from app.cli import main

    _isolate_serve_cwd(tmp_path, monkeypatch)
    (tmp_path / ".env.local").write_text("OPENAI_API_KEY=sk-not-printed\n", encoding="utf-8")
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert main(["serve", "--host", "127.0.0.1"]) == 2
    captured = capsys.readouterr()
    assert "this will place billable calls" in captured.err
    assert "sk-not-printed" not in captured.err
    assert "sk-not-printed" not in captured.out


def test_cli_serve_refuses_implicit_live_when_process_env_has_credentials(
    tmp_path, monkeypatch, capsys
):
    from app.cli import main

    _isolate_serve_cwd(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-printed")
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert main(["serve", "--host", "127.0.0.1"]) == 2
    captured = capsys.readouterr()
    assert "this will place billable calls" in captured.err
    assert "sk-not-printed" not in captured.err
    assert "sk-not-printed" not in captured.out


def test_cli_serve_explicit_live_warns_and_boots(tmp_path, monkeypatch, capsys):
    from app.cli import main

    _isolate_serve_cwd(tmp_path, monkeypatch)
    (tmp_path / ".env.local").write_text("OPENAI_API_KEY=sk-not-printed\n", encoding="utf-8")
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert main(["serve", "--profile", "live", "--host", "127.0.0.1"]) == 0
    captured = capsys.readouterr()
    assert "this process can place billable calls" in captured.err
    assert "sk-not-printed" not in captured.err


def test_cli_dotenv_live_profile_is_explicit(tmp_path, monkeypatch, capsys):
    from app.cli import main

    _isolate_serve_cwd(tmp_path, monkeypatch)
    (tmp_path / ".env.local").write_text(
        "AGENT_CALL_PROFILE=live\nOPENAI_API_KEY=sk-not-printed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert main(["serve", "--host", "127.0.0.1"]) == 0
    captured = capsys.readouterr()
    assert "this process can place billable calls" in captured.err
    assert "sk-not-printed" not in captured.err


def test_cli_process_live_profile_is_explicit(tmp_path, monkeypatch, capsys):
    from app.cli import main

    _isolate_serve_cwd(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_CALL_PROFILE", "live")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-printed")
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert main(["serve", "--host", "127.0.0.1"]) == 0
    captured = capsys.readouterr()
    assert "this process can place billable calls" in captured.err
    assert "sk-not-printed" not in captured.err


def test_cli_process_evaluation_refuses_non_loopback(tmp_path, monkeypatch):
    from app.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_CALL_PROFILE", "evaluation")
    assert main(["serve", "--host", "0.0.0.0"]) == 2


def test_cli_malformed_dotenv_fails_before_uvicorn(tmp_path, monkeypatch, capsys):
    from app.cli import main

    _isolate_serve_cwd(tmp_path, monkeypatch)
    (tmp_path / ".env.local").write_text("ALLOWED_COUNTRY_CODES=+1\n", encoding="utf-8")
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert main(["serve", "--profile", "evaluation", "--host", "127.0.0.1"]) == 2
    captured = capsys.readouterr()
    assert "ALLOWED_COUNTRY_CODES" in captured.err
    assert "+1" not in captured.err
