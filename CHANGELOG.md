# Changelog

Notable changes to this project are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This repository has **no tagged GitHub release** yet. Package metadata in
`pyproject.toml` currently reports `0.1.0` as a development version, not as a
published release.

## Unreleased

### Added

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

- Pre-commit Gitleaks now scans the working tree (`--no-git`) so local extra
  branches do not fail the hook. GitHub Actions still runs a full-history
  Gitleaks scan.
