from __future__ import annotations

import contextlib
import socket
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from dotenv import dotenv_values
from pydantic import SecretStr

from app import local_start
from app.settings import Settings


@pytest.fixture
def config_values(oauth_settings):
    names = (
        "agent_call_profile public_base_url database_url openai_api_key "
        "openai_project_id openai_webhook_secret twilio_account_sid twilio_auth_token "
        "twilio_caller_id owner_phone_e164 allowed_agent_user_id mcp_bearer_token "
        "debug_api_token deploy_guard_token mcp_oauth_enabled mcp_oauth_owner_secret_hash "
        "mcp_oauth_signing_key mcp_oauth_storage_encryption_key"
    ).split()
    values = {}
    for name in names:
        value = getattr(oauth_settings, name)
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        values[name.upper()] = str(value)
    return values


def write_config(path, values):
    path.write_text("".join(f"{key}='{value}'\n" for key, value in values.items()))


@pytest.fixture
def fake_tunnel(monkeypatch):
    tunnel = Mock()
    tunnel.url = "https://new.example.test"
    tunnel.is_running = True
    tunnel.__enter__ = Mock(return_value=tunnel)
    tunnel.__exit__ = Mock(return_value=False)
    factory = Mock(return_value=tunnel)
    monkeypatch.setattr(local_start, "QuickTunnel", factory)
    return factory, tunnel


@pytest.fixture
def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_fresh_start_passes_tunnel_url_to_setup(
    tmp_path, monkeypatch, fake_tunnel, free_port, config_values
):
    monkeypatch.setattr(local_start.sys.stdin, "isatty", lambda: True)
    old_cwd = Path.cwd()

    def setup(*, public_url):
        assert Path.cwd() == tmp_path
        assert public_url == fake_tunnel[1].url
        write_config(tmp_path / ".env.local", {**config_values, "PUBLIC_BASE_URL": public_url})
        return 0

    monkeypatch.setattr(local_start, "run_setup", setup)
    serve = Mock(return_value=0)
    monkeypatch.setattr(local_start, "_serve", serve)
    assert local_start.run_local(directory=tmp_path, port=free_port, profile="live") == 0
    assert serve.call_args.args[0]["PUBLIC_BASE_URL"] == fake_tunnel[1].url
    assert Path.cwd() == old_cwd
    fake_tunnel[1].__exit__.assert_called_once()


def test_existing_config_changes_only_public_url(
    tmp_path, monkeypatch, fake_tunnel, free_port, config_values
):
    path = tmp_path / ".env.local"
    write_config(path, config_values)
    setup = Mock(side_effect=AssertionError("existing setup must not rerun"))
    monkeypatch.setattr(local_start, "run_setup", setup)
    monkeypatch.setattr(local_start, "_serve", Mock(return_value=0))
    assert local_start.run_local(directory=tmp_path, port=free_port, profile="live") == 0
    assert dotenv_values(path) == {**config_values, "PUBLIC_BASE_URL": fake_tunnel[1].url}
    setup.assert_not_called()


def test_active_database_prevents_tunnel_and_url_change(
    tmp_path, fake_tunnel, free_port, config_values
):
    path = tmp_path / ".env.local"
    write_config(path, config_values)
    original = path.read_bytes()
    db_path = Settings.from_environ(config_values).database_path
    with contextlib.closing(sqlite3.connect(db_path)) as db:
        db.execute("CREATE TABLE calls (state TEXT)")
        db.execute("INSERT INTO calls VALUES ('in_progress')")
        db.commit()
    assert local_start.run_local(directory=tmp_path, port=free_port, profile="live") == 2
    fake_tunnel[0].assert_not_called()
    assert path.read_bytes() == original


def test_occupied_port_prevents_tunnel(tmp_path, fake_tunnel, config_values):
    write_config(tmp_path / ".env.local", config_values)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        assert (
            local_start.run_local(
                directory=tmp_path, port=occupied.getsockname()[1], profile="live"
            )
            == 2
        )
    fake_tunnel[0].assert_not_called()


def test_noninteractive_first_start_does_not_download(
    tmp_path, monkeypatch, fake_tunnel, free_port
):
    monkeypatch.setattr(local_start.sys.stdin, "isatty", lambda: False)
    assert local_start.run_local(directory=tmp_path, port=free_port, profile="live") == 2
    fake_tunnel[0].assert_not_called()
    assert not (tmp_path / ".env.local").exists()


def test_child_environment_excludes_ambient_settings(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-instance")
    monkeypatch.setenv("EXA_API_KEY", "ambient-search")
    monkeypatch.setenv("public_base_url", "https://wrong.example.test")
    monkeypatch.setenv("PATH", "/test/bin")
    env = local_start._child_environment({"OPENAI_API_KEY": "chosen-instance"}, "evaluation")
    assert env["OPENAI_API_KEY"] == "chosen-instance"
    assert "EXA_API_KEY" not in env
    assert "public_base_url" not in env
    assert env["PATH"] == "/test/bin"
    assert env["AGENT_CALL_PROFILE"] == "evaluation"


def test_ctrl_c_waits_for_idle_before_terminating(monkeypatch, config_values):
    monkeypatch.setattr(local_start, "_public_ready", lambda _: True)
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(local_start.subprocess, "Popen", Mock(return_value=process))
    requests = []

    def handler(request):
        if request.url.path == "/healthz":
            return httpx.Response(200)
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {config_values['DEPLOY_GUARD_TOKEN']}"
        process.terminate.assert_not_called()
        return httpx.Response(409 if len(requests) == 1 else 200)

    client = httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(local_start.httpx, "Client", lambda **_: client)
    sleeps = 0

    def interrupt(_):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            process.terminate.assert_not_called()
            assert len(requests) == 1
        raise KeyboardInterrupt

    monkeypatch.setattr(local_start.time, "sleep", interrupt)
    assert local_start._serve(config_values, 8000, "live", SimpleNamespace(is_running=True)) == 0
    assert [r.url.path for r in requests] == ["/internal/deployment-lock"] * 2
    process.terminate.assert_called_once()
    process.kill.assert_not_called()


def test_unhealthy_startup_terminates_child(monkeypatch, config_values):
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(local_start.subprocess, "Popen", Mock(return_value=process))
    client = httpx.Client(
        base_url="http://localhost",
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    )
    monkeypatch.setattr(local_start.httpx, "Client", lambda **_: client)
    times = iter([0, 31])
    monkeypatch.setattr(local_start.time, "monotonic", lambda: next(times))
    assert local_start._serve(config_values, 8000, "live", SimpleNamespace(is_running=True)) == 1
    process.terminate.assert_called_once()


def test_setup_failure_closes_tunnel(tmp_path, monkeypatch, fake_tunnel, free_port):
    monkeypatch.setattr(local_start.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(local_start, "run_setup", lambda **_: 2)
    assert local_start.run_local(directory=tmp_path, port=free_port, profile="live") == 2
    fake_tunnel[1].__exit__.assert_called_once()


def test_unexpected_monitor_failure_waits_for_idle_before_terminating(monkeypatch, config_values):
    monkeypatch.setattr(local_start, "_public_ready", lambda _: True)
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(local_start.subprocess, "Popen", Mock(return_value=process))
    requests = []

    def handler(request):
        if request.url.path == "/healthz":
            return httpx.Response(200)
        requests.append(request)
        assert request.url.path == "/internal/deployment-lock"
        assert request.headers["Authorization"] == f"Bearer {config_values['DEPLOY_GUARD_TOKEN']}"
        process.terminate.assert_not_called()
        return httpx.Response(409 if len(requests) == 1 else 200)

    client_type = httpx.Client
    monkeypatch.setattr(
        local_start.httpx,
        "Client",
        lambda **_: client_type(
            base_url="http://localhost", transport=httpx.MockTransport(handler)
        ),
    )
    sleeps = []

    def fail_monitor_then_wait(delay):
        sleeps.append(delay)
        process.terminate.assert_not_called()
        if len(sleeps) == 1:
            raise RuntimeError("monitor failed")
        assert len(requests) == 1

    monkeypatch.setattr(local_start.time, "sleep", fail_monitor_then_wait)
    with pytest.raises(RuntimeError, match="monitor failed"):
        local_start._serve(config_values, 8000, "live", SimpleNamespace(is_running=True))
    assert len(requests) == 2
    assert sleeps == [0.5, 1]
    process.terminate.assert_called_once()
    process.kill.assert_not_called()


def test_public_health_requires_expected_json(monkeypatch):
    responses = iter(
        [httpx.Response(200, text="interstitial"), httpx.Response(200, json={"status": "ok"})]
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda _: next(responses)))
    monkeypatch.setattr(local_start.httpx, "Client", lambda **_: client)
    monkeypatch.setattr(local_start.time, "sleep", lambda _: None)
    assert local_start._public_ready("https://trial.trycloudflare.com")
