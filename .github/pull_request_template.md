## Summary

<!-- What does this change do, and why? -->

## Risk

- [ ] Live-call teardown / recovery
- [ ] Billing (OpenAI Realtime, Twilio, or canary)
- [ ] Configuration / environment variables / Fly or Render
- [ ] Privacy (transcripts, phone numbers, logs, debug payloads)
- [ ] None of the above

Explain any checked items:

## Tests

- [ ] `uv run ruff format --check app tests scripts`
- [ ] `uv run ruff check app tests scripts`
- [ ] `uv run mypy app` (required locally / pre-commit; not in `.github/workflows/ci.yml`)
- [ ] `uv run pytest -q --cov=app`
- [ ] Added or updated tests for behavior changes

## Live SIP canary

- [ ] **Not run** (default for documentation and mocked-test changes)
- [ ] Run against **my own** deployment with real credentials (say which mode)

Do not run `scripts/run_sip_canary.py` against the maintainer's production app from a fork. It places a real billable call.

## Checklist

- [ ] No real credentials, E.164 numbers, or transcripts in the diff or this description
- [ ] Forks/docs do not instruct people to deploy to the maintainer `agent-call` Fly app
- [ ] Contributions are MIT-licensed
