# Repository Guidelines

## Project Structure & Module Organization

`app/` contains the Python service. `main.py` assembles FastAPI and FastMCP; `routes/` handles OpenAI, Twilio, debug, and deployment callbacks through explicit `CallService` methods (routes never touch `service.db` directly); `call_state.py` (the `CallService` facade and call state machine), `call_activity.py` (liveness/heartbeat tracking), `owner_transfer.py` (the owner-transfer saga), `twilio_bridge.py`, and `openai_realtime.py` coordinate live calls; and the `db/` package (a `Database` facade composed from per-concern modules: engine, plans, deployment, calls, transfers, termination, telemetry, webhooks, transcripts, questions), `models.py`, `policy.py`, and `security.py` own persistence, schemas, call policy, and authentication. Tests mirror these concerns in `tests/test_*.py`, with shared fixtures in `tests/conftest.py`. Operational files live at the repository root (`Dockerfile`, `fly.toml`, `render.yaml`), while `scripts/run_sip_canary.py` performs live end-to-end validation and `scripts/live_smoke.sh` boots the app with dummy credentials for a local MCP smoke test.

## Build, Test, and Development Commands

- `uv sync --all-groups --frozen`: install dependencies from `uv.lock`.
- `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`: run the local service with reload.
- `uv run ruff format --check app tests scripts`: verify formatting.
- `uv run ruff check app tests scripts`: run configured lint and import checks.
- `uv run mypy app`: run strict mypy type checking on the application package.
- `uv run pre-commit install`: install local git hooks (ruff, large-file check, gitleaks, mypy).
- `uv run pre-commit run --all-files`: run the same hooks on the full tree.
- `uv run pytest -q`: run the automated test suite.
- `uv run pytest -q tests/test_security.py`: run one focused test module.
- `uv run python scripts/run_sip_canary.py --mode full`: validate a deployed SIP flow; this places a real call and requires configured credentials.

## Coding Style & Naming Conventions

Use four-space indentation, Python type hints, and `from __future__ import annotations`. Ruff targets Python 3.12 with a 100-character line length and enforces pycodestyle, Pyflakes, import sorting, pyupgrade, bugbear, and async rules. Use `snake_case` for modules, functions, variables, and tests; `PascalCase` for classes and Pydantic models; and descriptive async names for network or database operations.

## Testing Guidelines

Tests use pytest, `pytest-asyncio` in auto mode, `respx`, and temporary SQLite databases. Name files `test_<area>.py` and tests `test_<behavior>`. Add regression coverage for state transitions, signed webhook handling, payload shapes, recovery, and teardown. CI enforces an 85% coverage floor on `app` via `pytest --cov=app`; new behavior should still exercise both success and failure paths.

## Commit & Pull Request Guidelines

Recent history uses short, imperative, sentence-case subjects such as `Add call-safe deployment lease`. Keep commits focused. Pull requests should explain the behavioral change, risks to live-call teardown or billing, configuration changes, and validation performed. Link relevant issues; include logs or payload examples for backend changes and canary evidence when telephony behavior changes. Ensure Ruff and pytest pass before review.

## Security & Operations

Copy `.env.example` to `.env.local`; never commit secrets, transcripts, or database files. Preserve webhook signature checks, replay protection, destination policy, and explicit call confirmation. Do not deploy or restart while a call is active, and keep production to one Fly Machine because SQLite state is volume-local.

### Rollback (prior-image recovery)

If a deploy breaks production, do not redeploy a fix while debugging. Roll back first:

1. Confirm no active calls (or wait for them to finish); this POST acquires the deployment lease and fails with HTTP 409 while calls are active:
   `curl --fail-with-body -sS --request POST -H "Authorization: Bearer $DEPLOY_GUARD_TOKEN" https://agent-call.fly.dev/internal/deployment-lock`
2. List recent releases with image references:
   `flyctl releases --image -a agent-call`
3. Redeploy the previous healthy image (no rebuild):
   `flyctl deploy --image <previous-image-reference> -a agent-call --ha=false --remote-only`
4. Verify:
   `curl -fsS https://agent-call.fly.dev/healthz`

Redeploying a prior image restores only the application image without a full rebuild; it does not roll back the current Fly configuration, environment variables, or secrets. Only ship a new build after calls are idle and the fix is verified locally (`ruff` + `mypy` + `pytest`).

### poke-call → agent-call cutover

Production migration steps (new Fly app, secrets, webhook re-point, OpenClaw/Hermes client config, old-app teardown) live in [docs/agent_call_migration.md](docs/agent_call_migration.md). Do not run that cutover while a call is active.

## Agent-Specific Notes

Notes below cover gotchas that are non-obvious from the sections above.

- Booting the app requires every variable in `Settings.require_runtime_configuration` (`app/settings.py`) or the lifespan aborts at startup. For local dev, copy `.env.example` to `.env.local` and fill dummy values (`.env.local` is gitignored); real Twilio/OpenAI credentials + a public HTTPS tunnel are only needed for live calls.
- Startup gotcha: do NOT put `ALLOWED_COUNTRY_CODES` in `.env.local` (or any dotenv file). pydantic-settings JSON-decodes list-typed fields from dotenv files, so `ALLOWED_COUNTRY_CODES=+1` crashes boot with a `SettingsError`. Omit it and rely on the `+1` default.
- The full end-to-end SIP canary (`scripts/run_sip_canary.py`) places a real billable call and needs real credentials, a public tunnel, and a human with a phone; do not run it in CI or an unattended environment. Practical verification is: `ruff format --check` + `ruff check`, `pytest -q` (all externals are mocked in `tests/conftest.py`, temp SQLite), boot the server, and drive the MCP tools via `scripts/live_smoke.sh`.
- The separate `python -m scripts.live_phone` harness supports unattended billable tests only against an explicitly configured test instance and dedicated automated callee/owner numbers. Follow `docs/live-phone-runbook.md`; keep the independent reaper running, and never fall back to a personal number. Its credential-free regression tests run in ordinary pytest; live runs require provisioned test infrastructure and explicit instance confirmation.
- MCP smoke test: the endpoint is `/mcp/` (Streamable HTTP) and requires both `Authorization: Bearer <MCP_BEARER_TOKEN>` and `X-Agent-User-Id: <ALLOWED_AGENT_USER_ID>`. `prepare_phone_call` runs without any external service (validates destination policy + persists a plan to SQLite); its `context.owner.callback_number` and `escalation.owner_phone` must equal `OWNER_PHONE_E164`, and a persisted `plan_id` is only returned once `authority_basis` (or `requested_by_owner`) is supplied.

### Resuming automated live testing

- Start with [docs/live-phone-handoff.md](docs/live-phone-handoff.md). It records the
  working local setup, private config locations, tunnel restart procedure, basic run
  command, evidence requirements, audio tracks, costs and known failures.
- `--scenario basic` has passed a real call covering web search, audible interruption
  and agent hangup. Run it first for basic acceptance; do not infer full-suite coverage.
- Reuse the existing isolated test resources and private configuration when available.
  Verify current processes, URLs and idle calls; old session state is not live evidence.
- A normal pytest/MCP smoke pass is not a phone test. Report a live PASS only from that
  run's audio, tool evidence, all assertions and verified cleanup without forced hangup.
