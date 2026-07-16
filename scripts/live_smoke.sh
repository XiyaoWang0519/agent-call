#!/usr/bin/env bash
# Local live smoke test: boot the app with dummy credentials and a temp SQLite DB,
# then drive /healthz and the MCP endpoint (initialize, tools/list, prepare_phone_call).
# Usage: scripts/live_smoke.sh [port]
set -euo pipefail

PORT="${1:-8765}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORKDIR="$(mktemp -d)"
SERVER_PID=""
trap 'if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; fi; rm -rf "$WORKDIR"' EXIT

export OPENAI_API_KEY="sk-dummy" OPENAI_WEBHOOK_SECRET="whsec_dummy" OPENAI_PROJECT_ID="proj_dummy"
export EXA_API_KEY="exa-dummy"
export TWILIO_ACCOUNT_SID="ACdummy" TWILIO_AUTH_TOKEN="dummy" TWILIO_CALLER_ID="+15550000000"
export OWNER_PHONE_E164="+15550000001" ALLOWED_POKE_USER_ID="smoke-user"
export MCP_BEARER_TOKEN="smoke-bearer" DEBUG_API_TOKEN="smoke-debug" DEPLOY_GUARD_TOKEN="smoke-deploy"
export PUBLIC_BASE_URL="https://smoke.example.com"
export DATABASE_URL="sqlite:///$WORKDIR/smoke.db"
export POKE_API_KEY="dummy" POKE_PUSH_ENABLED="false" ASK_POKE_ENABLED="true"

cd "$ROOT"
uv run uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >"$WORKDIR/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "FAIL: server exited during startup"; cat "$WORKDIR/server.log"; exit 1
  fi
  sleep 0.2
done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || { echo "FAIL: /healthz"; cat "$WORKDIR/server.log"; exit 1; }
echo "OK /healthz"

MCP_URL="http://127.0.0.1:$PORT/mcp/"
HDR=(-H "Authorization: Bearer smoke-bearer" -H "X-Poke-User-Id: smoke-user"
     -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")

mcp_call() { # $1=json body, $2=session id (optional)
  local extra=()
  [ -n "${2:-}" ] && extra=(-H "mcp-session-id: $2")
  curl -fsS -D "$WORKDIR/headers.txt" "${HDR[@]}" ${extra[@]+"${extra[@]}"} -X POST "$MCP_URL" -d "$1"
}

INIT_RESP="$(mcp_call '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}')"
echo "$INIT_RESP" | grep -q '"serverInfo"' || { echo "FAIL: initialize"; echo "$INIT_RESP"; exit 1; }
SESSION="$(grep -i '^mcp-session-id:' "$WORKDIR/headers.txt" | tr -d '\r' | awk '{print $2}' || true)"
mcp_call '{"jsonrpc":"2.0","method":"notifications/initialized"}' "$SESSION" >/dev/null || true
echo "OK mcp initialize${SESSION:+ (session $SESSION)}"

TOOLS="$(mcp_call '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' "$SESSION")"
for tool in prepare_phone_call start_phone_call get_call_result end_phone_call get_phone_call wait_for_call_event answer_call_question; do
  echo "$TOOLS" | grep -q "\"$tool\"" || { echo "FAIL: tool $tool missing"; echo "$TOOLS"; exit 1; }
done
echo "OK tools/list (7 tools present)"

PREPARE='{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"prepare_phone_call","arguments":{
  "context":{
    "owner":{"display_name":"Irvin","timezone":"America/Los_Angeles","callback_number":"+15550000001"},
    "target":{"name":"Smoke Target","phone":"+15550000002"},
    "objective":"Smoke test objective",
    "escalation":{"mode":"end_call","owner_phone":"+15550000001"}},
  "requested_by_owner":true}}}'
PREP_RESP="$(mcp_call "$(echo "$PREPARE" | tr -d '\n')" "$SESSION")"
echo "$PREP_RESP" | grep -q 'plan_id' || { echo "FAIL: prepare_phone_call returned no plan_id"; echo "$PREP_RESP"; exit 1; }
echo "OK prepare_phone_call (plan persisted)"

# Unauthenticated request must be rejected
STATUS="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$MCP_URL" -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":9,"method":"tools/list"}')"
[ "$STATUS" = "401" ] || [ "$STATUS" = "403" ] || { echo "FAIL: unauthenticated MCP got $STATUS"; exit 1; }
echo "OK auth rejection ($STATUS)"

echo "SMOKE PASS"
