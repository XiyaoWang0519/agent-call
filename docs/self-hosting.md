# Self-hosting

This is the operator guide for running **your own** Agent Call instance. Choose one golden path:

1. **Local dummy** — no provider credentials; `/healthz`, MCP initialize, seven tools, `prepare_phone_call`. `start_phone_call` returns `live_calls_disabled`.
2. **Local live (prepare-only first)** — your Twilio/OpenAI accounts and a public HTTPS tunnel. Prepare a plan; do not start a call until you intend to ring a phone.
3. **Supported web host** — one Fly Machine you own, using the template `fly.toml`.

Managed Agent Call is a forthcoming private service. It is not configured from this file.

> [!CAUTION]
> Filling real Twilio and OpenAI credentials and exposing a public webhook URL can place **real billable phone calls**. Stay on the evaluation profile until you intend to spend money and ring a real phone.

## Local dummy

No OpenAI, Twilio, or Exa accounts. The evaluation profile fills obvious dummy values and disables live calls. The default listener is loopback.

```bash
uv sync --all-groups --frozen
uv run agent-call doctor --dummy
uv run agent-call serve --profile evaluation --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
curl -fsS http://127.0.0.1:8000/healthz
uv run agent-call doctor --prepare-only
uv run agent-call smoke-prepare
```

Compose (named volume, host loopback only, non-root runtime user, no credentials
in the image; `.dockerignore` keeps `.git`, env files, and local data out of the
build context):

```bash
docker compose up --build
uv run agent-call smoke-prepare
```

`uv run python -m app` is equivalent to `uv run agent-call`. Evaluation refuses `--host 0.0.0.0` unless you pass `--unsafe-bind`. Bind publishing is a host/Compose concern; the Settings object does not own it.

Troubleshooting: [troubleshooting.md](troubleshooting.md).

## Local live (prepare-only)

### Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- A voice-enabled Twilio account (E.164 caller ID, outbound SIP, Conference Participant AMD)
- An OpenAI project with Realtime SIP access and a webhook signing secret
- An Exa API key (in-call web search)
- A stable public HTTPS origin for webhooks

You do **not** need those accounts for dummy boot or for lint/tests.

### Configure

```bash
test -e .env.local || cp .env.example .env.local   # guarded: keeps a saved key
uv sync --all-groups --frozen
```

Fill every required **core** value in `.env.local`. Generate `MCP_BEARER_TOKEN`, `DEBUG_API_TOKEN`, and `DEPLOY_GUARD_TOKEN` with `openssl rand -hex 32`. Leave `ALLOWED_COUNTRY_CODES` unset in dotenv files (the app defaults to `["+1"]`). Never commit env files — `.gitignore` already excludes them.

Booting a **live** process requires every variable in `Settings.require_runtime_configuration` (`app/settings.py`) or the lifespan aborts at startup.

Do not put `ALLOWED_COUNTRY_CODES` in `.env.local`. pydantic-settings JSON-decodes list-typed fields, so `ALLOWED_COUNTRY_CODES=+1` crashes boot. Omit it and rely on the `+1` default. For a non-default allowlist, set a JSON list in the process environment (for example `ALLOWED_COUNTRY_CODES=["+1"]`).

Do not run `doctor --live-ready` yet. That command includes DNS/TLS and health-path checks, so it is truthful only after the live server and a public HTTPS origin exist.

### Boot the live server

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

`uvicorn app.main:app` defaults to the live profile (unset `AGENT_CALL_PROFILE`). Dummy boot is `--profile evaluation` (or `AGENT_CALL_PROFILE=evaluation`). `agent-call serve` loads the same Settings object uvicorn will build, so evaluation-from-dotenv still refuses a non-loopback bind unless `--unsafe-bind` is passed. Core credentials in `.env.local` or the process environment without an explicit profile are refused — pass `--profile live` or set `AGENT_CALL_PROFILE=live` (this will place billable calls). Evaluation bind is CLI `--unsafe-bind` only; `AGENT_CALL_UNSAFE_BIND` is not a Settings field and is not honored.

### Expose webhooks

Webhooks need a public HTTPS origin. Start the server, then tunnel port 8000:

```bash
ngrok http 8000            # or: cloudflared tunnel --url http://localhost:8000
```

Set `PUBLIC_BASE_URL` to the exact HTTPS origin the tunnel prints — no trailing slash — and restart. This value is security-critical: Twilio signs the full public callback URL.

In **OpenAI Platform → Project → Webhooks**, create `https://YOUR_HOST/webhooks/openai`, subscribe it to `realtime.call.incoming`, and copy the signing secret to `OPENAI_WEBHOOK_SECRET`. The OpenAI project in the SIP URI is `OPENAI_PROJECT_ID`.

No static Twilio webhook is needed — every Conference Participant request carries its own signed status, conference, and AMD callback URLs. Confirm the Twilio account can call the OpenAI SIP URI and can use AMD on `/Participants`.

### Check live connectivity

After the live-profile process is up and `PUBLIC_BASE_URL` is a reachable HTTPS origin:

```bash
uv run agent-call doctor --live-ready
```

`doctor --live-ready` reports missing, blank, or malformed values **without printing secrets or full E.164 numbers**. It also probes SQLite writability (without altering the real database), DNS/TLS plus `/healthz` and `/webhooks/openai` on `PUBLIC_BASE_URL`, and non-billable Twilio/OpenAI metadata. Exa search and the OpenAI webhook signing secret stay **UNVERIFIED** because there is no side-effect-free check; the command then exits 1 and does not claim complete live readiness. It fails if `AGENT_CALL_PROFILE=evaluation`.

Then run a prepare-only MCP client call (or `uv run agent-call smoke-prepare` against that process using your live MCP bearer). Do not invoke `start_phone_call` until a separately authorized canary.

### Point an agent at it

**OpenClaw**

```bash
openclaw mcp add agent-call \
  --url https://YOUR_HOST/mcp/ \
  --transport streamable-http \
  --header "Authorization: Bearer <MCP_BEARER_TOKEN>" \
  --header "X-Agent-User-Id: <ALLOWED_AGENT_USER_ID>"
openclaw mcp probe agent-call
```

Optional reverse channel (wake on mid-call questions and post-call summaries): enable hooks in `openclaw.json`, then set:

```text
AGENT_PUSH_ENABLED=true
AGENT_WEBHOOK_URL=https://YOUR_GATEWAY/hooks/agent
AGENT_WEBHOOK_TOKEN=<hooks.token>
```

**Hermes Agent** (`~/.hermes/config.yaml`)

```yaml
mcp_servers:
  agent-call:
    url: "https://YOUR_HOST/mcp/"
    headers:
      Authorization: "Bearer <MCP_BEARER_TOKEN>"
      X-Agent-User-Id: "<ALLOWED_AGENT_USER_ID>"
```

Hermes has no inbound webhook today — leave `AGENT_PUSH_ENABLED=false` and rely on `wait_for_call_event` polling (fully supported; push is best-effort and non-canonical).

**Grok Bot** (private custom MCP connector, not Grok Build): see [docs/grok-bot/README.md](grok-bot/README.md). OpenClaw and Hermes keep using `/mcp/` with both headers. Grok Bot should use the optional OAuth endpoint `/grok/mcp/` so the connector UI only needs Name and Server URL. OAuth is **off by default** and is a self-hosted, single-owner authorization flow — not a managed multi-tenant product. Leave `AGENT_PUSH_ENABLED=false` and poll `wait_for_call_event`.

Manual MCP client config:

```text
URL:             https://YOUR_HOST/mcp/
Authorization:   Bearer MCP_BEARER_TOKEN
X-Agent-User-Id: ALLOWED_AGENT_USER_ID
Transport:       Streamable HTTP
```

Both the bearer token and `X-Agent-User-Id` are required on every MCP request.

## Optional Grok OAuth (self-hosted, single-owner)

This is **not** the managed multi-tenant design. Each operator owns their
deployment, domain, database, secrets, OAuth authorization, Twilio account, and
OpenAI account. Operators who do not use Grok Bot leave OAuth disabled and keep
the dual-header `/mcp/` setup.

**Implemented locally; pending live Grok OAuth verification.**

1. Generate a dedicated owner secret and store the plaintext only in a password
   manager. Hash it; the server stores only the Argon2id hash:

   ```bash
   uv run python scripts/hash_grok_oauth_owner_secret.py
   ```

2. Generate signing and storage keys (`openssl rand -hex 32` for each).
3. Set:

   ```text
   GROK_MCP_OAUTH_ENABLED=true
   GROK_MCP_OAUTH_OWNER_SECRET_HASH=<argon2id hash>
   GROK_MCP_OAUTH_SIGNING_KEY=<32+ character secret>
   GROK_MCP_OAUTH_STORAGE_ENCRYPTION_KEY=<32+ character secret>
   GROK_MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS=3600
   GROK_MCP_OAUTH_REFRESH_TOKEN_TTL_DAYS=90
   GROK_MCP_OAUTH_AUTH_CODE_TTL_SECONDS=300
   ```

4. Confirm no active calls, then deploy only after explicit approval.
5. In Grok, add a custom connector using **only Name and Server URL**:
   `https://YOUR_HOST/grok/mcp/`
6. Complete browser authorization on this Agent Call instance. The owner secret
   is entered on Agent Call's page; it never passes through Grok chat.
7. Verify with `tools/list`, then `prepare_phone_call` only. Do not call
   `start_phone_call` until you intend to place a billable call.

Exact URLs when OAuth is enabled (`PUBLIC_BASE_URL=https://YOUR_HOST`):

| Purpose | URL |
| --- | --- |
| Grok MCP (Streamable HTTP) | `https://YOUR_HOST/grok/mcp/` |
| Protected resource metadata | `https://YOUR_HOST/.well-known/oauth-protected-resource/grok/mcp/` |
| Authorization server metadata | `https://YOUR_HOST/.well-known/oauth-authorization-server` |
| Authorize | `https://YOUR_HOST/authorize` |
| Owner consent | `https://YOUR_HOST/grok/oauth/consent` |
| Token | `https://YOUR_HOST/token` |
| Client registration | `https://YOUR_HOST/register` |
| Token revocation | `https://YOUR_HOST/revoke` |
| Revoke all Grok families | `POST https://YOUR_HOST/internal/grok-oauth/revoke-all` with `Authorization: Bearer $DEBUG_API_TOKEN` |
| Legacy MCP (unchanged) | `https://YOUR_HOST/mcp/` |

Access tokens last **1 hour**. Refresh tokens last up to **90 days** and rotate
on every use, so a normal access-token expiry does **not** require another owner
login. Reauthentication is required when the refresh-token maximum lifetime
expires, authorization is revoked, refresh-token reuse is detected, the
connector is removed, the OAuth signing key or owner secret is rotated, or
persistent OAuth state is cleared.

Public `/register` (dynamic client registration) stays compatible with Grok's
connector UI: there is no extra callback-URI allowlist. To keep SQLite bounded,
the host stores at most **64** OAuth clients. Unused clients older than **30
days** are evicted first; if the table is still full, the oldest unused clients
are evicted to make room. A client counts as in use while it has an unrevoked
refresh family that has not expired. If every remaining client is still in use,
further registration is rejected.

Each successful registration (and other OAuth lifecycle events) appends an
`oauth_audit` row. Rows older than **90 days** are removed, and the table is
capped at the **2048** newest records. Outstanding authorization transactions
are capped at **64** globally and **8** per client; expired or consumed
transactions are deleted at process start, on each `/authorize`, and whenever a
token pair is issued. Expired authorization codes, access JTIs, refresh tokens
(consumed or not), and token families are deleted at process start and whenever
a token pair is issued; valid durable refresh families are left untouched.

Changing `PUBLIC_BASE_URL` (including a free-tier tunnel restart) does not
re-derive the storage encryption key. Existing OAuth ciphertext stays readable;
the operator still removes the old Grok connector and adds the new URL.
Rotating `GROK_MCP_OAUTH_OWNER_SECRET_HASH` or `GROK_MCP_OAUTH_SIGNING_KEY`
revokes existing Grok families on the next boot. Rotating
`GROK_MCP_OAUTH_STORAGE_ENCRYPTION_KEY` fails closed until the OAuth tables are
intentionally cleared. Do not change these secrets while a call is active.

Rollback: if a Grok OAuth deploy misbehaves, set `GROK_MCP_OAUTH_ENABLED=false`
(or roll back the previous image) after the deployment lease is acquired. Legacy
`/mcp/` clients are unaffected. Grok Voice remains out of scope. Real calls
remain billable.

## Deploying (your own Fly app)

Create **your** Fly app and volume. Replace `YOUR_FLY_APP_NAME` in `fly.toml`
before deploying. Do not use a maintainer overlay.

```bash
flyctl apps create YOUR_FLY_APP_NAME
# Update fly.toml `app` to the same name.
flyctl volumes create agent_call_data --region lax --size 1 -a YOUR_FLY_APP_NAME

# The service runs as fixed non-root UID 10001. The entrypoint also repairs
# legacy root-owned volume contents during upgrades.
flyctl machine run python:3.13-slim sleep infinity \
  --app YOUR_FLY_APP_NAME --region lax --volume agent_call_data:/data \
  --restart no --detach
flyctl machine list -a YOUR_FLY_APP_NAME
flyctl ssh console -a YOUR_FLY_APP_NAME -C \
  'chown 10001:10001 /data && chmod 750 /data'
flyctl machine destroy <maintenance-machine-id> -a YOUR_FLY_APP_NAME --force
```

Set `PUBLIC_BASE_URL` with secrets (HTTPS origin of **your** app, no path):

```bash
flyctl secrets set PUBLIC_BASE_URL=https://YOUR_FLY_APP_NAME.fly.dev -a YOUR_FLY_APP_NAME
flyctl config validate
flyctl deploy --ha=false --remote-only -a YOUR_FLY_APP_NAME
curl -fsS https://YOUR_FLY_APP_NAME.fly.dev/healthz
```

Set every required live value from `.env.example` with `flyctl secrets set` or `flyctl secrets import`. Point the OpenAI project webhook at `https://YOUR_HOST/webhooks/openai` (subscribed to `realtime.call.incoming`). After rotating the signing secret:

```bash
flyctl secrets deploy -a YOUR_FLY_APP_NAME
flyctl status -a YOUR_FLY_APP_NAME
```

> [!IMPORTANT]
> Keep exactly **one** Machine. SQLite state is volume-local; a second Machine would not share call state. `render.yaml` is an alternate Docker web service; it is not the Fly template.

The GitHub deploy workflow is upstream-maintainer-only and does not run on
forks. Forks should not assume it will ship their app. Deploy **your** Fly
app from `fly.toml` with `YOUR_FLY_APP_NAME`.

## Rollback

If a bad deploy reaches **your** production, roll back first (do not redeploy while diagnosing):

```bash
# Confirm no active calls (POST acquires the lease; HTTP 409 means wait)
curl --fail-with-body -sS --request POST \
  -H "Authorization: Bearer $DEPLOY_GUARD_TOKEN" \
  https://YOUR_HOST/internal/deployment-lock

# List image references and redeploy the previous healthy image
flyctl releases --image -a YOUR_FLY_APP_NAME
flyctl deploy --image <previous-image-reference> -a YOUR_FLY_APP_NAME --ha=false --remote-only

# Verify
curl -fsS https://YOUR_HOST/healthz
```

Redeploying a prior image restores only the application image without a full rebuild; it does not roll back the current Fly configuration, environment variables, or secrets. Ship a new build only after calls are idle and local checks pass (`ruff`, `mypy`, `pytest`).

## Live SIP canary

See the [README](../README.md#live-sip-canary). Those commands place a real billable call. Contributors should not run them casually; they are for the operator of a deployment that already has real credentials and a human on `OWNER_PHONE_E164`.

## Tuning knobs (timeouts, VAD, search)

- Live-call control requests use `OPENAI_CONNECT_TIMEOUT_SECONDS` and `OPENAI_HTTP_TIMEOUT_SECONDS` (3 and 10 seconds by default) with no SDK retries; post-call extraction uses `OPENAI_EXTRACTION_TIMEOUT_SECONDS` and keeps its single application-level retry.
- Twilio requests use the pooled, no-retry transport bounded by `TWILIO_HTTP_TIMEOUT_SECONDS`.
- `SEMANTIC_VAD_EAGERNESS` defaults to `auto` (OpenAI's medium-eagerness behavior); set `high` for quicker turn completion.
- `OPENAI_KEEPALIVE_EXPIRY_SECONDS=60` keeps the control-plane TLS connection reusable between sporadic calls; only set it to a bounded 5–300 second value.
- The voice model can call `search_web` for current or uncertain facts. The application fixes Exa Search to `type=auto`, 10 results, moderation, and token-efficient highlights, with a three-second wall-clock deadline controlled by `EXA_SEARCH_TIMEOUT_SECONDS`.
- Locked dependency versions live in `uv.lock` (authoritative).

## API/schema decisions

The implementation follows the current [OpenAI Realtime SIP guide](https://developers.openai.com/api/docs/guides/realtime-sip), [server-side controls guide](https://developers.openai.com/api/docs/guides/realtime-server-controls), [Realtime prompting guide](https://developers.openai.com/api/docs/guides/realtime-models-prompting), [webhook guide](https://developers.openai.com/api/docs/guides/webhooks), [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs), [Twilio Conference Participant reference](https://www.twilio.com/docs/voice/api/conference-participant-resource), [Twilio AMD guide](https://www.twilio.com/docs/voice/answering-machine-detection), and [Twilio request validation guide](https://www.twilio.com/docs/usage/security).

Live-schema deviations from the original contract are intentional:

- GA Realtime audio formats are objects such as `{"type":"audio/pcmu"}` when used, but SIP accept/session.update payloads must omit `audio.*.format`. OpenAI negotiates G.711 with the carrier; forcing a format has been observed to clobber PCMU into PCM and leave the callee with silence or static while WebSocket transcripts still advance.
- The current Conference Participants API has no `async_amd` parameter. AMD on Participants is asynchronous by design; the installed SDK uses `machine_detection="DetectMessageEnd"` plus `amd_status_callback` and `amd_status_callback_method`.
- OpenAI call accept is invoked through the installed typed SDK; hangup has no JSON body. REFER's live request field is `target_uri`, but v1 transfer deliberately does not use REFER.
- The installed transcription schema permits `INPUT_TRANSCRIPTION_DELAY` only for `gpt-realtime-whisper`, with `minimal|low|medium|high|xhigh` values.
- The OpenAI SIP agent participant is created with `early_media=false` and is explicitly unmuted before the model's opening turn, because Twilio mutes legs that join with `start_conference_on_enter=false` until the conference starts.

## Result semantics

Telephony state and extraction state remain separate. A successful phone call whose extractor fails stays `call_status=completed`, gets `finalization_status=failed`, `outcome=unknown`, retains its raw transcript, and tells the owner to review it. The service saves a telephony-only result and transcript transactionally before making the external Responses API extraction request. Only transient connection, timeout, or rate-limit failures receive one retry.
