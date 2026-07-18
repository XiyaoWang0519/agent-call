# Contributing

Thanks for considering a contribution to the Poke Phone-Call Bridge.

## Getting started

1. Clone the repo and install [uv](https://docs.astral.sh/uv/) if you don't
   have it.
2. Install dependencies:

   ```bash
   uv sync --all-groups --frozen
   ```

3. Copy the example environment file:

   ```bash
   test -e .env.local || cp .env.example .env.local
   ```

   `.env.local` is gitignored. Fill dummy values for local boots; you do not
   need real credentials to run the test suite — every external service
   (OpenAI, Twilio, Exa, Poke) is mocked in `tests/conftest.py`, and tests
   run against a temporary SQLite database. Real credentials plus a public
   HTTPS tunnel are only needed to place a live call.

   Do not set `ALLOWED_COUNTRY_CODES` in `.env.local` (or any dotenv file).
   pydantic-settings JSON-decodes list-typed fields, so a bare `=+1` value
   crashes startup — omit it and rely on the default `["+1"]`.

## Running tests

```bash
uv run pytest -q
```

Tests use `pytest-asyncio` (auto mode) and `respx` to mock outbound HTTP.
Add regression coverage for state transitions, signed webhook handling,
payload shapes, and recovery/teardown paths — exercise both success and
failure cases for new behavior.

CI enforces a coverage floor; to check locally run
`uv run pytest -q --cov=app` (fails under 85%, see `[tool.coverage.report]`
in `pyproject.toml`).

## Linting

```bash
uv run ruff check .
uv run ruff format --check .
```

Ruff targets Python 3.12 with a 100-character line length and enforces
pycodestyle, Pyflakes, import sorting (`I`), pyupgrade, bugbear, and async
rules (see `[tool.ruff]` in `pyproject.toml`). Run `uv run ruff format .` to
auto-format before committing. If you have `mypy` set up locally, `uv run
mypy app` runs strict type checking on the `app` package (not required for
CI, but appreciated).

## Pull request guidelines

- Branch from `main`.
- Keep PRs focused on a single change; avoid unrelated refactors.
- Add or update tests for any behavioral change.
- Ensure CI passes: GitHub Actions runs `ruff format --check`, `ruff check`,
  and `pytest -q` against `app`, `tests`, and `scripts` on every PR and push
  to `main`.
- Describe the behavioral change, any risk to live-call handling or billing,
  and configuration changes in the PR description.

By contributing, you agree your contributions will be licensed under this
project's [MIT License](LICENSE).
