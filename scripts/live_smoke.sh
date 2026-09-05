#!/usr/bin/env bash
# Local live smoke test: boot the app with dummy credentials and a temp SQLite DB,
# then drive /healthz and the MCP endpoint as a Grok-compatible Streamable HTTP
# client (initialize, exact tools/list, prepare_phone_call, either-credential
# auth rejection). When OAuth is disabled, discovery must fail closed. A second
# local process then boots the OAuth-enabled path and checks discovery, DCR,
# authorization, and that the same seven tools remain listed. No Twilio or
# OpenAI network calls; prepare does not dial; start_phone_call is never invoked.
# Usage: scripts/live_smoke.sh [port]
set -euo pipefail

PORT="${1:-8765}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORKDIR="$(mktemp -d)"
SERVER_PID=""
OAUTH_SERVER_PID=""
trap 'for pid in "$SERVER_PID" "$OAUTH_SERVER_PID"; do if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi; done; rm -rf "$WORKDIR"' EXIT

export OPENAI_API_KEY="sk-dummy" OPENAI_WEBHOOK_SECRET="whsec_dummy" OPENAI_PROJECT_ID="proj_dummy"
export EXA_API_KEY="exa-dummy"
export TWILIO_ACCOUNT_SID="ACdummy" TWILIO_AUTH_TOKEN="dummy" TWILIO_CALLER_ID="+15550000000"
export OWNER_PHONE_E164="+15550000001" ALLOWED_AGENT_USER_ID="smoke-user"
export MCP_BEARER_TOKEN="smoke-bearer" DEBUG_API_TOKEN="smoke-debug" DEPLOY_GUARD_TOKEN="smoke-deploy"
export PUBLIC_BASE_URL="https://smoke.example.com"
export DATABASE_URL="sqlite:///$WORKDIR/smoke.db"
export AGENT_CALL_PROFILE="${AGENT_CALL_PROFILE:-evaluation}"
export AGENT_PUSH_ENABLED="false" ASK_AGENT_ENABLED="true"

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
HDR=(-H "Authorization: Bearer $MCP_BEARER_TOKEN" -H "X-Agent-User-Id: smoke-user"
     -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")

mcp_call() { # $1=json body, $2=session id (optional)
  local extra=()
  [ -n "${2:-}" ] && extra=(-H "mcp-session-id: $2")
  curl -fsS -D "$WORKDIR/headers.txt" "${HDR[@]}" ${extra[@]+"${extra[@]}"} -X POST "$MCP_URL" -d "$1"
}

# Parse JSON or Streamable HTTP SSE (`data: {...}`) from an MCP response file.
mcp_check() {
  python3 - "$1" "$2" <<'PY'
import json, sys

def load_mcp(path: str) -> dict:
    raw = open(path, encoding="utf-8").read().strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                return json.loads(payload)
    raise SystemExit(f"could not parse MCP JSON from {path}")

path, mode = sys.argv[1], sys.argv[2]
payload = load_mcp(path)
if mode == "tools":
    expected = {
        "prepare_phone_call",
        "start_phone_call",
        "get_call_result",
        "end_phone_call",
        "get_phone_call",
        "wait_for_call_event",
        "answer_call_question",
    }
    names = {tool["name"] for tool in payload["result"]["tools"]}
    if names != expected:
        raise SystemExit(
            f"FAIL: tools/list expected {sorted(expected)}, got {sorted(names)}"
        )
    print("OK tools/list (exactly 7 tools)")
elif mode == "prepare":
    result = payload["result"]
    if result.get("isError"):
        raise SystemExit(f"FAIL: prepare_phone_call error: {result}")
    inner = result.get("structuredContent")
    if not isinstance(inner, dict) or not inner.get("plan_id"):
        text = result["content"][0]["text"]
        parsed = json.loads(text)
        inner = parsed.get("result", parsed) if isinstance(parsed, dict) else {}
    plan_id = inner.get("plan_id") if isinstance(inner, dict) else None
    if not plan_id:
        raise SystemExit(
            f"FAIL: prepare_phone_call returned no persisted plan_id: {result}"
        )
    print(f"OK prepare_phone_call (plan persisted {plan_id})")
else:
    raise SystemExit(f"unknown mcp_check mode {mode}")
PY
}

INIT_RESP="$(mcp_call '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}')" || { echo "FAIL: initialize (transport)"; cat "$WORKDIR/server.log"; exit 1; }
echo "$INIT_RESP" | grep -q '"serverInfo"' || { echo "FAIL: initialize"; echo "$INIT_RESP"; exit 1; }
SESSION="$(grep -i '^mcp-session-id:' "$WORKDIR/headers.txt" | tr -d '\r' | awk '{print $2}' || true)"
mcp_call '{"jsonrpc":"2.0","method":"notifications/initialized"}' "$SESSION" >/dev/null || true
echo "OK mcp initialize${SESSION:+ (session $SESSION)}"

TOOLS="$(mcp_call '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' "$SESSION")" || { echo "FAIL: tools/list (transport)"; cat "$WORKDIR/server.log"; exit 1; }
printf '%s' "$TOOLS" >"$WORKDIR/tools.json"
mcp_check "$WORKDIR/tools.json" tools

PREPARE='{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"prepare_phone_call","arguments":{
  "context":{
    "owner":{"display_name":"the owner","timezone":"America/Los_Angeles","callback_number":"+15550000001"},
    "target":{"name":"Smoke Target","phone":"+15550000002"},
    "objective":"Smoke test objective",
    "escalation":{"mode":"end_call","owner_phone":"+15550000001"}},
  "authority_basis":"Owner requested this local Grok-compatible smoke test",
  "requested_by_owner":true}}}'
PREP_RESP="$(mcp_call "$(echo "$PREPARE" | tr -d '\n')" "$SESSION")" || { echo "FAIL: prepare_phone_call (transport)"; cat "$WORKDIR/server.log"; exit 1; }
printf '%s' "$PREP_RESP" >"$WORKDIR/prepare.json"
mcp_check "$WORKDIR/prepare.json" prepare

# Unauthenticated request must be rejected
STATUS="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$MCP_URL" -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":9,"method":"tools/list"}')" || { echo "FAIL: unauthenticated MCP request (transport)"; cat "$WORKDIR/server.log"; exit 1; }
[ "$STATUS" = "401" ] || [ "$STATUS" = "403" ] || { echo "FAIL: unauthenticated MCP got $STATUS"; exit 1; }
echo "OK auth rejection ($STATUS)"

# Either required credential missing must fail (Grok-compatible client auth).
MISSING_BEARER="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$MCP_URL" \
  -H "Content-Type: application/json" -H "X-Agent-User-Id: smoke-user" \
  -d '{"jsonrpc":"2.0","id":10,"method":"tools/list"}')" || { echo "FAIL: missing bearer (transport)"; cat "$WORKDIR/server.log"; exit 1; }
[ "$MISSING_BEARER" = "401" ] || [ "$MISSING_BEARER" = "403" ] || { echo "FAIL: missing bearer MCP got $MISSING_BEARER"; exit 1; }
echo "OK missing bearer rejection ($MISSING_BEARER)"

MISSING_USER="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$MCP_URL" \
  -H "Content-Type: application/json" -H "Authorization: Bearer $MCP_BEARER_TOKEN" \
  -d '{"jsonrpc":"2.0","id":11,"method":"tools/list"}')" || { echo "FAIL: missing user id (transport)"; cat "$WORKDIR/server.log"; exit 1; }
[ "$MISSING_USER" = "401" ] || [ "$MISSING_USER" = "403" ] || { echo "FAIL: missing user id MCP got $MISSING_USER"; exit 1; }
echo "OK missing X-Agent-User-Id rejection ($MISSING_USER)"

# OAuth is disabled by default: the Grok endpoint and discovery must fail closed.
GROK_STATUS="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/grok/mcp/" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":12,"method":"tools/list"}')" || true
[ "$GROK_STATUS" = "404" ] || [ "$GROK_STATUS" = "405" ] || {
  echo "FAIL: disabled Grok MCP got $GROK_STATUS"; exit 1
}
echo "OK disabled Grok MCP closed ($GROK_STATUS)"
DISCOVERY_STATUS="$(curl -s -o /dev/null -w '%{http_code}' \
  "http://127.0.0.1:$PORT/.well-known/oauth-authorization-server")" || true
[ "$DISCOVERY_STATUS" = "404" ] || {
  echo "FAIL: OAuth discovery advertised while disabled ($DISCOVERY_STATUS)"; exit 1
}
echo "OK OAuth discovery absent while disabled ($DISCOVERY_STATUS)"

# --- OAuth-enabled local path (no provider, tunnel, or phone call) ---
OAUTH_PORT=$((PORT + 1))
OAUTH_SECRET="smoke-oauth-owner-secret"
OAUTH_HASH="$(uv run python -c "from app.grok_oauth.crypto import hash_owner_secret; print(hash_owner_secret('$OAUTH_SECRET'))")"
export GROK_MCP_OAUTH_ENABLED="true"
export GROK_MCP_OAUTH_OWNER_SECRET_HASH="$OAUTH_HASH"
export GROK_MCP_OAUTH_SIGNING_KEY="$(printf 's%.0s' {1..64})"
export GROK_MCP_OAUTH_STORAGE_ENCRYPTION_KEY="$(printf 'e%.0s' {1..64})"
export DATABASE_URL="sqlite:///$WORKDIR/oauth.db"

uv run uvicorn app.main:app --host 127.0.0.1 --port "$OAUTH_PORT" >"$WORKDIR/oauth-server.log" 2>&1 &
OAUTH_SERVER_PID=$!
for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:$OAUTH_PORT/healthz" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$OAUTH_SERVER_PID" 2>/dev/null; then
    echo "FAIL: OAuth server exited during startup"; cat "$WORKDIR/oauth-server.log"; exit 1
  fi
  sleep 0.2
done
curl -fsS "http://127.0.0.1:$OAUTH_PORT/healthz" >/dev/null || {
  echo "FAIL: OAuth /healthz"; cat "$WORKDIR/oauth-server.log"; exit 1
}
echo "OK OAuth /healthz"

if ! uv run python - "$OAUTH_PORT" "$OAUTH_SECRET" <<'PY'
import base64, hashlib, json, os, re, secrets, sys, urllib.error, urllib.parse, urllib.request

port, owner_secret = sys.argv[1], sys.argv[2]
base = f"http://127.0.0.1:{port}"
resource = "https://smoke.example.com/grok/mcp/"
expected_tools = {
    "prepare_phone_call",
    "start_phone_call",
    "get_call_result",
    "end_phone_call",
    "get_phone_call",
    "wait_for_call_event",
    "answer_call_question",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


opener = urllib.request.build_opener(NoRedirect)


def request(method, url, *, data=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, dict(resp.headers), body
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


status, _, body = request("GET", f"{base}/.well-known/oauth-authorization-server")
if status != 200:
    raise SystemExit(f"discovery status {status}: {body[:300]!r}")
advertised = json.loads(body)
if not advertised.get("registration_endpoint", "").endswith("/register"):
    raise SystemExit(f"registration_endpoint missing: {advertised}")
if advertised.get("code_challenge_methods_supported") != ["S256"]:
    raise SystemExit(f"PKCE S256 not advertised: {advertised}")
print("OK OAuth discovery")

reg_status, _, reg_body = request(
    "POST",
    f"{base}/register",
    data=json.dumps(
        {
            "redirect_uris": ["https://grok.example/callback"],
            "client_name": "Smoke Connector",
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "agent-call:use",
        }
    ).encode(),
    headers={"Content-Type": "application/json"},
)
if reg_status != 201:
    raise SystemExit(f"DCR status {reg_status}: {reg_body[:500]!r}")
registered = json.loads(reg_body)
print("OK DCR /register")

verifier = secrets.token_urlsafe(64)
challenge = (
    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
)
query = urllib.parse.urlencode(
    {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": "https://grok.example/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "agent-call:use",
        "resource": resource,
        "state": "smoke-state",
    }
)
auth_status, auth_headers, auth_body = request("GET", f"{base}/authorize?{query}")
if auth_status != 302:
    raise SystemExit(f"authorize status {auth_status}: {auth_body[:300]!r}")
consent_path = auth_headers.get("Location") or auth_headers.get("location")
if not consent_path or "/grok/oauth/consent" not in consent_path:
    raise SystemExit(f"authorize did not redirect to consent: {consent_path}")
print("OK /authorize")

if consent_path.startswith("/"):
    consent_url = base + consent_path
else:
    consent_url = consent_path
page_status, _, page_body = request("GET", consent_url)
page = page_body.decode()
if page_status != 200:
    raise SystemExit(f"consent status {page_status}: {page[:300]!r}")
if "Unverified client" not in page:
    raise SystemExit("consent page missing unverified warning")
if registered["client_id"] not in page:
    raise SystemExit("consent page missing exact client_id")
if "https://grok.example/callback" not in page:
    raise SystemExit("consent page missing exact redirect URI")
csrf = re.search(r'name="csrf_token" value="([^"]+)"', page)
tx = re.search(r'name="tx" value="([^"]+)"', page)
if not csrf or not tx:
    raise SystemExit("consent page missing csrf/tx fields")
print("OK owner consent identity")

form = urllib.parse.urlencode(
    {
        "tx": tx.group(1),
        "csrf_token": csrf.group(1),
        "owner_secret": owner_secret,
        "action": "approve",
    }
).encode()
approve_status, approve_headers, approve_body = request(
    "POST",
    f"{base}/grok/oauth/consent",
    data=form,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
if approve_status != 302:
    raise SystemExit(f"approve status {approve_status}: {approve_body[:300]!r}")
location = approve_headers.get("Location") or approve_headers.get("location") or ""
code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code", [None])[0]
if not code:
    raise SystemExit(f"approve redirect missing code: {location}")

token_status, _, token_body = request(
    "POST",
    f"{base}/token",
    data=urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://grok.example/callback",
            "client_id": registered["client_id"],
            "client_secret": registered["client_secret"],
            "code_verifier": verifier,
            "resource": resource,
        }
    ).encode(),
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
if token_status != 200:
    raise SystemExit(f"token status {token_status}: {token_body[:500]!r}")
access_token = json.loads(token_body)["access_token"]
print("OK OAuth token")


def mcp_json(path, payload, headers):
    status, hdrs, body = request(
        "POST",
        base + path,
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    if status != 200:
        raise SystemExit(f"{path} status {status}: {body[:500]!r}")
    text = body.decode().strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk and chunk != "[DONE]":
                    parsed = json.loads(chunk)
                    break
        if parsed is None:
            raise SystemExit(f"could not parse MCP JSON from {path}: {text[:300]!r}")
    return parsed, hdrs


oauth_headers = {
    "Authorization": f"Bearer {access_token}",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
init_payload, init_headers = mcp_json(
    "/grok/mcp/",
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "smoke-oauth", "version": "0"},
        },
    },
    oauth_headers,
)
if "serverInfo" not in init_payload.get("result", {}):
    raise SystemExit(f"OAuth MCP initialize failed: {init_payload}")
session = init_headers.get("mcp-session-id") or init_headers.get("Mcp-Session-Id")
if session:
    oauth_headers = {**oauth_headers, "mcp-session-id": session}
listed, _ = mcp_json("/grok/mcp/", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, oauth_headers)
names = {tool["name"] for tool in listed["result"]["tools"]}
if names != expected_tools:
    raise SystemExit(f"OAuth tools/list expected {sorted(expected_tools)}, got {sorted(names)}")
print("OK OAuth tools/list (exactly 7 tools)")

legacy_headers = {
    "Authorization": f"Bearer {os.environ['MCP_BEARER_TOKEN']}",
    "X-Agent-User-Id": "smoke-user",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
legacy, _ = mcp_json("/mcp/", {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, legacy_headers)
legacy_names = {tool["name"] for tool in legacy["result"]["tools"]}
if legacy_names != expected_tools:
    raise SystemExit(f"legacy tools/list expected {sorted(expected_tools)}, got {sorted(legacy_names)}")
print("OK legacy /mcp/ tools/list while OAuth enabled (exactly 7 tools)")
PY
then
  echo "FAIL: OAuth-enabled discovery/DCR/authorization smoke"
  cat "$WORKDIR/oauth-server.log"
  exit 1
fi

echo "SMOKE PASS"
