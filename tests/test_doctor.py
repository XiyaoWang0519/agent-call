from __future__ import annotations

import os
import re
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import httpx

from app.cli import main
from app.doctor import (
    CheckStatus,
    DoctorMode,
    DoctorProbes,
    ProbeResult,
    probe_openai,
    probe_public_origin,
    probe_sqlite,
    probe_twilio,
    run_doctor,
)
from app.evaluation import (
    EVALUATION_MCP_BEARER,
    EVALUATION_OPENAI_KEY,
    EVALUATION_OWNER_PHONE,
    EVALUATION_TWILIO_CALLER_ID,
)

_E164 = re.compile(r"\+[1-9]\d{6,14}")
_SECRETISH = re.compile(
    r"(sk-|whsec_|evaluation-mcp-bearer|evaluation-twilio-auth|"
    r"not-a-real-key|not-a-real-secret)",
    re.IGNORECASE,
)


def _doctor(mode: DoctorMode, environ: dict[str, str]):
    return run_doctor(mode, environ=environ, env_files=())


def _assert_no_secrets(text: str) -> None:
    assert _E164.search(text) is None, text
    assert _SECRETISH.search(text) is None, text
    assert EVALUATION_OPENAI_KEY not in text
    assert EVALUATION_MCP_BEARER not in text
    assert EVALUATION_OWNER_PHONE not in text
    assert EVALUATION_TWILIO_CALLER_ID not in text


def _live_env(**overrides: str) -> dict[str, str]:
    values = {
        "AGENT_CALL_PROFILE": "live",
        "OPENAI_API_KEY": "sk-live-not-printed",
        "OPENAI_WEBHOOK_SECRET": "whsec_live-not-printed",
        "OPENAI_PROJECT_ID": "proj_live",
        "EXA_API_KEY": "exa-live-not-printed",
        "TWILIO_ACCOUNT_SID": "AC" + "2" * 32,
        "TWILIO_AUTH_TOKEN": "twilio-live-not-printed",
        "TWILIO_CALLER_ID": "+14155550199",
        "OWNER_PHONE_E164": "+14155550101",
        "ALLOWED_AGENT_USER_ID": "agent-user-1",
        "MCP_BEARER_TOKEN": "mcp-live-not-printed",
        "DEBUG_API_TOKEN": "debug-live-not-printed",
        "DEPLOY_GUARD_TOKEN": "deploy-live-not-printed",
        "PUBLIC_BASE_URL": "https://example.test",
    }
    values.update(overrides)
    return values


def test_doctor_modes_are_distinct():
    dummy = _doctor(DoctorMode.DUMMY, {})
    prepare = _doctor(DoctorMode.PREPARE_ONLY, {})
    live = _doctor(DoctorMode.LIVE_READY, {})
    assert dummy.ok
    assert prepare.ok
    assert not live.ok
    dummy_names = {check.name for check in dummy.checks}
    prepare_names = {check.name for check in prepare.checks}
    live_names = {check.name for check in live.checks}
    assert "prepare_policy" not in dummy_names
    assert "prepare_policy" in prepare_names
    assert "PUBLIC_BASE_URL_format" not in dummy_names
    assert "PUBLIC_BASE_URL" in live_names
    assert dummy.format().splitlines()[0] != prepare.format().splitlines()[0]
    _assert_no_secrets(dummy.format())
    _assert_no_secrets(prepare.format())
    _assert_no_secrets(live.format())


def test_doctor_dummy_success_does_not_print_values():
    report = _doctor(DoctorMode.DUMMY, {})
    assert report.ok
    text = report.format()
    assert "--dummy" in text
    _assert_no_secrets(text)


def test_doctor_missing_required_live_values():
    report = _doctor(DoctorMode.LIVE_READY, {"AGENT_CALL_PROFILE": "live"})
    assert not report.ok
    names = {check.name for check in report.checks if not check.ok}
    assert "OPENAI_API_KEY" in names
    assert "PUBLIC_BASE_URL" in names
    _assert_no_secrets(report.format())


def test_doctor_blank_required_live_value():
    env = _live_env(OPENAI_API_KEY="  ")
    report = _doctor(DoctorMode.LIVE_READY, env)
    assert not report.ok
    blank = [check for check in report.checks if check.name == "OPENAI_API_KEY"]
    assert blank and blank[0].detail == "blank"
    _assert_no_secrets(report.format())
    assert "sk-live-not-printed" not in report.format()


def test_doctor_malformed_public_base_url():
    env = _live_env(PUBLIC_BASE_URL="http://example.test")
    report = _doctor(DoctorMode.LIVE_READY, env)
    assert not report.ok
    failed = {check.name for check in report.checks if not check.ok}
    assert "PUBLIC_BASE_URL_format" in failed
    assert "http://example.test" not in report.format()
    _assert_no_secrets(report.format())


def test_doctor_malformed_e164():
    env = _live_env(OWNER_PHONE_E164="555-0100")
    report = _doctor(DoctorMode.LIVE_READY, env)
    assert not report.ok
    failed = {check.name for check in report.checks if not check.ok}
    assert "OWNER_PHONE_E164_format" in failed
    assert "555-0100" not in report.format()
    _assert_no_secrets(report.format())


def test_doctor_same_caller_and_owner():
    env = _live_env(TWILIO_CALLER_ID="+14155550101", OWNER_PHONE_E164="+14155550101")
    report = _doctor(DoctorMode.LIVE_READY, env)
    assert not report.ok
    failed = {check.name: check.detail for check in report.checks if not check.ok}
    assert "caller_owner_distinct" in failed
    _assert_no_secrets(report.format())


def _offline_network_probes(*, origin_status: CheckStatus) -> DoctorProbes:
    origin_detail = {
        CheckStatus.PASS: "https health and webhook paths available",
        CheckStatus.FAIL: "origin unreachable",
        CheckStatus.UNVERIFIED: "origin not probed",
    }[origin_status]
    return DoctorProbes(
        public_origin=lambda _origin: ProbeResult(origin_status, origin_detail),
        twilio=lambda _sid, _token: ProbeResult(CheckStatus.PASS, "account metadata reachable"),
        openai=lambda _key: ProbeResult(CheckStatus.PASS, "api metadata reachable"),
    )


def test_doctor_live_ready_success_redacts_values(tmp_path):
    env = _live_env(DATABASE_URL=f"sqlite:///{tmp_path / 'doctor-ok.db'}")
    report = run_doctor(
        DoctorMode.LIVE_READY,
        environ=env,
        env_files=(),
        probes=_offline_network_probes(origin_status=CheckStatus.PASS),
    )
    assert report.ok
    assert not report.complete
    text = report.format()
    _assert_no_secrets(text)
    assert "sk-live-not-printed" not in text
    assert "+14155550101" not in text
    assert "https://example.test" not in text
    assert "UNVERIFIED" in text
    assert "Bearer" not in text


def test_doctor_live_ready_rejects_evaluation_profile():
    report = _doctor(DoctorMode.LIVE_READY, {"AGENT_CALL_PROFILE": "evaluation"})
    assert not report.ok
    assert any(check.name == "profile" and not check.ok for check in report.checks)


def test_shipped_operator_cli_doctor_dummy():
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["AGENT_CALL_PROFILE"] = "evaluation"
    console = Path(sys.prefix) / "bin" / "agent-call"
    assert console.is_file(), f"missing console script {console}"
    commands = (
        [str(console), "doctor", "--dummy"],
        [sys.executable, "-m", "app", "doctor", "--dummy"],
    )
    for cmd in commands:
        result = subprocess.run(
            cmd,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "--dummy" in result.stdout, cmd
        _assert_no_secrets(result.stdout)


def test_doctor_cli_requires_a_mode():
    try:
        main(["doctor"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("doctor without a mode must exit 2")


def test_doctor_reads_quoted_dotenv_without_printing_values(tmp_path: Path):
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "export OPENAI_API_KEY='sk-dotenv-not-printed'",
                'PUBLIC_BASE_URL="https://example.test"',
                "BLANK_LINE_SKIP",
                "OWNER_PHONE_E164=+14155550101",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    report = run_doctor(
        DoctorMode.LIVE_READY,
        environ={"AGENT_CALL_PROFILE": "live"},
        env_files=(env_file,),
    )
    text = report.format()
    assert "sk-dotenv-not-printed" not in text
    assert "+14155550101" not in text
    assert "https://example.test" not in text
    assert any(check.name == "OPENAI_API_KEY" and check.ok for check in report.checks)
    assert any(check.name == "PUBLIC_BASE_URL_format" and check.ok for check in report.checks)


def test_doctor_dummy_rejects_live_profile():
    report = _doctor(DoctorMode.DUMMY, {"AGENT_CALL_PROFILE": "live"})
    assert not report.ok
    assert any(check.name == "profile" and not check.ok for check in report.checks)


def test_doctor_dummy_rejects_malformed_dotenv(tmp_path: Path):
    env_file = tmp_path / ".env.local"
    env_file.write_text("ALLOWED_COUNTRY_CODES=+1\n", encoding="utf-8")
    report = run_doctor(
        DoctorMode.DUMMY,
        environ={"AGENT_CALL_PROFILE": "evaluation"},
        env_files=(env_file,),
    )
    assert not report.ok
    failed = {check.name: check.detail for check in report.checks if not check.ok}
    assert "ALLOWED_COUNTRY_CODES" in failed
    assert "+1" not in report.format()
    _assert_no_secrets(report.format())


def test_doctor_live_ready_reports_missing_oauth_names():
    env = _live_env(GROK_MCP_OAUTH_ENABLED="true")
    report = _doctor(DoctorMode.LIVE_READY, env)
    assert not report.ok
    names = {check.name for check in report.checks if not check.ok}
    assert "GROK_MCP_OAUTH_OWNER_SECRET_HASH" in names
    assert "GROK_MCP_OAUTH_SIGNING_KEY" in names
    assert "GROK_MCP_OAUTH_STORAGE_ENCRYPTION_KEY" in names
    _assert_no_secrets(report.format())


def test_doctor_live_ready_names_oauth_hash_failure():
    env = _live_env(
        GROK_MCP_OAUTH_ENABLED="true",
        GROK_MCP_OAUTH_OWNER_SECRET_HASH="not-an-argon2-hash",
        GROK_MCP_OAUTH_SIGNING_KEY="s" * 64,
        GROK_MCP_OAUTH_STORAGE_ENCRYPTION_KEY="e" * 64,
    )
    report = _doctor(DoctorMode.LIVE_READY, env)
    assert not report.ok
    failed = [check for check in report.checks if not check.ok]
    assert any(check.name == "GROK_MCP_OAUTH_OWNER_SECRET_HASH" for check in failed)
    assert "not-an-argon2-hash" not in report.format()
    _assert_no_secrets(report.format())


def test_from_environ_ignores_dotenv_files(tmp_path: Path, monkeypatch):
    from app.settings import Settings

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.local").write_text("AGENT_CALL_PROFILE=evaluation\n", encoding="utf-8")
    settings = Settings.from_environ({})
    assert settings.agent_call_profile is None
    assert settings.effective_profile == "live"
    assert settings.live_calls_enabled is True
    assert settings.has_core_runtime_credentials is False


def test_unset_profile_sees_mapping_credentials():
    from app.settings import Settings

    settings = Settings.from_environ({"OPENAI_API_KEY": "sk-x"})
    assert settings.agent_call_profile is None
    assert settings.effective_profile == "live"
    assert settings.has_core_runtime_credentials is True


def test_from_values_ignores_process_env(monkeypatch):
    from app.settings import Settings

    monkeypatch.setenv("AGENT_CALL_PROFILE", "evaluation")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-process-not-used")
    settings = Settings.from_values()
    assert settings.agent_call_profile is None
    assert settings.effective_profile == "live"
    assert settings.has_core_runtime_credentials is False


def test_doctor_cli_dummy_isolated(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "app.cli.run_doctor",
        lambda mode: run_doctor(mode, environ={"AGENT_CALL_PROFILE": "evaluation"}, env_files=()),
    )
    code = main(["doctor", "--dummy"])
    captured = capsys.readouterr()
    assert code == 0
    assert "--dummy" in captured.out
    _assert_no_secrets(captured.out)
    code = main(["doctor", "--prepare-only"])
    captured = capsys.readouterr()
    assert code == 0
    assert "--prepare-only" in captured.out
    code = main(["doctor", "--live-ready"])
    captured = capsys.readouterr()
    assert code == 1
    assert "--live-ready" in captured.out


def test_doctor_live_ready_rejects_unusable_sqlite_path():
    env = _live_env(DATABASE_URL="sqlite:////dev/null/agent_call.db")
    report = run_doctor(
        DoctorMode.LIVE_READY,
        environ=env,
        env_files=(),
        probes=_offline_network_probes(origin_status=CheckStatus.PASS),
    )
    assert not report.ok
    failed = {check.name: check.detail for check in report.checks if not check.ok}
    assert "DATABASE_URL" in failed
    text = report.format()
    _assert_no_secrets(text)
    assert "/dev/null" not in text
    assert "sqlite:" not in text
    assert "AC22222222222222222222222222222222" not in text


def test_doctor_live_ready_rejects_invalid_public_origin(tmp_path):
    env = _live_env(
        DATABASE_URL=f"sqlite:///{tmp_path / 'doctor-invalid.db'}",
        PUBLIC_BASE_URL="https://does-not-exist.invalid",
    )

    def origin_probe(origin: str) -> ProbeResult:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Name or service not known", request=request)

        return probe_public_origin(origin, transport=httpx.MockTransport(handler))

    probes = _offline_network_probes(origin_status=CheckStatus.PASS)
    probes.public_origin = origin_probe
    report = run_doctor(
        DoctorMode.LIVE_READY,
        environ=env,
        env_files=(),
        probes=probes,
    )
    assert not report.ok
    failed = {check.name: check.detail for check in report.checks if not check.ok}
    assert "PUBLIC_BASE_URL_reachability" in failed
    text = report.format()
    _assert_no_secrets(text)
    assert "does-not-exist.invalid" not in text
    assert "https://" not in text
    assert "sk-live-not-printed" not in text


def test_doctor_live_ready_mocked_healthy_https_deployment(tmp_path):
    env = _live_env(
        DATABASE_URL=f"sqlite:///{tmp_path / 'doctor-healthy.db'}",
        PUBLIC_BASE_URL="https://calls.example",
    )

    def origin_probe(origin: str) -> ProbeResult:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/healthz":
                return httpx.Response(200, json={"status": "ok"})
            if request.url.path == "/webhooks/openai":
                return httpx.Response(405)
            raise AssertionError(request.url)

        return probe_public_origin(origin, transport=httpx.MockTransport(handler))

    def twilio_probe(sid: str, token: str) -> ProbeResult:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "api.twilio.com"
            return httpx.Response(200, json={"status": "active"})

        return probe_twilio(sid, token, transport=httpx.MockTransport(handler))

    def openai_probe(key: str) -> ProbeResult:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "api.openai.com"
            return httpx.Response(200, json={"data": []})

        return probe_openai(key, transport=httpx.MockTransport(handler))

    report = run_doctor(
        DoctorMode.LIVE_READY,
        environ=env,
        env_files=(),
        probes=DoctorProbes(
            public_origin=origin_probe,
            twilio=twilio_probe,
            openai=openai_probe,
        ),
    )
    assert report.ok
    assert not report.complete
    names = {check.name: check.status for check in report.checks}
    assert names["DATABASE_URL"] is CheckStatus.PASS
    assert names["PUBLIC_BASE_URL_reachability"] is CheckStatus.PASS
    assert names["TWILIO_ACCOUNT"] is CheckStatus.PASS
    assert names["OPENAI_API"] is CheckStatus.PASS
    assert names["EXA_API_KEY"] is CheckStatus.UNVERIFIED
    assert names["OPENAI_WEBHOOK_SECRET"] is CheckStatus.UNVERIFIED
    text = report.format()
    _assert_no_secrets(text)
    assert "calls.example" not in text
    assert "sk-live-not-printed" not in text
    assert "whsec_live-not-printed" not in text
    assert "live-ready: UNVERIFIED" in text


def test_probe_sqlite_rejects_dev_null_parent():
    result = probe_sqlite(Path("/dev/null/agent_call.db"))
    assert result.status is CheckStatus.FAIL
    assert "/dev/null" not in result.detail
    assert "agent_call.db" not in result.detail


def _sqlite_snapshot(path: Path) -> tuple[list[tuple[str, str, str | None]], list[tuple[int, str]]]:
    conn = sqlite3.connect(path)
    try:
        master = list(conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name"))
        rows = list(conn.execute("SELECT id, name FROM items ORDER BY id"))
        return master, rows
    finally:
        conn.close()


def _leftover_probe_paths(directory: Path) -> list[str]:
    leftover: list[str] = []
    for path in directory.iterdir():
        name = path.name
        if name.startswith(".agent-call-doctor-") or name.endswith(("-journal", "-wal", "-shm")):
            leftover.append(name)
    return leftover


def test_probe_sqlite_rejects_directory_path(tmp_path: Path):
    db_dir = tmp_path / "not-a-database"
    db_dir.mkdir()
    result = probe_sqlite(db_dir)
    assert result.status is CheckStatus.FAIL
    assert str(db_dir) not in result.detail
    assert "not-a-database" not in result.detail


def test_probe_sqlite_rejects_corrupt_non_sqlite_file(tmp_path: Path):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite database")
    before = corrupt.read_bytes()
    result = probe_sqlite(corrupt)
    assert result.status is CheckStatus.FAIL
    assert str(corrupt) not in result.detail
    assert "corrupt.db" not in result.detail
    assert corrupt.read_bytes() == before


def test_probe_sqlite_rejects_connect_or_write_failure(tmp_path: Path, monkeypatch):
    existing = tmp_path / "locked.db"
    conn = sqlite3.connect(existing)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()

    def boom(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file /secret/path.db")

    monkeypatch.setattr("sqlite3.connect", boom)
    result = probe_sqlite(existing)
    assert result.status is CheckStatus.FAIL
    assert "/secret/path.db" not in result.detail
    assert "OperationalError" not in result.detail
    assert str(existing) not in result.detail


def test_probe_sqlite_rejects_readonly_existing_database(tmp_path: Path):
    existing = tmp_path / "readonly.db"
    conn = sqlite3.connect(existing)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO items (name) VALUES ('keep-me')")
    conn.commit()
    conn.close()
    existing.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        result = probe_sqlite(existing)
    finally:
        existing.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert result.status is CheckStatus.FAIL
    assert str(existing) not in result.detail
    assert "readonly.db" not in result.detail
    master, rows = _sqlite_snapshot(existing)
    assert rows == [(1, "keep-me")]
    assert any(name == "items" for _type, name, _sql in master)


def test_probe_sqlite_existing_database_unchanged(tmp_path: Path):
    existing = tmp_path / "existing.db"
    conn = sqlite3.connect(existing)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute("CREATE INDEX items_name ON items(name)")
    conn.execute("INSERT INTO items (name) VALUES ('keep-me')")
    conn.execute("INSERT INTO items (name) VALUES ('also-keep')")
    conn.commit()
    conn.close()
    before_bytes = existing.read_bytes()
    before_master, before_rows = _sqlite_snapshot(existing)
    wal = Path(str(existing) + "-wal")
    shm = Path(str(existing) + "-shm")
    journal = Path(str(existing) + "-journal")

    result = probe_sqlite(existing)

    assert result.status is CheckStatus.PASS
    assert str(existing) not in result.detail
    after_master, after_rows = _sqlite_snapshot(existing)
    assert after_master == before_master
    assert after_rows == before_rows
    assert existing.read_bytes() == before_bytes
    assert not wal.exists()
    assert not shm.exists()
    assert not journal.exists()


def test_probe_sqlite_new_database_leaves_no_artifacts(tmp_path: Path):
    parent = tmp_path / "data"
    parent.mkdir()
    target = parent / "agent_call.db"
    before = {path.name for path in parent.iterdir()}

    result = probe_sqlite(target)

    assert result.status is CheckStatus.PASS
    assert str(target) not in result.detail
    assert "agent_call.db" not in result.detail
    assert not target.exists()
    assert {path.name for path in parent.iterdir()} == before
    assert _leftover_probe_paths(parent) == []
    assert _leftover_probe_paths(tmp_path) == []


def test_probe_sqlite_zero_byte_file_behaves_like_new_location(tmp_path: Path):
    parent = tmp_path / "data"
    parent.mkdir()
    target = parent / "agent_call.db"
    target.write_bytes(b"")
    before = target.read_bytes()

    result = probe_sqlite(target)

    assert result.status is CheckStatus.PASS
    assert str(target) not in result.detail
    assert "agent_call.db" not in result.detail
    assert target.exists()
    assert target.read_bytes() == before
    assert _leftover_probe_paths(parent) == []
    assert _leftover_probe_paths(tmp_path) == []


def test_probe_sqlite_zero_byte_file_rejects_write_open_failure(tmp_path: Path, monkeypatch):
    target = tmp_path / "empty.db"
    target.touch()
    original_open = Path.open

    def guarded_open(path, mode="r", *args, **kwargs):
        if path == target and mode == "r+b":
            raise PermissionError("private path must not be printed")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    result = probe_sqlite(target)
    assert result.status is CheckStatus.FAIL
    assert result.detail == "database is not writable"
    assert target.read_bytes() == b""
    assert _leftover_probe_paths(tmp_path) == []


def test_probe_sqlite_missing_parent_is_conservative_failure(tmp_path: Path):
    target = tmp_path / "missing-parent" / "agent_call.db"
    result = probe_sqlite(target)
    assert result.status is CheckStatus.FAIL
    assert str(target) not in result.detail
    assert "missing-parent" not in result.detail
    assert not target.exists()
    assert not target.parent.exists()


def test_doctor_live_ready_rejects_directory_database_and_redacts(tmp_path: Path):
    db_dir = tmp_path / "db-as-dir"
    db_dir.mkdir()
    env = _live_env(DATABASE_URL=f"sqlite:///{db_dir}")
    report = run_doctor(
        DoctorMode.LIVE_READY,
        environ=env,
        env_files=(),
        probes=_offline_network_probes(origin_status=CheckStatus.PASS),
    )
    assert not report.ok
    failed = {check.name: check.detail for check in report.checks if not check.ok}
    assert "DATABASE_URL" in failed
    text = report.format()
    _assert_no_secrets(text)
    assert str(db_dir) not in text
    assert "db-as-dir" not in text
    assert "sqlite:" not in text


def test_probe_public_origin_connect_error_is_value_free():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known", request=request)

    result = probe_public_origin(
        "https://does-not-exist.invalid",
        transport=httpx.MockTransport(handler),
    )
    assert result.status is CheckStatus.FAIL
    assert result.detail == "origin unreachable"
    assert "invalid" not in result.detail
    assert "https://" not in result.detail
