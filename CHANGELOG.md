# Changelog

Notable changes to this project are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This repository has **no tagged GitHub release** yet. Package metadata in
`pyproject.toml` currently reports `0.1.0` as a development version, not as a
published release.

## Unreleased

### Fixed

- Compose can start with a read-only root filesystem: the entrypoint repairs
  only mismatched ownership in data directories before dropping privileges.
- SQLite doctor rejects unwritable zero-byte targets without modifying them.
- Docker context exclusions also cover nested logs, transcripts, runtime data,
  SQLite files, and journal files under otherwise allowed source directories.

### Added

- Evaluation/dummy profile (`AGENT_CALL_PROFILE=evaluation`) that boots without
  real provider credentials, serves `/healthz`, allows MCP prepare, and returns
  `live_calls_disabled` from `start_phone_call` before any OpenAI or Twilio
  client request. Default evaluation bind is loopback.
- Operator CLI via `uv run agent-call`: `serve`, `doctor --dummy|--prepare-only|--live-ready`,
  and `smoke-prepare`. Doctor does not print secrets or full E.164 values.
  `uv run python -m app` is equivalent.
- `compose.yaml` dummy stack: named volume, host-loopback publish, healthcheck,
  non-root runtime user, no credentials in the image.
- Restrictive `.dockerignore` allowlist so Git history, env files, virtualenvs,
  caches, and databases stay out of the Docker/Fly build context.
- Builder operating record under `docs/implementation/` (ADR-001 accepted;
  ADRs 002–010 unresolved).
- Optional self-hosted Grok Bot OAuth 2.1 for `/grok/mcp/`. Disabled by
  default. Existing `/mcp/` dual-header authentication is unchanged. Agent
  Call hosts the authorization page itself; there is no external identity
  provider. **Implemented locally; pending live Grok OAuth verification.**
- `scripts/hash_grok_oauth_owner_secret.py` to generate an Argon2id hash of
  the owner authorization secret. The server stores only the hash.
- Private Grok Bot integration docs and a copy-and-paste phone-call skill
  (`docs/grok-bot/`). Automated coverage extends `scripts/live_smoke.sh` and
  adds Grok-compatible MCP client tests that prepare a plan without Twilio
  or OpenAI network calls.

### Changed

- `doctor --live-ready` treats a zero-byte SQLite file as a new database
  location (parent writability probe) instead of a corrupt database. A missing
  parent directory remains a conservative failure; doctor does not create
  `DATABASE_URL` parents.
- `agent-call smoke-prepare` validates `--base-url` and `--mcp-path` before
  reading environment credentials. Loopback `http://` is allowed; non-loopback
  targets require `https://`. `--mcp-path` must be a same-origin path.
- `agent-call doctor --live-ready` probes SQLite with a real open and rolled-back
  write (or a temporary database for a new path), plus public-origin health and
  non-billable Twilio/OpenAI metadata. Exa and the OpenAI webhook secret stay
  unverified. Run it after the live server and HTTPS origin exist.
- Production Fly deploy workflow is upstream-maintainer-only
  (`github.repository == 'XiyaoWang0519/agent-call'`).
- `agent-call serve` no longer forces the evaluation profile. Settings and
  `uvicorn` treat an unset `AGENT_CALL_PROFILE` as live. Dummy boot is
  `--profile evaluation`. An unset profile with any core runtime credential
  (dotenv or process environment) is refused; pass `--profile live` (this will
  place billable calls) or `--profile evaluation`.
- README presents Self-Hosted vs Managed (managed marked forthcoming/private).
  Local and web golden paths no longer default-target a maintainer Fly app.
  User `fly.toml` is a `YOUR_FLY_APP_NAME` template; maintainer production
  overlay lives in `deploy/maintainer/`.
- Pre-commit Gitleaks now scans the working tree (`--no-git`) so local extra
  branches do not fail the hook. GitHub Actions still runs a full-history
  Gitleaks scan.

### Removed

- Managed Agent Call remains a forthcoming private service and is not
  configured in this public repository.
- `AGENT_CALL_UNSAFE_BIND` is no longer read. Evaluation non-loopback bind is
  CLI `--unsafe-bind` only.
- `scripts/doctor.py` and `scripts/smoke_prepare.py` wrappers. Use
  `uv run agent-call doctor` / `uv run agent-call smoke-prepare`, or
  `uv run python -m app …`.
