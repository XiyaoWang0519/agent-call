from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from pydantic import ValidationError

from scripts.live_phone.config import Config
from scripts.live_phone.provider import Provider
from scripts.live_phone.scenarios import SCENARIOS, SMOKE
from scripts.live_phone.server import create_app
from scripts.live_phone.store import Store


async def download(http: httpx.AsyncClient, base: str, run_id: str, output: Path) -> None:
    root = output / run_id
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in (
        "report.json",
        "report.html",
        "junit.xml",
        "callee-received.wav",
        "callee-sent.wav",
        "owner-received.wav",
        "owner-sent.wav",
    ):
        response = await http.get(f"{base}/runs/{run_id}/artifacts/{name}")
        if response.status_code == 404 and name.endswith(".wav"):
            continue
        response.raise_for_status()
        path = root / name
        path.write_bytes(response.content)
        path.chmod(0o600)


async def run_suite(config: Config, names: list[str], output: Path, confirm: str) -> int:
    if confirm != config.instance_id:
        raise ValueError("explicit instance confirmation required")
    if not names or any(name not in SCENARIOS for name in names):
        raise ValueError("unknown or empty scenario selection")
    if sum(SCENARIOS[name].seconds + 45 for name in names) > config.max_suite_seconds:
        raise ValueError(
            "suite exceeds configured duration budget; select fewer scenarios or adjust authorization"
        )
    base = config.public_url.rstrip("/")
    async with httpx.AsyncClient(
        timeout=300,
        follow_redirects=False,
        headers={"Authorization": "Bearer " + config.token.get_secret_value()},
    ) as http:
        results = []
        for name in names:
            response = await http.post(
                base + "/runs", json={"scenario": name, "confirm_instance": confirm}
            )
            response.raise_for_status()
            run_id = response.json()["run_id"]
            print(f"{name}: {run_id}", flush=True)
            # A dropped start response is never retried: the server owns the run and cleanup.
            deadline = time.monotonic() + SCENARIOS[name].seconds + 120
            while time.monotonic() < deadline:
                response = await http.get(base + f"/runs/{run_id}")
                response.raise_for_status()
                result = response.json()
                if result["done"]:
                    await download(http, base, run_id, output)
                    results.append(result)
                    print(f"{name}: {'PASS' if result.get('passed') else 'FAIL'}", flush=True)
                    break
                await asyncio.sleep(2)
            else:
                raise TimeoutError("run still unfinished; independent reaper must reconcile it")
        await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True, mode=0o700)
        path = output / "suite.json"
        path.write_text(json.dumps(results, indent=2) + "\n")
        path.chmod(0o600)
        return 0 if all(row.get("passed") is True for row in results) else 1


async def reap(config: Config, watch: bool) -> int:
    store = Store(config.artifacts)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as http:
        provider = Provider(config, http)
        while True:
            results = await provider.reap(store)
            for result in results:
                print(json.dumps(result), flush=True)
            if not watch:
                return 1 if any(not r["verified"] for r in results) else 0
            await asyncio.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Authorized, unattended real-phone testing")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Explicit ignored harness configuration; no default dotenv loading",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List scenarios without credentials or network calls")
    serve = commands.add_parser("serve", help="Host automated callee and owner endpoints")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8091)
    run = commands.add_parser("run", help="Place real billable calls to configured test numbers")
    run.add_argument("--suite", choices=("smoke", "full"), default="smoke")
    run.add_argument(
        "--scenario", action="append", help="Run selected scenario(s) instead of a suite"
    )
    run.add_argument("--confirm-instance", required=True)
    run.add_argument("--output", type=Path, default=Path(".live-phone-results"))
    cleanup = commands.add_parser("reap", help="Independent provider cleanup supervisor")
    cleanup.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    if args.command == "list":
        print(json.dumps([s.public() for s in SCENARIOS.values()], indent=2))
        return 0
    try:
        config = Config(_env_file=args.env_file)  # type: ignore[call-arg]
        if args.command == "serve":
            uvicorn.run(
                create_app(config),
                host=args.host,
                port=args.port,
                ws_max_size=64 * 1024,
                access_log=False,
            )
            return 0
        if args.command == "reap":
            return asyncio.run(reap(config, args.watch))
        names = args.scenario or (list(SMOKE) if args.suite == "smoke" else list(SCENARIOS))
        return asyncio.run(run_suite(config, names, args.output, args.confirm_instance))
    except ValidationError as exc:
        # Pydantic's default repr contains input values, including secrets.
        fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors(include_input=False))
        print(f"Invalid harness configuration fields: {fields}", file=sys.stderr)
        return 2
    except (Exception, KeyboardInterrupt) as exc:
        print(
            f"Harness stopped: {type(exc).__name__}. Check the authenticated run status and reaper.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
