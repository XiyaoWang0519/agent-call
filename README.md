<h1 align="center">📞 Phone-Call Bridge for Poke</h1>

<p align="center">
  <em>Text Poke. Poke calls a human. An AI does the talking. You get the transcript.</em>
</p>

<p align="center">
  <a href="#-quick-start">Quick start</a> ·
  <a href="#-how-a-call-happens">How it works</a> ·
  <a href="#-the-seven-tools">MCP tools</a> ·
  <a href="#-safety-rails">Safety</a> ·
  <a href="#-deploying">Deploy</a>
</p>

---

A single-user [FastAPI](https://fastapi.tiangolo.com) + FastMCP service that lets [Poke](https://poke.com) prepare, confirm, start, monitor, and end outbound phone calls for its owner. [Twilio](https://www.twilio.com) dials an [OpenAI](https://openai.com) SIP voice agent into a private conference, the service prewarms `gpt-realtime-2.1` over a sideband WebSocket, and only then rings the callee. SQLite keeps the receipts: plans, state, ordered transcripts, and deterministic final results.

> [!NOTE]
> This is an independent, unofficial integration. It is not endorsed by or
> affiliated with Poke, OpenAI, Twilio, Exa, Fly.io, FastAPI, or Astral.

## ✨ How a call happens

```mermaid
sequenceDiagram
    participant P as 💬 Poke
    participant B as 🌉 Bridge
    participant O as 🤖 OpenAI SIP agent
    participant T as ☎️ Twilio
    participant C as 🧑 Callee

    P->>B: prepare_phone_call (plan + policy checks)
    P->>B: start_phone_call (explicit confirmation)
    B->>T: create conference, dial OpenAI SIP leg
    O-->>B: realtime.call.incoming webhook
    B->>O: accept + prewarm over sideband WebSocket
    B->>T: now dial the callee (with AMD)
    T->>C: 🔔 ring ring
    O<<->>C: conversation (transcribed live)
    O->>B: end_call → one spoken goodbye → teardown
    P->>B: wait_for_call_event → get_call_result
```

The callee's phone never rings until the AI is already on the line, warmed up, and ready to speak.

## 🧰 Built with

| Piece | Job |
| --- | --- |
| [Poke](https://poke.com) | MCP client, call intent, optional push |
| [OpenAI Realtime SIP](https://developers.openai.com/api/docs/guides/realtime-sip) | Voice agent, accept, sideband control |
| [Twilio](https://www.twilio.com) | Conference, callee dial, answering-machine detection |
| [Exa](https://exa.ai) | In-call public-web search |
| [FastAPI](https://fastapi.tiangolo.com) + FastMCP | HTTP surface and MCP tools |
| [Fly.io](https://fly.io) | Production host, volume-backed SQLite |
| [Astral](https://astral.sh) uv / Ruff | Package lock and lint |

## 🚀 Quick start

You'll need Python 3.12+, [uv](https://docs.astral.sh/uv/), a voice-enabled Twilio account (E.164 caller ID, outbound SIP + Conference Participant AMD), an OpenAI project with Realtime SIP access and a webhook signing secret, an Exa API key, and a stable public HTTPS URL.

```bash
test -e .env.local || cp .env.example .env.local   # guarded: keeps a saved key
uv sync --all-groups --frozen
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Fill every required value in `.env.local`. Generate `MCP_BEARER_TOKEN` and `DEBUG_API_TOKEN` with `openssl rand -hex 32`. Leave `ALLOWED_COUNTRY_CODES` unset in dotenv files (the app defaults to `["+1"]`). Never commit env files — `.gitignore` already excludes them.

Then check your work:

```bash
uv run ruff format --check app tests scripts
uv run ruff check app tests scripts
uv run pytest -q
```

### Expose it to the world

Webhooks need a public HTTPS origin. Start the server, then tunnel port 8000:

```bash
ngrok http 8000            # or: cloudflared tunnel --url http://localhost:8000
```

Set `PUBLIC_BASE_URL` to the exact HTTPS origin the tunnel prints — no trailing slash — and restart. This value is security-critical: Twilio signs the full public callback URL.

In **OpenAI Platform → Project → Webhooks**, create `https://YOUR_HOST/webhooks/openai`, subscribe it to `realtime.call.incoming`, and copy the signing secret to `OPENAI_WEBHOOK_SECRET`. The OpenAI project in the SIP URI is `OPENAI_PROJECT_ID`.

No static Twilio webhook is needed — every Conference Participant request carries its own signed status, conference, and AMD callback URLs. Just confirm the Twilio account can call the OpenAI SIP URI and can use AMD on `/Participants`.

### Point Poke at it

```text
URL:            https://YOUR_HOST/mcp/
Authorization:  Bearer MCP_BEARER_TOKEN
X-Poke-User-Id: ALLOWED_POKE_USER_ID
Transport:      Streamable HTTP
```

## 🛠️ The seven tools

The server exposes exactly seven MCP tools — no more, no less:

| Tool | What it does |
| --- | --- |
| `prepare_phone_call` | Validate destination policy, persist a plan |
| `start_phone_call` | Explicit confirmation → dial |
| `wait_for_call_event` | Canonical live-call monitoring loop |
| `get_phone_call` | Snapshot of call state |
| `answer_call_question` | Feed an answer to the live agent |
| `end_phone_call` | Manual stop button |
| `get_call_result` | Deterministic final result + transcript |

During a live call, `wait_for_call_event` is the canonical monitoring loop; once it reports a terminal state, call `get_call_result`. Poke push is optional and non-canonical — a push failure is logged and never changes call state.

## 🛡️ Safety rails

- 🔑 The MCP endpoint always requires its bearer token, and validates the configured Poke user ID whenever Poke supplies the header (Poke's refresh follow-up currently omits it).
- ✍️ Every OpenAI and Twilio webhook is signature-verified; OpenAI delivery IDs are replay-protected.
- 🙋 A call cannot start without an unexpired prepared plan **and** explicit confirmation text.
- 🚫 Destination policy blocks malformed E.164, emergency/N11/short codes, premium-rate prefixes, disallowed country codes, and the service's own Twilio number.
- 🤐 The voice model may not share or request passwords, auth codes, payment credentials, or government identifiers. It chooses how to open from Poke's approved call context; the bridge does not impose identity, disclosure, or recipient-confirmation wording.
- ☎️ The agent can press automated phone-menu (IVR) keys via a signed announce webhook, but is instructed never to enter payment, authentication, or identity digits that way.
- 👋 The in-call model decides when the conversation is done and invokes its private `end_call` function; the bridge asks for one final spoken goodbye, waits for it, then tears down OpenAI and Twilio.

> [!WARNING]
> Do not deploy or restart while a call is active. Recovery stops stranded billable media and finalizes missing results, but a process restart necessarily ends the live call.

Calls are retained as transcripts. The owner remains responsible for jurisdiction-specific disclosure, recording, robocall, consent, and retention compliance. Apply transcript retention independently of extraction success or failure.

## 🐦 Live SIP canary

Before trusting a deployment, make it prove itself with a real call to `OWNER_PHONE_E164`:

```bash
uv run python scripts/run_sip_canary.py --mode full
```

Answer the phone, say the printed nonce when asked, and talk over the assistant once. The script gates on accept status, echoed transcription configuration, semantic VAD, nonce transcription, function-tool use, interruption handling, and the terminal result. Then run the second variant to prove the deterministic finalizer does not depend on `record_call_outcome`:

```bash
uv run python scripts/run_sip_canary.py --mode no-outcome-tool
```

Both exit nonzero if any gate fails. The debug evidence endpoint they use requires `DEBUG_API_TOKEN`.

> [!NOTE]
> No mini realtime model can be selected by configuration in v1. Do not relax the `realtime_model` literal or the `MINI_MODELS_ENABLED=false` gate until that exact model passes both canaries, including SIP tool calling.

## 🎈 Deploying

### Self-hosting on Fly.io

The checked-in `fly.toml` documents the maintainer's deployment and must be
customized for a new installation. Before deploying a fork:

1. Change `app` and `primary_region` in `fly.toml` to your own Fly app and
   preferred region.
2. Set `PUBLIC_BASE_URL` to your app's HTTPS origin.
3. Create a 1 GB volume named `poke_call_data` in the same region as the
   Machine; the app stores SQLite state at `/data/poke_call.db`.
4. Configure every required secret listed in `.env.example`.
5. Point the OpenAI project webhook at
   `https://YOUR_HOST/webhooks/openai` and subscribe it to
   `realtime.call.incoming`.
6. If you retain `.github/workflows/fly-deploy.yml`, replace the hard-coded app
   name and URLs, configure a protected `production` environment, and add the
   `FLY_API_TOKEN` and `DEPLOY_GUARD_TOKEN` repository secrets.

Keep exactly one Machine. SQLite state is volume-local; multiple Machines do
not share call state. `render.yaml` is available as a paid Render alternative
and prompts for deployment-specific secrets.

### Maintainer production

The repository's `fly.toml` currently defines the maintainer deployment at
`https://poke-call.fly.dev`: one always-on shared-CPU machine, 512 MB RAM, a
1 GB volume at `/data`, and SQLite at `sqlite:////data/poke_call.db`.

```bash
flyctl config validate
flyctl deploy --ha=false --remote-only
curl -fsS https://poke-call.fly.dev/healthz
```

Set every required value from `.env.example` with `flyctl secrets set` or `flyctl secrets import`. Point the OpenAI project webhook at `https://poke-call.fly.dev/webhooks/openai` (subscribed to `realtime.call.incoming`). After rotating the signing secret:

```bash
flyctl secrets deploy -a poke-call
flyctl status -a poke-call
```

### Rollback

If a bad deploy reaches production, roll back first (do not redeploy while diagnosing):

```bash
# Confirm no active calls (POST acquires the lease; HTTP 409 means wait)
curl --fail-with-body -sS --request POST \
  -H "Authorization: Bearer $DEPLOY_GUARD_TOKEN" \
  https://poke-call.fly.dev/internal/deployment-lock

# List image references and redeploy the previous healthy image
flyctl releases --image -a poke-call
flyctl deploy --image <previous-image-reference> -a poke-call --ha=false --remote-only

# Verify
curl -fsS https://poke-call.fly.dev/healthz
```

Redeploying a prior image restores only the application image without a full rebuild; it does not roll back the current Fly configuration, environment variables, or secrets. Ship a new build only after calls are idle and local checks pass (`ruff`, `mypy`, `pytest`).

**CI/CD:** `.github/workflows/fly-deploy.yml` runs the locked test suite and deploys every push to `main`. It serializes production deployments, waits up to 10 minutes for active calls to finish via the authenticated `/internal/deployment-lock` lease, keeps `--ha=false`, and checks `/healthz` before reporting success. It needs the repository secrets `FLY_API_TOKEN` (app-scoped deploy token) and `DEPLOY_GUARD_TOKEN` — store the latter independently from the MCP and debug tokens; it can only touch the deployment lease. The lease is acquired atomically only when no call is active, blocks new calls while a deployment starts, expires after 15 minutes if canceled, and is cleared by a successful restart.

## 🔬 The fine print

<details>
<summary><strong>Tuning knobs (timeouts, VAD, search)</strong></summary>

- Live-call control requests use `OPENAI_CONNECT_TIMEOUT_SECONDS` and `OPENAI_HTTP_TIMEOUT_SECONDS` (3 and 10 seconds by default) with no SDK retries; post-call extraction uses `OPENAI_EXTRACTION_TIMEOUT_SECONDS` and keeps its single application-level retry.
- Twilio requests use the pooled, no-retry transport bounded by `TWILIO_HTTP_TIMEOUT_SECONDS`.
- `SEMANTIC_VAD_EAGERNESS` defaults to `auto` (OpenAI's medium-eagerness behavior); set `high` for quicker turn completion.
- `OPENAI_KEEPALIVE_EXPIRY_SECONDS=60` keeps the control-plane TLS connection reusable between sporadic calls; only set it to a bounded 5–300 second value.
- The voice model can call `search_web` for current or uncertain facts. The application fixes Exa Search to `type=auto`, 10 results, moderation, and token-efficient highlights, with a three-second wall-clock deadline controlled by `EXA_SEARCH_TIMEOUT_SECONDS`.
- Runtime and development dependencies are locked in `uv.lock`; use
  `uv sync --all-groups --frozen` rather than copying version numbers from the
  documentation.

</details>

<details>
<summary><strong>API/schema decisions (verified 2026-07-13)</strong></summary>

The implementation follows the current [OpenAI Realtime SIP guide](https://developers.openai.com/api/docs/guides/realtime-sip), [server-side controls guide](https://developers.openai.com/api/docs/guides/realtime-server-controls), [Realtime prompting guide](https://developers.openai.com/api/docs/guides/realtime-models-prompting), [webhook guide](https://developers.openai.com/api/docs/guides/webhooks), [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs), [Twilio Conference Participant reference](https://www.twilio.com/docs/voice/api/conference-participant-resource), [Twilio AMD guide](https://www.twilio.com/docs/voice/answering-machine-detection), and [Twilio request validation guide](https://www.twilio.com/docs/usage/security).

Live-schema deviations from the original contract are intentional:

- GA Realtime audio formats are objects such as `{"type":"audio/pcmu"}` when used, but SIP accept/session.update payloads must omit `audio.*.format`. OpenAI negotiates G.711 with the carrier; forcing a format has been observed to clobber PCMU into PCM and leave the callee with silence or static while WebSocket transcripts still advance.
- The current Conference Participants API has no `async_amd` parameter. AMD on Participants is asynchronous by design; the installed SDK uses `machine_detection="DetectMessageEnd"` plus `amd_status_callback` and `amd_status_callback_method`.
- OpenAI call accept is invoked through the installed typed SDK; hangup has no JSON body. REFER's live request field is `target_uri`, but v1 transfer deliberately does not use REFER.
- The installed transcription schema permits `INPUT_TRANSCRIPTION_DELAY` only for `gpt-realtime-whisper`, with `minimal|low|medium|high|xhigh` values.
- The OpenAI SIP agent participant is created with `early_media=false` and is explicitly unmuted before the model's opening turn, because Twilio mutes legs that join with `start_conference_on_enter=false` until the conference starts.

</details>

<details>
<summary><strong>Result semantics</strong></summary>

Telephony state and extraction state remain separate. A successful phone call whose extractor fails stays `call_status=completed`, gets `finalization_status=failed`, `outcome=unknown`, retains its raw transcript, and tells the owner to review it. The service saves a telephony-only result and transcript transactionally before making the external Responses API extraction request. Only transient connection, timeout, or rate-limit failures receive one retry.

</details>

## Contributing and support

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md) before opening a pull request. Use
[SUPPORT.md](SUPPORT.md) for setup questions and [SECURITY.md](SECURITY.md) for
private vulnerability reporting.

## License

Original project code and documentation are available under the
[MIT License](LICENSE). Third-party services, names, and marks are governed by
their respective owners' terms and are not included in that license grant.

---

Poke, OpenAI, Twilio, Exa, Fly.io, FastAPI, Astral, and related names and marks
belong to their respective owners. Their use here identifies interoperability
only and does not imply sponsorship or endorsement. Third-party names and marks
are not licensed under this project's MIT License.
