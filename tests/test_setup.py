from __future__ import annotations

import os
import stat

import pytest
from dotenv import dotenv_values

from app.cli import main
from app.mcp_oauth.crypto import verify_owner_secret
from app.settings import Settings


def inputs(monkeypatch, *, exa=""):
    plain = iter(
        [
            "https://calls.example.test",
            "proj_example",
            "AC" + "1" * 32,
            "+14165550100",
            "+14165550101",
            "Owner's \\ name",
            "UTC",
        ]
    )
    secret = iter(
        [
            "sk-test-private",
            "whsec_private",
            "twilio-private",
            exa,
            "owner-password-private",
            "owner-password-private",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(plain))
    monkeypatch.setattr("app.setup.getpass.getpass", lambda _: next(secret))
    monkeypatch.setattr("app.setup.sys.stdin.isatty", lambda: True)


@pytest.mark.parametrize("exa", ["", "exa-private"])
def test_setup_writes_loadable_private_config_without_secret_output(
    tmp_path, monkeypatch, capsys, exa
):
    monkeypatch.chdir(tmp_path)
    inputs(monkeypatch, exa=exa)
    assert main(["setup"]) == 0
    config = tmp_path / ".env.local"
    values = dotenv_values(config)
    settings = Settings.from_environ({k: v for k, v in values.items() if v is not None})
    settings.require_runtime_configuration()
    assert settings.live_calls_enabled
    assert settings.mcp_oauth_enabled
    assert settings.owner_display_name == "Owner's \\ name"
    assert values["EXA_API_KEY"] == exa
    assert verify_owner_secret(
        secret="owner-password-private", secret_hash=values["MCP_OAUTH_OWNER_SECRET_HASH"]
    )
    tokens = [
        values[k]
        for k in (
            "MCP_BEARER_TOKEN",
            "DEBUG_API_TOKEN",
            "DEPLOY_GUARD_TOKEN",
            "MCP_OAUTH_SIGNING_KEY",
            "MCP_OAUTH_STORAGE_ENCRYPTION_KEY",
        )
    ]
    assert len(set(tokens)) == 5
    if os.name == "posix":
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
    out = capsys.readouterr()
    assert "owner-password-private" not in config.read_text()
    for value in [
        "sk-test-private",
        "whsec_private",
        "twilio-private",
        "owner-password-private",
        *tokens,
    ]:
        assert value not in out.out + out.err
    assert "/connect/mcp/" in out.out
    assert not (tmp_path / "agent_call.db").exists()


@pytest.mark.parametrize("name", [".env", ".env.local"])
def test_setup_refuses_existing_configuration(tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    (tmp_path / name).write_text("keep me")
    assert main(["setup"]) == 2
    assert (tmp_path / name).read_text() == "keep me"


def test_setup_refuses_symlink(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.local").symlink_to(tmp_path / "absent")
    assert main(["setup"]) == 2
    assert not (tmp_path / "absent").exists()


def test_setup_requires_terminal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("app.setup.sys.stdin.isatty", lambda: False)
    assert main(["setup"]) == 2
    assert not (tmp_path / ".env.local").exists()


def test_setup_cancel_keeps_directory_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("app.setup.sys.stdin.isatty", lambda: True)

    def cancel(_):
        raise EOFError

    monkeypatch.setattr("builtins.input", cancel)
    assert main(["setup"]) == 130
    assert not (tmp_path / ".env.local").exists()


def test_prompt_rejects_dotenv_injection(monkeypatch):
    from app.setup import _ask

    answers = iter(["${HOME}", "hello\nMCP_OAUTH_ENABLED=false", "safe"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert _ask("test") == "safe"


@pytest.mark.parametrize(
    "value",
    [
        "http://host",
        "https://user:pass@host",
        "https://host/path",
        "https://host?x=1",
        "https://host:bad",
        "https://[broken",
    ],
)
def test_setup_rejects_invalid_public_origin(value):
    from app.setup import _origin

    assert not _origin(value)


def test_setup_aborts_if_hidden_input_unavailable(tmp_path, monkeypatch, capsys):
    import getpass
    import warnings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("app.setup.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "https://calls.example.test")

    def unavailable(_):
        warnings.warn("Cannot control echo on the terminal", getpass.GetPassWarning, stacklevel=2)
        pytest.fail("Must abort before falling back to echoed input")

    monkeypatch.setattr("app.setup.getpass.getpass", unavailable)
    assert main(["setup"]) == 2
    assert not (tmp_path / ".env.local").exists()
    assert "no secret values" in capsys.readouterr().err
