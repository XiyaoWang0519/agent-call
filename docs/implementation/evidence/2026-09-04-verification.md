# Phase 1 local verification — 2026-09-04

This record supersedes the August 28 Docker-unavailable and test-count notes.
Verified code: `1c6418b41b455d42a02d95e97d5049a43d2ed896` on
`phase1-sanitized`, based on `origin/main` at
`c2964b5868eccf593844336096f99a0f55914675`.

## Fixes found through execution

- The first real Compose launch failed with `chown: changing ownership of
  '/var/data': Read-only file system`. The entrypoint now repairs only entries
  whose user/group differ from the application account. Correct image-owned
  directories are left untouched. The root-owned volume repair still works.
- Nested logs, transcripts, data directories, SQLite files and journals are
  excluded even inside otherwise included application source directories.
- An existing zero-byte SQLite target must allow a read/write open before
  doctor tests the parent location. Failure is redacted and the file is unchanged.

## Automated checks

All commands below exited zero:

```sh
uv sync --all-groups --frozen
uv run ruff format --check app tests scripts
uv run ruff check app tests scripts
uv run mypy app
AGENT_CALL_DOCKER_TESTS=1 AGENT_CALL_DOCKER_IMAGE=agent-call-self-host:local \
  uv run pytest -q --cov=app --cov-report=term:skip-covered
uv run pre-commit run --all-files
git diff --check origin/main...HEAD
gitleaks git . --log-opts=HEAD --redact --no-banner
```

Result: **485 passed**, **88.04% application coverage** (85% required).
The container module passed all ten tests, including two opt-in Docker tests.
Without the Docker opt-ins, those two tests are skipped.
Gitleaks scanned 108 commits reachable from the verified code commit, no leaks.

The BuildKit context test exports a synthetic context using the real
`.dockerignore`. Required source files survive; synthetic Git objects, env
files, caches, logs, transcripts and databases do not. It never reads local
credential files. The entrypoint test uses a read-only root, a root-owned
temporary `/data`, and no network, and verifies UID 10001 throughout.

## Actual image and Compose

Docker Engine 28.1.1, local Linux ARM64 image:
`sha256:ecd159d24f974cb237e70a6ff365bcfde3ab4feecf0f35964eb3fbb644fa6311`.
No image was published. The build used the repository Dockerfile and lockfile.

Executed with isolated Compose project `agent-call-verification-20260904`:

```sh
docker compose -p agent-call-verification-20260904 up --build -d --wait --wait-timeout 90
docker compose -p agent-call-verification-20260904 exec -T --user 10001:10001 app agent-call smoke-prepare
docker compose -p agent-call-verification-20260904 exec -T --user 10001:10001 app agent-call doctor --dummy
docker compose -p agent-call-verification-20260904 down
docker compose -p agent-call-verification-20260904 up -d --wait --wait-timeout 90
```

- Build succeeded; container healthy; read-only root enabled.
- `/proc/1/status` confirmed UID 10001 for the server.
- Host-loopback `/healthz` returned `{"status":"ok"}`.
- Installed CLI initialized MCP, listed seven tools, and persisted one plan.
- Dummy doctor passed with live calls disabled.
- After removing and recreating the container, a database query confirmed the
  exact same plan ID remained, the calls table contained zero rows, and
  `PRAGMA integrity_check` returned `ok`.
- Verification container/network were removed afterward. The isolated test
  volume and local image were retained. Unrelated containers were not modified.

## Source smoke and history

`AGENT_CALL_PROFILE=evaluation GROK_MCP_OAUTH_ENABLED=false bash
scripts/live_smoke.sh 18765` exited zero. Health, MCP initialization, seven-tool
discovery, persisted prepare, missing-credential rejection, disabled OAuth
discovery, enabled OAuth discovery/registration/consent/token exchange and both
MCP surfaces passed. No start-call tool was invoked. Script-owned servers exited.

`origin/main` remained the ancestor. The private plan path is absent from HEAD
and from `git log origin/main..HEAD`; neither the private path nor its known
blob appears in `git rev-list origin/main..HEAD --objects`.
Old local branches `main` and `phase1-unsafe-source` were left untouched and must
not be pushed. Only the sanitized branch is suitable for a future PR.

## Remaining release work

This verifies the local Phase 1 increment, not the complete product split.
Fresh non-maintainer web deployment, live telephony canary, production rollback,
clean-machine usability study, x86 image execution, release signing/SBOM and
managed-product decisions are not proven by this run. No push, public PR,
deployment, webhook change, phone call, or provider purchase was performed.
