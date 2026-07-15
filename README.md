# Poke Phone-Call Bridge

Single-user FastAPI + FastMCP service that lets Poke prepare, confirm, start, monitor, and end outbound phone calls for Irvin. Twilio originates an OpenAI SIP agent leg into a private conference, the service accepts and prewarms `gpt-realtime-2.1` over a sideband WebSocket, and only then dials the callee. SQLite stores plans, state, ordered transcripts, and deterministic final results.

## Safety and operating model

- The MCP endpoint always requires its bearer token. It also validates the configured Poke user ID whenever Poke supplies the header; Poke's refresh follow-up currently omits it.
- Every OpenAI and Twilio webhook is signature-verified; OpenAI delivery IDs are replay-protected.
- A call cannot start without an unexpired prepared plan and explicit confirmation text.
- Destination policy blocks malformed E.164, emergency/N11/short codes, premium-rate prefixes, disallowed country codes, and the service's own Twilio number.
- The voice model chooses how to open from Poke's approved call context; the bridge does not impose identity, disclosure, or recipient-confirmation wording. It may not share or request passwords, auth codes, payment credentials, or government identifiers.
- The in-call voice model decides when the conversation is finished and invokes its private `end_call` function. The bridge asks it for one final spoken goodbye, waits for that closing response to complete, and then tears down OpenAI and Twilio. The public `end_phone_call` MCP tool remains a manual stop.
- Poke push is optional and non-canonical. Polling `get_call_result` is canonical.

Do not deploy or restart while a call is active. Recovery stops stranded billable media and finalizes missing results, but a process restart necessarily ends the live call.

Calls are retained as transcripts. The application does not force a verbal identity or AI-disclosure script; the owner remains responsible for jurisdiction-specific disclosure, recording, robocall, consent, and retention compliance. Apply transcript retention independently of extraction success or failure.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- A paid/voice-enabled Twilio account, an E.164 caller ID, and outbound SIP/Conference Participant AMD access
- An OpenAI project with Realtime SIP access, an API key, and a webhook signing secret
- An Exa API key for in-call public-web search
- A stable public HTTPS URL (tunnel for local work; always-on Render instance for deployment)

The locked implementation was developed against OpenAI Python `2.45.0`, Twilio Python `9.10.9`, FastMCP `3.4.4`, FastAPI `0.139.0`, and websockets `16.1`. `uv.lock` is authoritative.

## Local setup

```bash
test -e .env.local || cp .env.example .env.local
uv sync --all-groups --frozen
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Fill every required value in `.env.local`; the guarded copy command deliberately preserves a key previously saved by the OpenAI Platform picker. Generate `MCP_BEARER_TOKEN` and `DEBUG_API_TOKEN` with a password generator or `openssl rand -hex 32`. Never commit env files; `.gitignore` excludes them.

Live-call control requests use `OPENAI_CONNECT_TIMEOUT_SECONDS` and
`OPENAI_HTTP_TIMEOUT_SECONDS` (3 and 10 seconds by default) with no SDK retries; post-call
extraction uses `OPENAI_EXTRACTION_TIMEOUT_SECONDS` and retains its single application-level
retry. Twilio requests use the pooled, no-retry transport bounded by
`TWILIO_HTTP_TIMEOUT_SECONDS`. `SEMANTIC_VAD_EAGERNESS` defaults to `auto` (OpenAI's
medium-eagerness behavior); set it to `high` for quicker turn completion.
`OPENAI_KEEPALIVE_EXPIRY_SECONDS=60` keeps the control-plane TLS connection reusable between
sporadic calls; set it only to a bounded 5-300 second value.
The voice model can call `search_web` for current or uncertain facts. The application fixes Exa
Search to `type=auto`, 10 results, moderation, and token-efficient highlights, with a three-second
wall-clock deadline controlled by `EXA_SEARCH_TIMEOUT_SECONDS`.

Run validation:

```bash
uv run ruff format --check app tests scripts
uv run ruff check app tests scripts
uv run pytest -q
```

## HTTPS tunnel and webhooks

Start the server, then expose port 8000. Either command works:

```bash
ngrok http 8000
# or
cloudflared tunnel --url http://localhost:8000
```

Set `PUBLIC_BASE_URL` to the exact HTTPS origin printed by the tunnel, without a trailing slash, and restart the service. This exact value is security-critical because Twilio signs the full public callback URL.

In **OpenAI Platform → Project → Webhooks**, create an endpoint:

```text
https://YOUR_HOST/webhooks/openai
```

Subscribe it to `realtime.call.incoming`, copy its signing secret to `OPENAI_WEBHOOK_SECRET`, and restart. The OpenAI project in the SIP URI is `OPENAI_PROJECT_ID`.

No static Twilio webhook needs to be configured: each Conference Participant request supplies its signed status, conference, and AMD callback URL. Confirm that the Twilio account can call the OpenAI SIP URI and can use AMD on `/Participants`. The service supplies `DetectMessageEnd` and an AMD callback on every callee leg.

Configure Poke's MCP connection as:

```text
URL: https://YOUR_HOST/mcp/
Authorization: Bearer MCP_BEARER_TOKEN
X-Poke-User-Id: ALLOWED_POKE_USER_ID
Transport: Streamable HTTP
```

The server exposes exactly five tools: `prepare_phone_call`, `start_phone_call`, `get_call_result`, `end_phone_call`, and `get_phone_call`.

## Live SIP canary

Deploy or tunnel the service first. The full canary calls `OWNER_PHONE_E164`, prints a random spoken nonce, asks you to interrupt the assistant once, and checks accept status, echoed transcription configuration, semantic VAD, nonce transcription, function-tool use, interruption, and the terminal result:

```bash
uv run python scripts/run_sip_canary.py --mode full
```

Answer the call, say the printed nonce when asked, and speak over the assistant once. After the call, the script asks you to confirm that audible assistant speech stopped promptly; the server-side cancellation event must also be present. Then run the no-advisory-tool variant to prove the deterministic finalizer does not depend on `record_call_outcome`:

```bash
uv run python scripts/run_sip_canary.py --mode no-outcome-tool
```

Both commands exit nonzero if any gate fails. The debug evidence endpoint they use requires `DEBUG_API_TOKEN`.

No mini realtime model can be selected by configuration in v1. Do not relax the `realtime_model` literal or `MINI_MODELS_ENABLED=false` gate until that exact model passes both canaries, including SIP tool calling.

## Fly.io deployment

`fly.toml` defines the production deployment at `https://poke-call.fly.dev`: one always-on shared CPU machine with 512 MB RAM and a 1 GB persistent volume mounted at `/data`. SQLite uses `sqlite:////data/poke_call.db`. Validate and deploy it with:

```bash
flyctl config validate
flyctl deploy --ha=false --remote-only
curl -fsS https://poke-call.fly.dev/healthz
```

Set every required value from `.env.example` with `flyctl secrets set` or `flyctl secrets import`; never commit secret values. The OpenAI project webhook must point to `https://poke-call.fly.dev/webhooks/openai` and subscribe to `realtime.call.incoming`. After rotating its signing secret, activate staged values with:

```bash
flyctl secrets deploy -a poke-call
flyctl status -a poke-call
```

Keep exactly one Machine for this SQLite deployment. A second Machine would need its own local volume and would not share call state. `render.yaml` remains available as a paid Render alternative.

Automated deployments use the authenticated `/internal/deployment-lock` lease before replacing
the Machine. The lease is acquired atomically only when no call is active, blocks new calls while
the deployment starts, expires after 15 minutes if a deployment is canceled, and is cleared by a
successful application restart. Store `DEPLOY_GUARD_TOKEN` independently from the MCP and debug
tokens; it can only access this deployment lease.

`.github/workflows/fly-deploy.yml` runs the locked test suite and deploys every push to `main`,
including every merged pull request. It serializes production deployments, waits up to 10 minutes
for active calls to finish, preserves the single-Machine volume topology with `--ha=false`, and
checks `/healthz` before reporting success. GitHub Actions requires the repository secrets
`FLY_API_TOKEN` (an app-scoped Fly deploy token) and `DEPLOY_GUARD_TOKEN`.

## API/schema decisions verified on 2026-07-13

The implementation follows the current [OpenAI Realtime SIP guide](https://developers.openai.com/api/docs/guides/realtime-sip), [server-side controls guide](https://developers.openai.com/api/docs/guides/realtime-server-controls), [Realtime prompting guide](https://developers.openai.com/api/docs/guides/realtime-models-prompting), [webhook guide](https://developers.openai.com/api/docs/guides/webhooks), [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs), [Twilio Conference Participant reference](https://www.twilio.com/docs/voice/api/conference-participant-resource), [Twilio AMD guide](https://www.twilio.com/docs/voice/answering-machine-detection), and [Twilio request validation guide](https://www.twilio.com/docs/usage/security).

Live-schema deviations from the original contract are intentional:

- GA Realtime audio formats are objects such as `{"type":"audio/pcmu"}` when used, but SIP accept/session.update payloads must omit `audio.*.format`. OpenAI negotiates G.711 with the carrier; forcing a format has been observed to clobber PCMU into PCM and leave the callee with silence or static while WebSocket transcripts still advance.
- The current Conference Participants API has no `async_amd` parameter. AMD on Participants is asynchronous by design; the installed SDK uses `machine_detection="DetectMessageEnd"` plus `amd_status_callback` and `amd_status_callback_method`.
- OpenAI call accept is invoked through the installed typed SDK; hangup has no JSON body. REFER's live request field is `target_uri`, but v1 transfer deliberately does not use REFER.
- The installed transcription schema permits `INPUT_TRANSCRIPTION_DELAY` only for `gpt-realtime-whisper`, with `minimal|low|medium|high|xhigh` values.
- The OpenAI SIP agent participant is created with `early_media=false` and is explicitly unmuted before the model's opening turn, because Twilio mutes legs that join with `start_conference_on_enter=false` until the conference starts.

## Result semantics

Telephony state and extraction state remain separate. A successful phone call whose extractor fails stays `call_status=completed`, gets `finalization_status=failed`, `outcome=unknown`, retains its raw transcript, and tells the owner to review it. The service saves a telephony-only result and transcript transactionally before making the external Responses API extraction request. Only transient connection, timeout, or rate-limit failures receive one retry.

Optional Poke push remains disabled by default. A push failure is logged and never changes call state or polling availability.
