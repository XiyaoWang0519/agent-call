"""One foreground command for a private local instance and managed HTTPS tunnel."""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

import httpx
from dotenv import dotenv_values, set_key
from filelock import FileLock, Timeout

from app.models import TERMINAL_STATES
from app.settings import Settings
from app.setup import run_setup
from app.tunnel import QuickTunnel, TunnelError


@contextlib.contextmanager
def _working_directory(directory: Path) -> Iterator[None]:
    old = Path.cwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(old)


def _config(path: Path) -> dict[str, str]:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("configuration must be a regular file")
    return {
        key: value
        for key, value in dotenv_values(path, interpolate=False).items()
        if value is not None
    }


def _idle_database(settings: Settings) -> bool:
    path = settings.database_path.resolve()
    if not path.exists():
        return True
    # Read only: do not initialize or repair a database in a launcher.
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='calls'"
        ).fetchone()
        if not exists:
            return True
        states = tuple(state.value for state in TERMINAL_STATES)
        placeholders = ",".join("?" for _ in states)
        count = db.execute(
            f"SELECT COUNT(*) FROM calls WHERE state NOT IN ({placeholders})", states
        ).fetchone()
        return bool(count is not None and count[0] == 0)


def _child_environment(values: Mapping[str, str], profile: str) -> dict[str, str]:
    fields = {name.upper() for name in Settings.model_fields}
    # Managed startup belongs to its private config, never another instance's shell credentials.
    env = {key: value for key, value in os.environ.items() if key.upper() not in fields}
    env.update(values)
    env["AGENT_CALL_PROFILE"] = profile
    return env


def _stop_allowed(client: httpx.Client, token: str) -> bool:
    try:
        response = client.post(
            "/internal/deployment-lock", headers={"Authorization": f"Bearer {token}"}
        )
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def _public_ready(url: str) -> bool:
    deadline = time.monotonic() + 90
    with httpx.Client(timeout=3, trust_env=False) as public:
        while time.monotonic() < deadline:
            try:
                response = public.get(url + "/healthz")
                if response.status_code == 200 and response.json() == {"status": "ok"}:
                    return True
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(1)
    return False


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, signal.SIG_IGN)
    try:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _serve(values: dict[str, str], port: int, profile: str, tunnel: QuickTunnel) -> int:
    env = _child_environment(values, profile)
    process = subprocess.Popen(
        [sys.executable, "-m", "app", "serve", "--profile", profile, "--port", str(port)],
        env=env,
        start_new_session=True,
    )
    ready = False
    shutdown_allowed = False
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False
        ) as client:
            deadline = time.monotonic() + 30
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    ready = client.get("/healthz").status_code == 200
                except httpx.HTTPError:
                    pass
                if ready:
                    break
                time.sleep(0.2)
            if not ready:
                print(
                    "error: local server did not become healthy; check the startup message.",
                    file=sys.stderr,
                )
                return 1
            url = values["PUBLIC_BASE_URL"]
            print(
                "Checking public HTTPS reachability; a new temporary address may take a moment.",
                flush=True,
            )
            if not _public_ready(url):
                print(
                    "error: public HTTPS health check failed; the connector is not ready. Retry when the network is available.",
                    file=sys.stderr,
                )
                return 1
            print(f"\nLocal server ready. Browser connector: {url}/connect/mcp/", flush=True)
            print(f"OpenAI webhook: {url}/webhooks/openai (realtime.call.incoming)", flush=True)
            print("Use your owner login password on the authorization page.", flush=True)
            print(
                "Keep this terminal and computer running. Ctrl-C stops when calls are idle.",
                flush=True,
            )
            if profile == "evaluation":
                print("Evaluation: plans are allowed; dialing is disabled.", flush=True)
            else:
                print("Live: review and explicitly approve a call plan before dialing.", flush=True)
            tunnel_failed = False
            while process.poll() is None:
                try:
                    if not tunnel.is_running:
                        if not tunnel_failed:
                            print(
                                "error: public tunnel stopped; waiting for calls to finish before stopping.",
                                file=sys.stderr,
                            )
                        tunnel_failed = True
                        if _stop_allowed(client, values["DEPLOY_GUARD_TOKEN"]):
                            shutdown_allowed = True
                            return 1
                    time.sleep(0.5)
                except KeyboardInterrupt:
                    if _stop_allowed(client, values["DEPLOY_GUARD_TOKEN"]):
                        shutdown_allowed = True
                        return 0
                    print(
                        "A call is active or idle status is unavailable. Finish/end it through the connector, then press Ctrl-C again.",
                        flush=True,
                    )
            return int(process.returncode or 0)
    finally:
        # An unexpected launcher failure must not cut off a live call either.
        # Keep both child and tunnel alive until the atomic deployment lease
        # confirms idle and blocks new starts, or the child has already exited.
        try:
            if ready and not shutdown_allowed:
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False
                ) as guard:
                    announced = False
                    while process.poll() is None:
                        try:
                            if _stop_allowed(guard, values["DEPLOY_GUARD_TOKEN"]):
                                break
                            if not announced:
                                print(
                                    "Waiting for verified idle state before stopping the local server.",
                                    flush=True,
                                )
                                announced = True
                            time.sleep(1)
                        except KeyboardInterrupt:
                            continue
            _terminate(process)
        finally:
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)


def run_local(*, directory: Path, port: int, profile: str) -> int:
    if profile not in {"live", "evaluation"} or not 1 <= port <= 65535:
        print("error: use profile live/evaluation and port 1–65535.", file=sys.stderr)
        return 2
    try:
        directory = directory.resolve()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with FileLock(str(directory / ".start.lock"), timeout=0), _working_directory(directory):
            target = directory / ".env.local"
            if (directory / ".env").exists():
                raise ValueError(
                    "managed startup requires a separate private configuration directory"
                )
            values = _config(target)
            if not values and not sys.stdin.isatty():
                print(
                    "error: first start needs an interactive terminal for setup.", file=sys.stderr
                )
                return 2
            if values:
                settings = Settings.from_environ(values, agent_call_profile=profile)
                settings.require_runtime_configuration()
                if not settings.mcp_oauth_enabled:
                    print(
                        "error: enable browser OAuth before using managed local startup.",
                        file=sys.stderr,
                    )
                    return 2
                if not _idle_database(settings):
                    print(
                        "error: existing calls are not terminal; keep their original server and URL until cleanup completes.",
                        file=sys.stderr,
                    )
                    return 2
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            print(
                "Starting a temporary HTTPS address; required tunnel software is downloaded and verified automatically.",
                flush=True,
            )
            with QuickTunnel(port=port, cache_dir=directory / ".tunnel") as tunnel:
                previous = values.get("PUBLIC_BASE_URL")
                if not values:
                    if run_setup(public_url=tunnel.url) != 0:
                        return 2
                    values = _config(target)
                elif previous != tunnel.url:
                    set_key(target, "PUBLIC_BASE_URL", tunnel.url, quote_mode="always")
                    values["PUBLIC_BASE_URL"] = tunnel.url
                    print(
                        "Temporary address changed. Update the OpenAI webhook and recreate browser connectors using the URL below.",
                        flush=True,
                    )
                # Validate again after setup; no provider request is made here.
                Settings.from_environ(
                    values, agent_call_profile=profile
                ).require_runtime_configuration()
                return _serve(values, port, profile, tunnel)
    except Timeout:
        print("error: this local instance is already running.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStartup cancelled.", file=sys.stderr)
        return 130
    except TunnelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        print(
            "error: local startup failed. Check configuration, the local port, and internet access; secret values are not shown.",
            file=sys.stderr,
        )
        return 2
