# Poke Phone-Call Bridge

Single-user FastAPI + FastMCP service that lets Poke prepare, confirm, start, monitor, and end
outbound phone calls for Irvin. Twilio originates an authenticated xAI SIP agent leg into a
private conference, the service attaches and prewarms `grok-voice-think-fast-1.0` over a sideband
WebSocket, and only then dials the callee. SQLite stores plans, state, ordered transcripts, and
deterministic final results.

## Safety and operating model

- The MCP endpoint requires its bearer token and validates the configured Poke user ID when sent.
- Every xAI and Twilio webhook is signature-verified; xAI delivery IDs are replay-protected.
- Calls require an unexpired plan and explicit confirmation. Destination policy blocks malformed
  E.164, emergency/N11/short codes, premium-rate prefixes, disallowed countries, and self-calls.
- The voice model chooses the opening from Poke's approved context. It may not share or request
  passwords, auth codes, payment credentials, or government identifiers.
- The model invokes its private `end_call` function when finished. The bridge requests one final
  spoken goodbye and then tears down xAI and Twilio. `end_phone_call` remains a manual stop.
- Poke push is optional; polling `get_call_result` is canonical.

Do not deploy or restart while a call is active. Calls are retained as transcripts. The owner
remains responsible for jurisdiction-specific disclosure, recording, robocall, consent, and
retention compliance.

## Requirements and local setup

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- A paid/voice-enabled Twilio account with an E.164 caller ID and Conference Participant AMD
- An xAI account with Voice Agent API access and an API key
- A stable public HTTPS URL

```bash
test -e .env.local || cp .env.example .env.local
uv sync --all-groups --frozen
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Fill every required value in `.env.local`. Generate the MCP, debug, deployment, and SIP digest
secrets with a password generator or `openssl rand -hex 32`. Never commit env files.

Live-call control uses `XAI_CONNECT_TIMEOUT_SECONDS` and `XAI_HTTP_TIMEOUT_SECONDS` with no SDK
retries. Post-call `grok-4.3` extraction uses low reasoning, strict JSON Schema,
`XAI_EXTRACTION_TIMEOUT_SECONDS`, and one application-level retry for transient failures. Every
returned evidence ID is still checked against the persisted transcript. Twilio uses its own
bounded, pooled, no-retry transport.

Run validation:

```bash
uv run ruff format --check app tests scripts
uv run ruff check app tests scripts
uv run pytest -q
```

## xAI SIP and webhooks

Register `TWILIO_CALLER_ID` as an xAI BYO-trunk phone number with digest SIP credentials and this
webhook:

```text
https://YOUR_HOST/webhooks/xai
```

Store the returned signing secret in `XAI_WEBHOOK_SECRET`, the registered number in
`XAI_SIP_PHONE_NUMBER`, and the digest credentials in `XAI_SIP_AUTH_USERNAME` and
`XAI_SIP_AUTH_PASSWORD`. The service dials `sip:+NUMBER@sip.voice.x.ai;transport=tls` and supplies
those credentials through Twilio's Conference Participant API. No static Twilio webhook is
required; each participant request supplies signed status, conference, and AMD callback URLs.

Configure Poke's MCP connection as:

```text
URL: https://YOUR_HOST/mcp/
Authorization: Bearer MCP_BEARER_TOKEN
X-Poke-User-Id: ALLOWED_POKE_USER_ID
Transport: Streamable HTTP
```

The server exposes `prepare_phone_call`, `start_phone_call`, `get_call_result`, `end_phone_call`,
and `get_phone_call`.

## Live SIP canary

The full canary places a real call to `OWNER_PHONE_E164`. It checks the xAI sideband, echoed
transcription and server VAD configuration, nonce transcription, function tools, interruption,
and terminal result.

```bash
uv run python scripts/run_sip_canary.py --mode full
uv run python scripts/run_sip_canary.py --mode no-outcome-tool
```

Both commands exit nonzero if a gate fails. They require a human to answer and confirm that
audible speech stopped promptly after interruption. The release-gated voice model is
`grok-voice-think-fast-1.0`; the post-call extractor is `grok-4.3` with low reasoning.

## Fly.io deployment

`fly.toml` defines `https://poke-call.fly.dev` with one always-on Machine and one persistent SQLite
volume. Keep exactly one Machine because the volume-local database is not shared.

```bash
flyctl config validate
flyctl deploy --ha=false --remote-only
curl -fsS https://poke-call.fly.dev/healthz
```

Set every required value from `.env.example` as Fly secrets. The xAI phone-number webhook must
point to `https://poke-call.fly.dev/webhooks/xai`. Staged secrets can be activated with:

```bash
flyctl secrets deploy -a poke-call
flyctl status -a poke-call
```

Automated deployments acquire `/internal/deployment-lock`, wait for active calls to finish, keep
the single-Machine topology, and verify `/healthz`. GitHub Actions requires `FLY_API_TOKEN` and
`DEPLOY_GUARD_TOKEN`.

## API decisions verified on 2026-07-14

The implementation follows the current [xAI Voice Agent guide](https://docs.x.ai/developers/model-capabilities/audio/voice-agent),
[xAI SIP guide](https://docs.x.ai/developers/model-capabilities/audio/voice-agent/sip),
[xAI Structured Outputs guide](https://docs.x.ai/developers/model-capabilities/text/structured-outputs),
[Twilio Conference Participant reference](https://www.twilio.com/docs/voice/api/conference-participant-resource),
[Twilio AMD guide](https://www.twilio.com/docs/voice/answering-machine-detection), and
[Twilio request validation guide](https://www.twilio.com/docs/usage/security).

- Sideband joins `wss://api.x.ai/v1/realtime?call_id=...`; hangup uses
  `POST /v1/realtime/calls/{call_id}/hangup`.
- xAI SIP negotiates carrier audio. The sideband configures `grok-transcribe`, voice `eve`, and
  server VAD without forcing an audio format.
- Twilio Conference Participant AMD is asynchronous and uses `DetectMessageEnd` plus its callback.
- The xAI SIP leg joins with `early_media=false` and is explicitly unmuted before the opening turn.

## Result semantics

Telephony and extraction state remain separate. A successful call whose extractor fails remains
`call_status=completed`, gets `finalization_status=failed`, retains its raw transcript, and asks the
owner to review it. The service saves a telephony-only result transactionally before the external
Responses request. Optional Poke push failures never change call state or polling availability.
