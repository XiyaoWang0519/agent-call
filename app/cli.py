"""Operator CLI: `uv run agent-call <command>` (also `uv run python -m app`)."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from app.doctor import DoctorMode, run_doctor
from app.settings import is_loopback_bind_host


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-call",
        description="Self-hosted Agent Call operator commands.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser(
        "serve",
        help="boot the HTTP/MCP app",
        epilog="Dummy boot: agent-call serve --profile evaluation --host 127.0.0.1",
    )
    serve.add_argument(
        "--profile",
        choices=("live", "evaluation"),
        default=None,
        help="live can place billable calls; evaluation fills dummies "
        "(unset defaults to live). Credentials without an explicit profile "
        "are refused. Dummy boot: --profile evaluation",
    )
    serve.add_argument("--host", default=None, help="bind address (evaluation default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--unsafe-bind",
        action="store_true",
        help="allow a non-loopback bind in evaluation mode (CLI flag only)",
    )

    doctor = sub.add_parser("doctor", help="report missing/invalid prerequisites without values")
    mode = doctor.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dummy", action="store_true", help="evaluation/dummy boot checks")
    mode.add_argument(
        "--prepare-only",
        action="store_true",
        help="dummy boot plus prepare_phone_call policy checks",
    )
    mode.add_argument(
        "--live-ready",
        action="store_true",
        help="real-call prerequisites; never prints secret or E.164 values",
    )

    smoke = sub.add_parser(
        "smoke-prepare",
        help="initialize MCP, list seven tools, prepare a plan; never start a call",
    )
    smoke.add_argument("--base-url", default="http://127.0.0.1:8000")
    smoke.add_argument("--mcp-path", default="/mcp/")

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "serve":
        return _serve(args)
    if args.command == "doctor":
        return _doctor(args)
    return _smoke_prepare(args)


def _serve(args: argparse.Namespace) -> int:
    from pydantic import ValidationError

    from app.settings import Settings, settings_error_check

    if args.profile is not None:
        os.environ["AGENT_CALL_PROFILE"] = str(args.profile)
    try:
        settings = Settings()
    except (ValidationError, ValueError) as exc:
        name, detail = settings_error_check(exc)
        print(f"error: {name}: {detail}", file=sys.stderr)
        return 2

    profile = settings.effective_profile
    unsafe = bool(args.unsafe_bind)
    host = args.host or ("0.0.0.0" if unsafe else "127.0.0.1")
    if profile == "evaluation" and not is_loopback_bind_host(host) and not unsafe:
        print(
            "error: evaluation profile refuses non-loopback bind; "
            "use --host 127.0.0.1 or pass --unsafe-bind",
            file=sys.stderr,
        )
        return 2
    if settings.agent_call_profile is None and settings.has_core_runtime_credentials:
        print(
            "error: live credentials are present; pass --profile live "
            "(this will place billable calls) or set AGENT_CALL_PROFILE=live, "
            "or --profile evaluation for dummy boot",
            file=sys.stderr,
        )
        return 2
    if profile == "live":
        print(
            "warning: live profile; this process can place billable calls",
            file=sys.stderr,
        )

    import uvicorn

    uvicorn.run("app.main:app", host=host, port=args.port, factory=False)
    return 0


def _doctor(args: argparse.Namespace) -> int:
    if args.dummy:
        mode = DoctorMode.DUMMY
    elif args.prepare_only:
        mode = DoctorMode.PREPARE_ONLY
    else:
        mode = DoctorMode.LIVE_READY
    report = run_doctor(mode)
    sys.stdout.write(report.format())
    return 0 if report.complete else 1


def _smoke_prepare(args: argparse.Namespace) -> int:
    from app.smoke_prepare import (
        SmokeTargetError,
        credentials_from_environ,
        run_prepare_only_smoke,
        validate_smoke_target,
    )

    try:
        origin, mcp_path = validate_smoke_target(args.base_url, args.mcp_path)
    except SmokeTargetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a runtime dependency
        print("error: httpx is required for smoke-prepare", file=sys.stderr)
        return 2
    bearer, user_id = credentials_from_environ()
    with httpx.Client(base_url=origin, timeout=15.0, trust_env=False) as client:
        health = client.get("/healthz")
        if health.status_code != 200:
            print("error: healthz returned a non-success status", file=sys.stderr)
            return 1
        result = run_prepare_only_smoke(
            client.post,
            bearer=bearer,
            user_id=user_id,
            mcp_path=mcp_path,
        )
    if not result.ok:
        print(f"error: {result.detail}", file=sys.stderr)
        return 1
    print(f"OK prepare-only smoke (plan {result.plan_id}; start_phone_call not invoked)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
