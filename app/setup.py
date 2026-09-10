"""Local, interactive setup; never contacts providers or starts a call."""

from __future__ import annotations

import getpass
import os
import secrets
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.mcp_oauth.constants import OWNER_SECRET_MIN_LENGTH
from app.mcp_oauth.crypto import hash_owner_secret
from app.settings import Settings, is_e164_phone, is_https_origin


def _secret(prompt: str) -> str:
    # getpass otherwise falls back to echoed stdin if terminal control fails.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


def _ask(
    label: str,
    *,
    secret: bool = False,
    default: str = "",
    optional: bool = False,
    valid: Callable[[str], bool] | None = None,
    hint: str = "Enter a nonempty, single-line value.",
) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = (_secret if secret else input)(f"{label}{suffix}: ").strip() or default
        if optional and not value:
            return ""
        if (
            value
            and not any(ord(char) < 32 for char in value)
            and "${" not in value
            and (valid is None or valid(value))
        ):
            return value
        # Keep validation feedback literal so a future caller cannot accidentally
        # echo a value that originated in a secret-bearing configuration map.
        print("Invalid value; follow the prompt and try again.")


def _origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            is_https_origin(value)
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and parsed.port != 0
        )
    except ValueError:
        return False


def _timezone(value: str) -> bool:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _dotenv(values: dict[str, str]) -> str:
    lines = ["# Private Agent Call configuration. Do not share or commit this file."]
    for key, value in values.items():
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        lines.append(f"{key}='{escaped}'")
    return "\n".join(lines) + "\n"


def run_setup(*, public_url: str | None = None) -> int:
    target = Path.cwd() / ".env.local"
    if any(os.path.lexists(path) for path in (target, Path.cwd() / ".env")):
        print(
            "error: .env or .env.local already exists; edit it or use a new private directory.",
            file=sys.stderr,
        )
        return 2
    if not sys.stdin.isatty():
        print(
            "error: setup requires an interactive terminal to keep secret input hidden.",
            file=sys.stderr,
        )
        return 2
    print("Agent Call setup — keep this directory private; run serve and doctor here.")
    print("Have your OpenAI project and Twilio voice number ready. Exa is optional.")
    print("This writes local configuration only. It does not contact providers or place calls.")
    try:
        values = _collect(public_url=public_url)
        # Validate only the collected configuration, independent of shell/repo credentials.
        Settings.from_environ(values).require_runtime_configuration()
        body = _dotenv(values)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled; no completed configuration written.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, getpass.GetPassWarning):
        print(
            "error: configuration could not be validated or saved; no secret values are shown.",
            file=sys.stderr,
        )
        return 2
    print(f"Saved private configuration to {target} (owner-only permissions on POSIX).")
    print("Keep your owner login password in a password manager; only its hash was saved.")
    if public_url is not None:
        return 0
    print("From this directory: agent-call serve --profile live")
    print("Keep your HTTPS tunnel or reverse proxy pointed at localhost:8000.")
    print("Then, in another terminal here: agent-call doctor --live-ready")
    print(
        "Shell environment variables override this file; doctor checks the effective configuration."
    )
    print("Connect ChatGPT Work or Claude web via OAuth at the /connect/mcp/ path.")
    print("Regular ChatGPT mode is not supported. Sign in with your owner login password.")
    print("Ask your assistant to prepare a plan, review it, and explicitly approve before dialing.")
    print(
        "Readiness checks do not prove webhook delivery or phone audio; the first approved call does."
    )
    return 0


def _collect(*, public_url: str | None = None) -> dict[str, str]:
    values = {
        "AGENT_CALL_PROFILE": "live",
        "PUBLIC_BASE_URL": public_url
        or _ask(
            "Public HTTPS origin (tunnel or hosted instance)",
            valid=_origin,
            hint="Enter an HTTPS origin without credentials, path, query, or fragment.",
        ).rstrip("/"),
        "OPENAI_API_KEY": _ask("OpenAI API key", secret=True),
        "OPENAI_PROJECT_ID": _ask(
            "OpenAI project ID",
            valid=lambda value: value.startswith("proj_"),
            hint="Use the proj_ ID of the project that owns the API key and webhook.",
        ),
    }
    print("In that OpenAI project, add the /webhooks/openai endpoint for this service.")
    print("Subscribe to live.transport.incoming, then copy its signing secret below.")
    values["OPENAI_WEBHOOK_SECRET"] = _ask("OpenAI webhook signing secret", secret=True)
    values["TWILIO_ACCOUNT_SID"] = _ask(
        "Twilio account SID",
        valid=lambda value: (
            len(value) == 34
            and value.startswith("AC")
            and all(char in "0123456789abcdefABCDEF" for char in value[2:])
        ),
        hint="Enter the AC-prefixed 34-character account SID.",
    )
    values["TWILIO_AUTH_TOKEN"] = _ask("Twilio auth token", secret=True)
    values["TWILIO_CALLER_ID"] = _ask(
        "Twilio voice caller number",
        valid=is_e164_phone,
        hint="Use international E.164 format, such as +14165550100.",
    )
    values["OWNER_PHONE_E164"] = _ask(
        "Your owner callback number",
        valid=lambda value: is_e164_phone(value) and value != values["TWILIO_CALLER_ID"],
        hint="Use your E.164 callback number, different from the Twilio caller number.",
    )
    values["OWNER_DISPLAY_NAME"] = _ask("Owner name to give your assistant")
    values["OWNER_TIMEZONE"] = _ask(
        "Owner timezone",
        default="UTC",
        valid=_timezone,
        hint="Use an IANA timezone such as America/Toronto or UTC.",
    )
    values["ALLOWED_AGENT_USER_ID"] = "owner"
    values["EXA_API_KEY"] = _ask(
        "Exa API key (optional; Enter disables web search)", secret=True, optional=True
    )
    print("Choose a sufficiently long owner login phrase for the browser connector.")
    while True:
        password = _ask(
            "Owner login password",
            secret=True,
            valid=lambda value: OWNER_SECRET_MIN_LENGTH <= len(value) <= 1024,
            hint=f"Use between {OWNER_SECRET_MIN_LENGTH} and 1024 characters.",
        )
        if _secret("Confirm owner login password: ") == password:
            break
        print("Passwords did not match; try again.")
    values["MCP_OAUTH_ENABLED"] = "true"
    values["MCP_OAUTH_OWNER_SECRET_HASH"] = hash_owner_secret(password)
    for key in (
        "MCP_BEARER_TOKEN",
        "DEBUG_API_TOKEN",
        "DEPLOY_GUARD_TOKEN",
        "MCP_OAUTH_SIGNING_KEY",
        "MCP_OAUTH_STORAGE_ENCRYPTION_KEY",
    ):
        values[key] = secrets.token_hex(32)
    values["DATABASE_URL"] = "sqlite:///./agent_call.db"
    print(
        "Default destination policy permits +1 numbers. See self-hosting docs for other country prefixes."
    )
    return values
