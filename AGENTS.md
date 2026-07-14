# Repository Guidelines

## Project Structure & Module Organization

`app/` contains the Python service. `main.py` assembles FastAPI and FastMCP; `routes/` handles OpenAI, Twilio, debug, and deployment callbacks; `call_state.py`, `twilio_bridge.py`, and `openai_realtime.py` coordinate live calls; and `db.py`, `models.py`, `policy.py`, and `security.py` own persistence, schemas, call policy, and authentication. Tests mirror these concerns in `tests/test_*.py`, with shared fixtures in `tests/conftest.py`. Operational files live at the repository root (`Dockerfile`, `fly.toml`, `render.yaml`), while `scripts/run_sip_canary.py` performs live end-to-end validation.

## Build, Test, and Development Commands

- `uv sync --all-groups --frozen`: install dependencies from `uv.lock`.
- `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`: run the local service with reload.
- `uv run ruff format --check app tests scripts`: verify formatting.
- `uv run ruff check app tests scripts`: run configured lint and import checks.
- `uv run pytest -q`: run the automated test suite.
- `uv run pytest -q tests/test_security.py`: run one focused test module.
- `uv run python scripts/run_sip_canary.py --mode full`: validate a deployed SIP flow; this places a real call and requires configured credentials.

## Coding Style & Naming Conventions

Use four-space indentation, Python type hints, and `from __future__ import annotations`. Ruff targets Python 3.12 with a 100-character line length and enforces pycodestyle, Pyflakes, import sorting, pyupgrade, bugbear, and async rules. Use `snake_case` for modules, functions, variables, and tests; `PascalCase` for classes and Pydantic models; and descriptive async names for network or database operations.

## Testing Guidelines

Tests use pytest, `pytest-asyncio` in auto mode, `respx`, and temporary SQLite databases. Name files `test_<area>.py` and tests `test_<behavior>`. Add regression coverage for state transitions, signed webhook handling, payload shapes, recovery, and teardown. No numeric coverage threshold is configured; new behavior should still exercise both success and failure paths.

## Commit & Pull Request Guidelines

Recent history uses short, imperative, sentence-case subjects such as `Add call-safe deployment lease`. Keep commits focused. Pull requests should explain the behavioral change, risks to live-call teardown or billing, configuration changes, and validation performed. Link relevant issues; include logs or payload examples for backend changes and canary evidence when telephony behavior changes. Ensure Ruff and pytest pass before review.

## Security & Operations

Copy `.env.example` to `.env.local`; never commit secrets, transcripts, or database files. Preserve webhook signature checks, replay protection, destination policy, and explicit call confirmation. Do not deploy or restart while a call is active, and keep production to one Fly Machine because SQLite state is volume-local.
