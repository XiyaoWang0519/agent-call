# Contributing

Thanks for considering a contribution to Agent Call. By contributing, you agree
your contributions will be licensed under this project's [MIT License](LICENSE).

Please follow the [code of conduct](CODE_OF_CONDUCT.md). Report security issues
using [SECURITY.md](SECURITY.md), not a public issue.

## Supported toolchain

- **Python:** 3.12 or newer (`requires-python = ">=3.12"`). Ruff and mypy target
  3.12. GitHub Actions **CI** currently installs **Python 3.13**.
- **uv:** CI pins **uv 0.9.27** via `astral-sh/setup-uv`. Locally, a current uv
  0.9.x that can consume `uv.lock` is sufficient.

You do not need OpenAI, Twilio, Exa, or a phone number to contribute.

## Credential-free setup

```bash
uv sync --all-groups --frozen
uv run agent-call doctor --dummy
uv run agent-call serve --profile evaluation --host 127.0.0.1 --port 8000
```

Optional Compose dummy stack: `docker compose up --build` (publishes
`127.0.0.1:8000` only). Then `uv run agent-call smoke-prepare`.

```bash
test -e .env.local || cp .env.example .env.local
```

`.env.local` is gitignored. Dummy values are enough to boot the app. The test
suite does not read `.env.local`: every external service (OpenAI, Twilio, Exa;
the optional agent webhook) is mocked in `tests/conftest.py`, and tests run
against a temporary SQLite database. Real credentials plus a public HTTPS
tunnel are only needed to place a live call.

Do not set `ALLOWED_COUNTRY_CODES` in `.env.local` (or any dotenv file).
pydantic-settings JSON-decodes list-typed fields, so a bare `=+1` value
crashes startup — omit it and rely on the default `["+1"]`.

## Lint, type-check, test, coverage

These match what maintainers run before review. Commands marked **CI** are
exactly the steps in `.github/workflows/ci.yml` (pull requests and pushes to
`main`):

```bash
uv run ruff format --check app tests scripts    # CI
uv run ruff check app tests scripts             # CI
uv run mypy app                                 # pre-commit + deploy workflow; not in ci.yml
uv run pytest -q --cov=app                      # CI; fails under 85% (see pyproject.toml)
uv run pre-commit run --all-files               # local hooks: ruff, gitleaks, mypy, file checks
```

`uv run pytest -q` (no `--cov`) runs the same tests without the coverage gate.

Ruff targets Python 3.12 with a 100-character line length and enforces
pycodestyle, Pyflakes, import sorting (`I`), pyupgrade, bugbear, and async
rules (see `[tool.ruff]` in `pyproject.toml`). Auto-format with
`uv run ruff format app tests scripts`.

A separate **Secret scan** workflow runs Gitleaks on pull requests and `main`.
The **Deploy production** workflow additionally runs `mypy app` and pytest
without coverage before attempting a Fly.io deploy; that job is not part of
pull-request CI.

### Focused tests

```bash
uv run pytest -q tests/test_security.py
uv run pytest -q tests/test_security.py::test_name
```

Tests use `pytest-asyncio` (auto mode), `respx` for outbound HTTP, and
temporary SQLite databases. Name files `test_<area>.py` and tests
`test_<behavior>`. Add regression coverage for state transitions, signed
webhook handling, payload shapes, recovery, and teardown. New behavior should
exercise both success and failure paths.

## Live SIP canary

`uv run python scripts/run_sip_canary.py --mode full` places a **real billable
call** to `OWNER_PHONE_E164`. It is appropriate only when an operator is
validating a deployment they control, with real credentials, a public HTTPS
URL, and a human answering that phone.

Do **not** run it casually, in CI, unattended, or against the maintainer's
production app from a fork. Practical contributor verification is Ruff + mypy
+ pytest (externals mocked) and, optionally, `scripts/live_smoke.sh` (dummy
credentials, no call).

## Architecture and navigation

Start at [docs/architecture.md](docs/architecture.md) and [AGENTS.md](AGENTS.md).

- `app/main.py` — FastAPI + FastMCP assembly
- `app/mcp_tools.py` — the seven MCP tools
- `app/call_state.py` — `CallService` facade and call state machine
- `app/routes/` — OpenAI, Twilio, debug, and deployment HTTP handlers
- `app/db/` — SQLite facade (plans, calls, webhooks, transcripts, …)
- `app/policy.py` / `app/security.py` — destination policy and auth
- `tests/test_*.py` — mirrors those concerns; fixtures in `tests/conftest.py`

Operator deploy/rollback notes: [docs/self-hosting.md](docs/self-hosting.md).
Maintainer Fly rename history: [docs/agent_call_migration.md](docs/agent_call_migration.md)
(not needed for typical contributions).

## Pull requests

- Branch from `main`. Keep the PR focused; avoid unrelated refactors.
- Add or update tests for any behavioral change.
- Ensure the **CI** and **Secret scan** workflows pass.
- Use the pull-request template. In particular, call out:
  - risk to **live-call teardown**, **billing**, **configuration**, or **privacy**
  - whether transcripts, phone numbers, or credentials appear in logs/fixtures
  - whether you ran (or did **not** run) the live SIP canary
- Do not include real credentials, E.164 numbers, or call transcripts in the
  PR body, screenshots, or fixtures. Use reserved/test numbers only.

Contributions that change live-call handling, billing, configuration, privacy,
or teardown need extra care: explain the failure modes, keep webhook signature
and replay checks intact, and prefer tests over a real canary unless the
change cannot be proven any other way.
