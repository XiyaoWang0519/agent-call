# poke-call → agent-call infra migration

Run this when there are **no active calls**. Configs in the repo already target
`agent-call` / `agent-call.fly.dev`.

Merging this rebrand before the new Fly app exists will fail the GitHub
**Deploy production** workflow: it waits on `https://agent-call.fly.dev` for a
deployment lease, then exits after a few unreachable attempts. That is
expected. Production stays on `poke-call` until you finish this cutover and
re-run the workflow (or deploy with `flyctl` as below).

To keep auto-deploy green on the merge commit, complete steps 2–5 first so the
lease endpoint exists, then merge. Otherwise merge first, ignore that failed
deploy run, and continue here.

## 1. Lock the old app

```bash
curl --fail-with-body -sS --request POST \
  -H "Authorization: Bearer $DEPLOY_GUARD_TOKEN" \
  https://poke-call.fly.dev/internal/deployment-lock
```

HTTP 409 means wait for calls to finish.

## 2. Create the new Fly app and volume

```bash
flyctl apps create agent-call
# If the name is taken, pick agent-call-<suffix> and update fly.toml, PUBLIC_BASE_URL, CI, and docs.
flyctl volumes create agent_call_data --region lax --size 1 -a agent-call
```

## 3. Set secrets

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

## 4. Optional SQLite copy

```bash
# From the old machine
flyctl ssh sftp get /data/poke_call.db ./poke_call.db -a poke-call
# After first deploy creates /data on agent-call, upload as agent_call.db
flyctl ssh sftp put ./poke_call.db /data/agent_call.db -a agent-call
```

Or start fresh (call history only).

## 5. Deploy and verify

```bash
flyctl deploy -a agent-call --ha=false --remote-only
curl -fsS https://agent-call.fly.dev/healthz
```

## 6. Re-point external webhooks

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

## 8. Smoke, then decommission

```bash
# Against the new URL (set PUBLIC_BASE_URL / MCP target accordingly)
scripts/live_smoke.sh
# Optional live canary
uv run python scripts/run_sip_canary.py --mode full

flyctl apps destroy poke-call
```

Optionally rename the local project directory `poke-call` → `agent-call`.
