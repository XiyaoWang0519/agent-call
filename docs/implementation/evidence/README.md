# Evidence

Redacted command logs and Phase 1 verification notes for the public repository.

Do not place secrets, full credentials, full E.164 numbers, transcripts, or
customer identifiers in this directory.

Files written by the Phase 1 builder:

- [2026-09-04-verification.md](2026-09-04-verification.md) — latest independent
  verification, actual Docker build/startup/persistence, final fixes and test totals;
  supersedes the older Docker-unavailable notes below

- `ruff-mypy-pytest.log` — format, lint, types, coverage-gated pytest, pre-commit, gitleaks
- `dockerfile-force-include.log` — hatch force-include paths are COPYed before uv sync
- `container-security.log` — `.dockerignore` allowlist and explicit Dockerfile COPY
- `eval-start.log` — evaluation `start_phone_call` gate
- `doctor.log` — doctor missing/blank/malformed/success cases plus zero-byte SQLite
- `fork-safety.log` — golden-path scan plus upstream-maintainer-only workflow guard
- `smoke-prepare.log` — prepare-only MCP smoke and target-validation regressions
- `healthz-1.json` / `healthz-2.json` — dummy-boot health checks
- `compose-launch.log` — Compose availability (or why it was skipped)
