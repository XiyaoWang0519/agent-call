"""Readiness checks that never print secret values or full phone numbers."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import httpx
from dotenv import dotenv_values
from pydantic import ValidationError

from app.evaluation import EVALUATION_TARGET_PHONE, evaluation_prepare_packet
from app.policy import validate_context
from app.settings import (
    CORE_RUNTIME_ENV_NAMES,
    OAUTH_RUNTIME_ENV_NAMES,
    Settings,
    is_e164_phone,
    is_https_origin,
    settings_error_check,
)

_TRUE_ENV_FLAGS = frozenset({"1", "true", "yes", "on", "y"})
_PROBE_TIMEOUT_SECONDS = 5.0
_SQLITE_PROBE_TIMEOUT_SECONDS = 1.0
_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_PROBE_PREFIX = ".agent-call-doctor-"
_HEALTH_PATH = "/healthz"
_WEBHOOK_PATH = "/webhooks/openai"
_OPENAI_MODELS_URL = "https://api.openai.com/v1/models"


class DoctorMode(StrEnum):
    DUMMY = "dummy"
    PREPARE_ONLY = "prepare-only"
    LIVE_READY = "live-ready"


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: CheckStatus
    detail: str


@dataclass(slots=True)
class DoctorProbes:
    sqlite: Callable[[Path], ProbeResult] | None = None
    public_origin: Callable[[str], ProbeResult] | None = None
    twilio: Callable[[str, str], ProbeResult] | None = None
    openai: Callable[[str], ProbeResult] | None = None
    exa: Callable[[str], ProbeResult] | None = None
    openai_webhook: Callable[[str], ProbeResult] | None = None


@dataclass(slots=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    detail: str

    @property
    def ok(self) -> bool:
        return self.status is not CheckStatus.FAIL


@dataclass(slots=True)
class DoctorReport:
    mode: DoctorMode
    checks: list[DoctorCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.status is not CheckStatus.FAIL for check in self.checks)

    @property
    def complete(self) -> bool:
        return all(check.status is CheckStatus.PASS for check in self.checks)

    def add(self, name: str, ok: bool | CheckStatus, detail: str) -> None:
        if isinstance(ok, CheckStatus):
            status = ok
        else:
            status = CheckStatus.PASS if ok else CheckStatus.FAIL
        self.checks.append(DoctorCheck(name=name, status=status, detail=detail))

    def format(self) -> str:
        lines = [f"agent-call doctor --{self.mode}"]
        for check in self.checks:
            lines.append(f"{check.status} {check.name}: {check.detail}")
        if not self.ok:
            outcome = "FAILED"
        elif not self.complete:
            outcome = "UNVERIFIED"
        else:
            outcome = "OK"
        failed = sum(1 for check in self.checks if check.status is CheckStatus.FAIL)
        unverified = sum(1 for check in self.checks if check.status is CheckStatus.UNVERIFIED)
        lines.append(
            f"{self.mode}: {outcome} ({failed} failed, {unverified} unverified, "
            f"{len(self.checks)} checks)"
        )
        return "\n".join(lines) + "\n"


def collect_environ(
    *,
    environ: Mapping[str, str] | None = None,
    env_files: tuple[Path, ...] | None = None,
    cwd: Path | None = None,
) -> dict[str, str]:
    merged: dict[str, str] = {}
    if env_files is None:
        root = cwd or Path.cwd()
        env_files = (root / ".env", root / ".env.local")
    for path in env_files:
        if path.is_file():
            for key, value in dotenv_values(path).items():
                if key and value is not None:
                    merged[key] = value
    source = os.environ if environ is None else environ
    for key, value in source.items():
        merged[key] = value
    return merged


def _presence_check(report: DoctorReport, environ: Mapping[str, str], name: str) -> str | None:
    if name not in environ:
        report.add(name, False, "missing")
        return None
    value = environ[name]
    if not value.strip():
        report.add(name, False, "blank")
        return None
    return value


def run_doctor(
    mode: DoctorMode | str,
    *,
    environ: Mapping[str, str] | None = None,
    env_files: tuple[Path, ...] | None = None,
    cwd: Path | None = None,
    probes: DoctorProbes | None = None,
) -> DoctorReport:
    resolved = DoctorMode(mode)
    env = collect_environ(environ=environ, env_files=env_files, cwd=cwd)
    report = DoctorReport(mode=resolved)
    if resolved is DoctorMode.DUMMY:
        _check_dummy(report, env)
    elif resolved is DoctorMode.PREPARE_ONLY:
        _check_prepare_only(report, env)
    else:
        _check_live_ready(report, env, probes or DoctorProbes())
    return report


def _flag_enabled(merged: Mapping[str, str], name: str) -> bool:
    return (merged.get(name) or "").strip().lower() in _TRUE_ENV_FLAGS


def _record_settings_failure(report: DoctorReport, exc: BaseException) -> None:
    name, detail = settings_error_check(exc)
    report.add(name, False, detail)


def _check_dummy(report: DoctorReport, merged: Mapping[str, str]) -> Settings | None:
    explicit = (merged.get("AGENT_CALL_PROFILE") or "").strip()
    if explicit == "live":
        report.add(
            "profile",
            False,
            "dummy mode expects AGENT_CALL_PROFILE=evaluation (or unset)",
        )
        return None
    report.add("profile", True, "evaluation")
    try:
        settings = Settings.from_environ(merged, agent_call_profile="evaluation")
        settings.require_runtime_configuration()
    except (ValidationError, RuntimeError, ValueError) as exc:
        _record_settings_failure(report, exc)
        return None
    report.add("evaluation_boot", True, "required fields filled for dummy boot")
    report.add(
        "live_calls",
        not settings.live_calls_enabled,
        "start_phone_call returns live_calls_disabled",
    )
    return settings


def _check_prepare_only(report: DoctorReport, merged: Mapping[str, str]) -> None:
    settings = _check_dummy(report, merged)
    if settings is None or not report.ok:
        return
    if settings.twilio_caller_id == settings.owner_phone_e164:
        report.add("caller_owner_distinct", False, "caller id matches owner phone")
        return
    report.add("caller_owner_distinct", True, "caller id and owner phone differ")
    packet = evaluation_prepare_packet(
        owner_phone=settings.owner_phone_e164 or "",
        owner_display_name=settings.owner_display_name,
        owner_timezone=settings.owner_timezone,
        target_phone=EVALUATION_TARGET_PHONE,
    )
    errors = validate_context(packet, settings)
    if errors:
        codes = ",".join(sorted({error.code for error in errors}))
        report.add("prepare_policy", False, f"sample evaluation destination rejected ({codes})")
        return
    report.add("prepare_policy", True, "sample evaluation destination is allowed")


def _check_live_ready(
    report: DoctorReport, merged: Mapping[str, str], probes: DoctorProbes
) -> None:
    profile = (merged.get("AGENT_CALL_PROFILE") or "live").strip() or "live"
    if profile == "evaluation":
        report.add(
            "profile",
            False,
            "evaluation profile disables live calls; use live for --live-ready",
        )
        return
    report.add("profile", True, "live")
    required_names = CORE_RUNTIME_ENV_NAMES
    if _flag_enabled(merged, "GROK_MCP_OAUTH_ENABLED"):
        required_names = CORE_RUNTIME_ENV_NAMES + OAUTH_RUNTIME_ENV_NAMES
    values: dict[str, str] = {}
    for name in required_names:
        present = _presence_check(report, merged, name)
        if present is not None:
            values[name] = present
            report.add(name, True, "present")
    public_base = values.get("PUBLIC_BASE_URL")
    if public_base is not None:
        if is_https_origin(public_base):
            report.add("PUBLIC_BASE_URL_format", True, "https origin")
        else:
            report.add(
                "PUBLIC_BASE_URL_format",
                False,
                "must be an HTTPS origin without a path or query",
            )
    caller = values.get("TWILIO_CALLER_ID")
    owner = values.get("OWNER_PHONE_E164")
    if caller is not None:
        if is_e164_phone(caller):
            report.add("TWILIO_CALLER_ID_format", True, "E.164")
        else:
            report.add("TWILIO_CALLER_ID_format", False, "must be E.164")
    if owner is not None:
        if is_e164_phone(owner):
            report.add("OWNER_PHONE_E164_format", True, "E.164")
        else:
            report.add("OWNER_PHONE_E164_format", False, "must be E.164")
    if caller is not None and owner is not None and caller == owner:
        report.add(
            "caller_owner_distinct",
            False,
            "caller id matches owner phone; the service cannot call its own number",
        )
    elif caller is not None and owner is not None:
        report.add("caller_owner_distinct", True, "caller id and owner phone differ")
    if not report.ok:
        return
    try:
        settings = Settings.from_environ(merged)
        settings.require_runtime_configuration()
    except (ValidationError, RuntimeError, ValueError) as exc:
        _record_settings_failure(report, exc)
        return
    sqlite_probe = probes.sqlite or probe_sqlite
    _record_probe(report, "DATABASE_URL", sqlite_probe(settings.database_path))
    origin = settings.public_base_url or ""
    origin_probe = probes.public_origin or probe_public_origin
    _record_probe(report, "PUBLIC_BASE_URL_reachability", origin_probe(origin))
    sid = settings.twilio_account_sid or ""
    twilio_token = Settings.reveal(settings.twilio_auth_token)
    twilio_probe = probes.twilio or probe_twilio
    _record_probe(report, "TWILIO_ACCOUNT", twilio_probe(sid, twilio_token))
    openai_key = Settings.reveal(settings.openai_api_key)
    openai_probe = probes.openai or probe_openai
    _record_probe(report, "OPENAI_API", openai_probe(openai_key))
    exa_key = Settings.reveal(settings.exa_api_key)
    exa_probe = probes.exa or probe_exa_unverified
    _record_probe(report, "EXA_API_KEY", exa_probe(exa_key))
    webhook = Settings.reveal(settings.openai_webhook_secret)
    webhook_probe = probes.openai_webhook or probe_openai_webhook_unverified
    _record_probe(report, "OPENAI_WEBHOOK_SECRET", webhook_probe(webhook))


def _record_probe(report: DoctorReport, name: str, result: ProbeResult) -> None:
    report.add(name, result.status, result.detail)


def _remove_sqlite_artifacts(probe_name: str) -> bool:
    if not probe_name:
        return True
    removed = True
    for suffix in ("", "-journal", "-wal", "-shm"):
        artifact = probe_name + suffix
        try:
            os.unlink(artifact)
        except FileNotFoundError:
            continue
        except OSError:
            removed = False
    return removed


def _close_sqlite(conn: sqlite3.Connection | None) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except sqlite3.Error:
        pass


def _probe_existing_sqlite(db_path: Path) -> ProbeResult:
    try:
        info = db_path.lstat()
    except OSError:
        return ProbeResult(CheckStatus.FAIL, "database is not writable")
    if not stat.S_ISREG(info.st_mode):
        return ProbeResult(CheckStatus.FAIL, "database path is not a regular SQLite file")
    if info.st_size == 0:
        # sqlite3.connect would write a header into an empty file. Probe the
        # parent like a new location and leave the zero-byte file untouched.
        return _probe_new_sqlite_location(db_path)
    try:
        with db_path.open("rb") as handle:
            header = handle.read(len(_SQLITE_HEADER))
    except OSError:
        return ProbeResult(CheckStatus.FAIL, "database is not writable")
    if header != _SQLITE_HEADER:
        return ProbeResult(CheckStatus.FAIL, "database path is not a regular SQLite file")
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            str(db_path),
            timeout=_SQLITE_PROBE_TIMEOUT_SECONDS,
            isolation_level=None,
        )
        conn.execute("BEGIN IMMEDIATE")
        table = f"__agent_call_doctor_probe_{os.getpid()}_{id(conn):x}__"
        conn.execute(f'CREATE TABLE "{table}" (x INTEGER NOT NULL)')
        conn.execute(f'INSERT INTO "{table}" (x) VALUES (1)')
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        if conn is not None:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        return ProbeResult(CheckStatus.FAIL, "database is not writable")
    finally:
        _close_sqlite(conn)
    return ProbeResult(CheckStatus.PASS, "database exists and is writable")


def _probe_new_sqlite_location(db_path: Path) -> ProbeResult:
    parent = db_path.parent
    if parent.exists() and not parent.is_dir():
        return ProbeResult(CheckStatus.FAIL, "database parent is not a directory")
    if not parent.is_dir():
        return ProbeResult(CheckStatus.FAIL, "database parent cannot be created")
    handle: int | None = None
    probe_name = ""
    conn: sqlite3.Connection | None = None
    failure: ProbeResult | None = None
    try:
        handle, probe_name = tempfile.mkstemp(prefix=_SQLITE_PROBE_PREFIX, suffix=".db", dir=parent)
        os.close(handle)
        handle = None
        conn = sqlite3.connect(
            probe_name,
            timeout=_SQLITE_PROBE_TIMEOUT_SECONDS,
            isolation_level=None,
        )
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE probe (id INTEGER NOT NULL)")
        conn.execute("INSERT INTO probe (id) VALUES (1)")
        conn.execute("COMMIT")
    except (OSError, sqlite3.Error):
        failure = ProbeResult(CheckStatus.FAIL, "data location is not writable")
    finally:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        _close_sqlite(conn)
        if not _remove_sqlite_artifacts(probe_name):
            failure = ProbeResult(CheckStatus.FAIL, "data location is not writable")
    if failure is not None:
        return failure
    return ProbeResult(CheckStatus.PASS, "database parent exists and is writable")


def probe_sqlite(db_path: Path) -> ProbeResult:
    try:
        if db_path.exists() or db_path.is_symlink():
            return _probe_existing_sqlite(db_path)
        return _probe_new_sqlite_location(db_path)
    except OSError:
        return ProbeResult(CheckStatus.FAIL, "database is not writable")


def _http_client(*, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=_PROBE_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )


def probe_public_origin(
    origin: str, *, transport: httpx.BaseTransport | None = None
) -> ProbeResult:
    health_url = origin.rstrip("/") + _HEALTH_PATH
    webhook_url = origin.rstrip("/") + _WEBHOOK_PATH
    try:
        with _http_client(transport=transport) as client:
            health = client.get(health_url)
            if health.status_code != 200:
                return ProbeResult(CheckStatus.FAIL, "health path unavailable")
            webhook = client.get(webhook_url)
            if webhook.status_code == 404:
                return ProbeResult(CheckStatus.FAIL, "webhook path unavailable")
    except httpx.ConnectError:
        return ProbeResult(CheckStatus.FAIL, "origin unreachable")
    except httpx.TimeoutException:
        return ProbeResult(CheckStatus.FAIL, "origin timed out")
    except httpx.HTTPError:
        return ProbeResult(CheckStatus.FAIL, "origin unreachable")
    return ProbeResult(CheckStatus.PASS, "https health and webhook paths available")


def probe_twilio(
    account_sid: str,
    auth_token: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ProbeResult:
    request_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}.json"
    try:
        with _http_client(transport=transport) as client:
            response = client.get(request_url, auth=(account_sid, auth_token))
    except httpx.HTTPError:
        return ProbeResult(CheckStatus.FAIL, "account metadata unreachable")
    if response.status_code in {401, 403}:
        return ProbeResult(CheckStatus.FAIL, "authentication failed")
    if response.status_code >= 400:
        return ProbeResult(CheckStatus.FAIL, "account metadata unavailable")
    return ProbeResult(CheckStatus.PASS, "account metadata reachable")


def probe_openai(api_key: str, *, transport: httpx.BaseTransport | None = None) -> ProbeResult:
    try:
        with _http_client(transport=transport) as client:
            response = client.get(
                _OPENAI_MODELS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError:
        return ProbeResult(CheckStatus.FAIL, "api metadata unreachable")
    if response.status_code in {401, 403}:
        return ProbeResult(CheckStatus.FAIL, "authentication failed")
    if response.status_code >= 400:
        return ProbeResult(CheckStatus.FAIL, "api metadata unavailable")
    return ProbeResult(CheckStatus.PASS, "api metadata reachable")


def probe_exa_unverified(_api_key: str) -> ProbeResult:
    return ProbeResult(
        CheckStatus.UNVERIFIED,
        "no non-billable connectivity check; search would incur usage",
    )


def probe_openai_webhook_unverified(_secret: str) -> ProbeResult:
    return ProbeResult(
        CheckStatus.UNVERIFIED,
        "no side-effect-free check; signing is verified on inbound webhooks",
    )
