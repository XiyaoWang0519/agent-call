# Evidence

Redacted command logs and Phase 1 verification notes for the public repository.

Do not place secrets, full credentials, full E.164 numbers, transcripts, or
customer identifiers in this directory.

Files written by the Phase 1 builder:

- `ruff-mypy-pytest.log` — format, lint, types, and coverage-gated pytest
- `dockerfile-force-include.log` — hatch force-include paths are COPYed before uv sync
- `eval-start.log` — evaluation `start_phone_call` gate
- `doctor.log` — doctor missing/blank/malformed/success cases
- `fork-safety.log` — golden-path scan plus upstream-maintainer-only workflow guard
- `smoke-prepare.log` — prepare-only MCP smoke and target-validation regressions
- `healthz-1.json` / `healthz-2.json` — dummy-boot health checks
- `compose-launch.log` — Compose availability (or why it was skipped)
