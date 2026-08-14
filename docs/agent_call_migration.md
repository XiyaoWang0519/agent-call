# poke-call → agent-call infra migration

> Maintainer-only cutover notes for the private Fly.io rename. **Public users
> and forks should ignore this file.** Provision your own Fly app name and
> follow [self-hosting.md](self-hosting.md). Do not deploy to the maintainer's
> `agent-call` application.

The rebrand is already merged, so the **Deploy production** workflow will remain
red until `agent-call` has been provisioned and first-deployed manually. Keep
`poke-call` running until the new app is healthy and the cutover smoke passes.

## 1. Create the new Fly app and volume

```bash
flyctl apps create agent-call
# If the name is taken, pick agent-call-<suffix> and update fly.toml, PUBLIC_BASE_URL, CI, and docs.
flyctl volumes create agent_call_data --region lax --size 1 -a agent-call
```

## 2. Set runtime and CI secrets

Copy Twilio / OpenAI / Exa / MCP / debug / deploy-guard values from the old app's vault
(or local `.env`), then set the renamed agent vars:

```bash
flyctl secrets set -a agent-call \
  ALLOWED_AGENT_USER_ID=... \
  MCP_BEARER_TOKEN=... \
  DEBUG_API_TOKEN=... \
  DEPLOY_GUARD_TOKEN=... \
  OPENAI_API_KEY=... \
  OPENAI_WEBHOOK_SECRET=... \
  OPENAI_PROJECT_ID=... \
  EXA_API_KEY=... \
  TWILIO_ACCOUNT_SID=... \
  TWILIO_AUTH_TOKEN=... \
  TWILIO_CALLER_ID=... \
  OWNER_PHONE_E164=... \
  AGENT_PUSH_ENABLED=false \
  ASK_AGENT_ENABLED=true
# Optional OpenClaw wake channel:
# AGENT_WEBHOOK_URL=https://YOUR_GATEWAY/hooks/agent
# AGENT_WEBHOOK_TOKEN=...
```

`flyctl secrets list` shows names only — pull values from your local vault.

The existing GitHub Actions `FLY_API_TOKEN` is scoped to `poke-call`. Create a
new token scoped to `agent-call` and replace the secret in the `production`
GitHub environment without printing it:

```bash
flyctl tokens create deploy -a agent-call -x 8760h \
  | gh secret set FLY_API_TOKEN --env production
```

## 3. Lock the old app

Acquire the lease immediately before capturing state. HTTP 409 means wait for
calls to finish; HTTP 200 blocks new calls for the next 15 minutes.

```bash
curl --fail-with-body -sS --request POST \
  -H "Authorization: Bearer $DEPLOY_GUARD_TOKEN" \
  https://poke-call.fly.dev/internal/deployment-lock
```

## 4. Optional SQLite copy

The service uses SQLite WAL mode, so do not copy only the live `poke_call.db`
file and do not overwrite a database opened by a running destination process.
Create a consistent online backup, seed the unattached new volume through a
short-lived maintenance Machine, and only then perform the first deploy:

```bash
migration_dir="$(mktemp -d)"

# The sqlite3 backup API includes committed WAL contents in one consistent file.
flyctl ssh console -a poke-call -C \
  'python -c "from pathlib import Path; import sqlite3; snapshot=Path(\"/data/poke_call_snapshot.db\"); snapshot.unlink(missing_ok=True); source=sqlite3.connect(\"file:/data/poke_call.db?mode=ro\", uri=True); target=sqlite3.connect(snapshot); source.backup(target); target.close(); source.close()"'
flyctl ssh sftp get /data/poke_call_snapshot.db \
  "$migration_dir/poke_call_snapshot.db" -a poke-call

# Attach the new volume to a temporary maintenance Machine, upload the snapshot,
# verify it, and give the runtime's fixed non-root UID ownership of the volume.
flyctl machine run python:3.13-slim sleep infinity \
  --app agent-call \
  --region lax \
  --volume agent_call_data:/data \
  --restart no \
  --detach
flyctl machine list -a agent-call
flyctl ssh sftp put "$migration_dir/poke_call_snapshot.db" \
  /data/agent_call.db -a agent-call
flyctl ssh console -a agent-call -C \
  'python -c "import sqlite3; connection=sqlite3.connect(\"file:/data/agent_call.db?mode=ro\", uri=True); result=connection.execute(\"PRAGMA integrity_check\").fetchone()[0]; connection.close(); assert result == \"ok\", result"'
flyctl ssh console -a agent-call -C \
  'chown -R 10001:10001 /data && chmod 750 /data && chmod 640 /data/agent_call.db'
flyctl machine destroy <maintenance-machine-id> -a agent-call --force

flyctl ssh console -a poke-call -C \
  'python -c "from pathlib import Path; Path(\"/data/poke_call_snapshot.db\").unlink(missing_ok=True)"'
rm -f "$migration_dir/poke_call_snapshot.db"
rmdir "$migration_dir"
```

For a fresh database, still use the maintenance Machine to run
`chown 10001:10001 /data && chmod 750 /data` before the first deploy. The
container deliberately runs as UID 10001 and must not be granted world-writable
volume access.

## 5. Perform the first deploy and verify

```bash
flyctl deploy -a agent-call --ha=false --remote-only
curl -fsS https://agent-call.fly.dev/healthz
```

This manual first deploy makes the deployment-lease endpoint available so the
push-to-main workflow can operate normally.

## 6. Freeze the old app and re-point external webhooks

Refresh the old app's 15-minute lease immediately before stopping it. HTTP 409
means a call became active and the cutover must wait. Once the old Machine is
stopped, it cannot accept a new call if the lease expires during later steps.

```bash
curl --fail-with-body -sS --request POST \
  -H "Authorization: Bearer $DEPLOY_GUARD_TOKEN" \
  https://poke-call.fly.dev/internal/deployment-lock
flyctl machine list -a poke-call
flyctl machine stop <old-poke-call-machine-id> -a poke-call
```

- OpenAI Platform → Project → Webhooks → `https://agent-call.fly.dev/webhooks/openai` (`realtime.call.incoming`)
- Any Twilio configs that still reference `poke-call.fly.dev`

## 7. Configure the agent client

**OpenClaw**

```bash
openclaw mcp add agent-call \
  --url https://agent-call.fly.dev/mcp/ \
  --transport streamable-http \
  --header "Authorization: Bearer <MCP_BEARER_TOKEN>" \
  --header "X-Agent-User-Id: <ALLOWED_AGENT_USER_ID>"
openclaw mcp probe agent-call
```

Enable `hooks` in `openclaw.json` if using push; point `AGENT_WEBHOOK_URL` at `<gateway>/hooks/agent`.

**Hermes Agent** — add `mcp_servers.agent-call` with `url` + `headers` in `~/.hermes/config.yaml`; leave `AGENT_PUSH_ENABLED=false`.

## 8. Smoke the cutover

```bash
# Against the new URL (set PUBLIC_BASE_URL / MCP target accordingly)
scripts/live_smoke.sh
# Optional live canary
uv run python scripts/run_sip_canary.py --mode full
```

If validation fails, point the client and webhooks back to `poke-call`, then
restart its stopped Machine. Do not run both apps against the same SQLite
volume.

## 9. Verify automatic deployment

Re-run the failed **Deploy production** workflow and confirm it acquires the
live `agent-call` lease, deploys merged `main`, and passes its health check with
the new app-scoped token.

## 10. Decommission the old app

Verify the old Machine is still stopped immediately before destruction. If it
was restarted for a rollback, repeat the deployment-lock request from step 6,
wait for HTTP 200, and stop it again.

```bash
flyctl machine list -a poke-call
flyctl apps destroy poke-call
```

Optionally rename the local project directory `poke-call` → `agent-call`.
