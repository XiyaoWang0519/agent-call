#!/usr/bin/env bash
# Boot the app locally with dummy credentials and a temp SQLite DB so the HTTP
# and MCP endpoints are reachable without real Twilio/OpenAI credentials. The
# server can answer /healthz and prepare_phone_call, but cannot place a real
# call (that needs real credentials + a public HTTPS tunnel). For a scripted
# end-to-end check that exits, use scripts/live_smoke.sh instead.
#
# Usage: scripts/dev_server.sh [port]   (default port 8000)
set -euo pipefail

PORT="${1:-8000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# uv is installed to ~/.local/bin by the environment install step.
export PATH="$HOME/.local/bin:$PATH"

# Dummy dev credentials (same fakes as scripts/live_smoke.sh). Real values are
# only needed to place a live call; these satisfy Settings.require_runtime_configuration.
: "${OPENAI_API_KEY:=sk-dummy}"
: "${OPENAI_WEBHOOK_SECRET:=whsec_dummy}"
: "${OPENAI_PROJECT_ID:=proj_dummy}"
: "${EXA_API_KEY:=exa-dummy}"
: "${TWILIO_ACCOUNT_SID:=ACdummy}"
: "${TWILIO_AUTH_TOKEN:=dummy}"
: "${TWILIO_CALLER_ID:=+15550000000}"
: "${OWNER_PHONE_E164:=+15550000001}"
: "${ALLOWED_AGENT_USER_ID:=local-dev-user}"
: "${MCP_BEARER_TOKEN:=local-dev-bearer}"
: "${DEBUG_API_TOKEN:=local-dev-debug}"
: "${DEPLOY_GUARD_TOKEN:=local-dev-deploy}"
: "${PUBLIC_BASE_URL:=https://local-dev.example.com}"
: "${DATABASE_URL:=sqlite:///${ROOT}/agent_call.db}"
export OPENAI_API_KEY OPENAI_WEBHOOK_SECRET OPENAI_PROJECT_ID EXA_API_KEY
export TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN TWILIO_CALLER_ID
export OWNER_PHONE_E164 ALLOWED_AGENT_USER_ID MCP_BEARER_TOKEN DEBUG_API_TOKEN DEPLOY_GUARD_TOKEN
export PUBLIC_BASE_URL DATABASE_URL
export AGENT_CALL_PROFILE="${AGENT_CALL_PROFILE:-evaluation}"

cd "$ROOT"
exec uv run uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
