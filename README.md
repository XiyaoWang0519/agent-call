# Agent Call

[![CI](https://github.com/XiyaoWang0519/agent-call/actions/workflows/ci.yml/badge.svg)](https://github.com/XiyaoWang0519/agent-call/actions/workflows/ci.yml)
[![Secret scan](https://github.com/XiyaoWang0519/agent-call/actions/workflows/secret-scan.yml/badge.svg)](https://github.com/XiyaoWang0519/agent-call/actions/workflows/secret-scan.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Status: early-stage v0.x](https://img.shields.io/badge/status-early--stage%20v0.x-yellow.svg)](#project-status)

Agent Call is a **self-hosted, single-owner MCP bridge** that connects an AI agent to outbound phone calls through [OpenAI Realtime SIP](https://developers.openai.com/api/docs/guides/realtime-sip) and [Twilio](https://www.twilio.com).

An MCP client such as OpenClaw, Hermes Agent, or a private Grok Bot custom MCP connector prepares, confirms, starts, monitors, and ends calls. Twilio places an OpenAI SIP voice agent into a private conference, the service prewarms `gpt-realtime-2.1` over a sideband WebSocket, and only then rings the callee. SQLite stores plans, call state, ordered transcripts, and deterministic final results.

## Two products

| Edition | Status | What you run |
| --- | --- | --- |
| **Agent Call Self-Hosted** | This public MIT repository | Your process, your SQLite file, your Twilio and OpenAI accounts. Complete and useful on its own. |
| **Agent Call Managed** | Forthcoming private service | A separately operated product. It is **not** configured from this README and is not mixed into the self-host setup. |

The public edition does not phone home, does not include a remote license check, and does not require a managed account.

## Project status

This is an **early-stage / v0.x self-hosted reference implementation**. Package metadata currently reports version `0.1.0`, but there is **no tagged GitHub release** yet. The project is intended for a single operator running their own instance. It is not a hosted product and not a multi-tenant platform.

## Who it is for

- Operators who want **their own** agent to place outbound calls on their behalf
- Contributors evaluating the architecture, safety rails, and test suite without spending money
- Maintainers of a single-owner deployment who can accept SQLite and one-instance constraints

## What it does

- Validate a call plan against destination policy and persist it without dialing
- Require explicit confirmation before any callee is rung
- Prewarm the OpenAI SIP agent, then dial the callee into a Twilio conference with answering-machine detection
- Expose seven MCP tools for prepare / start / monitor / snapshot / mid-call answers / end / final result
- Keep `wait_for_call_event` polling as the canonical live-call loop; optional agent webhook push is best-effort
- Persist transcripts and a deterministic final result even when post-call extraction fails

## Non-goals and limitations

- **Single-owner architecture.** One configured owner phone, one allowed agent user id, one MCP bearer. This is not a general multi-tenant calling platform.
- **SQLite and one instance.** Call state is volume-local. A second Fly Machine (or a second process sharing the same public URL) will not share state correctly.
- **Real OpenAI and Twilio costs.** Live calls bill Realtime SIP audio plus Twilio voice. Pytest does not place calls; the SIP canary and opt-in live phone harness do.
- **You own compliance.** Consent, recording, robocall, disclosure, and retention rules are jurisdiction-specific. The owner remains responsible. Transcripts are retained; apply retention independently of extraction success or failure.
- **Not a drop-in hosted service.** Forks must provision their own Twilio, OpenAI, Exa, and compute accounts. The committed `fly.toml` is a template (`YOUR_FLY_APP_NAME`); it does not deploy to a maintainer app.

## Safety properties

- The legacy MCP endpoint (`/mcp/`) requires its bearer token **and** a matching `X-Agent-User-Id` on every request. Optional Grok OAuth on `/grok/mcp/` is disabled by default and does not replace that gate.
- Every OpenAI and Twilio webhook is signature-verified; OpenAI delivery IDs are replay-protected.
- A call cannot start without an unexpired prepared plan **and** explicit confirmation text.
- Destination policy blocks malformed E.164, emergency/N11/short codes, premium-rate prefixes, disallowed country codes, and the service's own Twilio number.
- The voice model may not share or request passwords, auth codes, payment credentials, or government identifiers. It chooses how to open from the approved call context; the bridge does not impose identity, disclosure, or recipient-confirmation wording.
- The agent can press automated phone-menu (IVR) keys via a signed announce webhook, but is instructed never to enter payment, authentication, or identity digits that way.
- The in-call model decides when the conversation is done and invokes its private `end_call` function; the bridge asks for one final spoken goodbye, waits for it, then tears down OpenAI and Twilio.
- Evaluation/dummy profile: `prepare_phone_call` still persists a plan; `start_phone_call` returns `live_calls_disabled` before any OpenAI or Twilio client request.

> [!WARNING]
> Do not deploy or restart while a call is active. Recovery stops stranded billable media and finalizes missing results, but a process restart necessarily ends the live call.

## Golden path: evaluate without credentials

Lint, type-check, test, and boot a dummy instance with **no OpenAI key, Twilio account, phone number, or paid API usage**. External services are mocked in `tests/conftest.py`; tests use a temporary SQLite database. **No call is placed.**

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/) (CI uses uv `0.9.27` and Python 3.13).

```bash
uv sync --all-groups --frozen
uv run ruff format --check app tests scripts
uv run ruff check app tests scripts
uv run mypy app
uv run pytest -q --cov=app
uv run agent-call doctor --dummy
```

### Dummy boot (source)

The evaluation listener binds loopback unless you pass `--unsafe-bind`. Core credentials in `.env.local` or the process environment plus bare `agent-call serve` do not silently go live; pass `--profile evaluation` (dummy) or `--profile live` (this will place billable calls).

```bash
uv run agent-call serve --profile evaluation --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
curl -fsS http://127.0.0.1:8000/healthz
uv run agent-call doctor --prepare-only
uv run agent-call smoke-prepare
```

`smoke-prepare` initializes MCP, lists exactly seven tools, prepares a plan, and never invokes `start_phone_call`. Starting a confirmed plan in this profile returns `live_calls_disabled`.

`uv run python -m app` is equivalent to `uv run agent-call`.

### Dummy boot (Compose)

```bash
docker compose up --build
```

Compose publishes `127.0.0.1:8000` only, uses a named volume for SQLite, runs as the image's non-root user after the entrypoint, and sets `AGENT_CALL_PROFILE=evaluation`. No provider credentials are passed into the image.

Then `uv run agent-call smoke-prepare` against `http://127.0.0.1:8000`.

If you already have a running dummy server, `scripts/live_smoke.sh` is a broader Grok-compatible handshake (including optional OAuth) that still never calls `start_phone_call`.

## How a call happens

```mermaid
sequenceDiagram
    participant A as Agent
    participant B as Bridge
    participant O as OpenAI SIP agent
    participant T as Twilio
    participant C as Callee

    A->>B: prepare_phone_call (plan + policy checks)
    A->>B: start_phone_call (explicit confirmation)
    B->>T: create conference, dial OpenAI SIP leg
    O-->>B: realtime.call.incoming webhook
    B->>O: accept + prewarm over sideband WebSocket
    B->>T: now dial the callee (with AMD)
    T->>C: ring
    O<<->>C: conversation (transcribed live)
    O->>B: end_call → one spoken goodbye → teardown
    A->>B: wait_for_call_event → get_call_result
```

The callee's phone never rings until the AI is already on the line, warmed up, and ready to speak.

See [docs/architecture.md](docs/architecture.md) for components, state, persistence, and webhook verification.

## MCP tools

The server exposes exactly seven MCP tools:

| Tool | What it does |
| --- | --- |
| `prepare_phone_call` | Validate destination policy, persist a plan |
| `start_phone_call` | Explicit confirmation → dial |
| `wait_for_call_event` | Canonical live-call monitoring loop |
| `get_phone_call` | Snapshot of call state |
| `answer_call_question` | Feed an answer to the live agent |
| `end_phone_call` | Manual stop button |
| `get_call_result` | Deterministic final result + transcript |

During a live call, `wait_for_call_event` is the canonical monitoring loop; once it reports a terminal state, call `get_call_result`. Agent push is optional and non-canonical — a push failure is logged and never changes call state.

## Golden path: local live (prepare-only first)

Full environment, tunnel, webhook, and agent-client instructions: [docs/self-hosting.md](docs/self-hosting.md).

```bash
test -e .env.local || cp .env.example .env.local
# Fill live values, boot the live-profile server, then expose a public HTTPS origin.
uv run agent-call doctor --live-ready
```

Run `doctor --live-ready` only after the live server and HTTPS tunnel or deployment are available. It reports missing or invalid prerequisites **without printing secret values or full phone numbers**, then probes SQLite writability and public-origin health. Exa and the webhook signing secret stay unverified. Then run a prepare-only MCP smoke against the live-profile process. Do not call `start_phone_call` until you intend to ring a real phone.

> [!CAUTION]
> Any command that reaches a configured Twilio/OpenAI environment can place a **real billable phone call**. Do not run the live SIP canary, point webhooks at a shared production host, or deploy with real secrets until you intend to spend money and ring a real phone.

## Golden path: supported web host

Create **your** Fly app from the template `fly.toml` (`app = 'YOUR_FLY_APP_NAME'`). Replace that placeholder, set secrets, and deploy with `--ha=false`. Details: [docs/self-hosting.md](docs/self-hosting.md#deploying-your-own-fly-app).

Forks must not copy a maintainer overlay. The user template does not name a pre-existing production app.

## Live SIP canary

For unattended real calls to dedicated automated callee and owner numbers, use the
[automated live-phone harness](docs/live-phone-runbook.md). It records received audio,
tests conversation and tools, and reports independent audio and provider-cleanup evidence.
The manual canary below still requires a person with a phone.

> [!CAUTION]
> The following commands place a **real billable call** to `OWNER_PHONE_E164`. They need real credentials, a public HTTPS URL, and a human with a phone. Do not run them in CI, on a fork against someone else's app, or casually while browsing the repo.

Before trusting a deployment, make it prove itself with a real call:

```bash
uv run python scripts/run_sip_canary.py --mode full
```

Answer the phone, say the printed nonce when asked, and talk over the assistant once. Then run the second variant to prove the deterministic finalizer does not depend on `record_call_outcome`:

```bash
uv run python scripts/run_sip_canary.py --mode no-outcome-tool
```

Both exit nonzero if any gate fails. The debug evidence endpoint they use requires `DEBUG_API_TOKEN`.

> [!NOTE]
> No mini realtime model can be selected by configuration in v1. Do not relax the `realtime_model` literal or the `MINI_MODELS_ENABLED=false` gate until that exact model passes both canaries, including SIP tool calling.

## Documentation

For future agents and local sessions, start with the [live phone testing handoff](docs/live-phone-handoff.md).

| Doc | Contents |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | Components, state machine, SQLite, webhooks, confirmation, finalization |
| [docs/self-hosting.md](docs/self-hosting.md) | Local and web golden paths, tunnels, Fly/Render, rollback, tuning |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Dummy boot, doctor, MCP auth, `live_calls_disabled` |
| [docs/grok-bot/README.md](docs/grok-bot/README.md) | Private Grok Bot custom MCP connector, optional OAuth, copy-paste skill, Dev Phone |
| [docs/implementation/](docs/implementation/) | Builder operating record (Phase 1; ADR-001) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Credential-free setup, lint/test commands, PR expectations |
| [SECURITY.md](SECURITY.md) | Vulnerability reporting |
| [SUPPORT.md](SUPPORT.md) | Questions and bug reports |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Community standard |
| [MAINTAINERS.md](MAINTAINERS.md) | Maintainer |
| [CHANGELOG.md](CHANGELOG.md) | Unreleased changes |
| [LICENSE](LICENSE) | MIT |

Maintainer-only production overlay notes live in [docs/maintainer-deploy.md](docs/maintainer-deploy.md). Public users and forks can ignore that file.

## Built with

| Piece | Job |
| --- | --- |
| OpenClaw / Hermes Agent / Grok Bot | MCP client, call intent, optional OpenClaw webhook wake |
| [OpenAI Realtime SIP](https://developers.openai.com/api/docs/guides/realtime-sip) | Voice agent, accept, sideband control |
| [Twilio](https://www.twilio.com) | Conference, callee dial, answering-machine detection |
| [Exa](https://exa.ai) | In-call public-web search |
| [FastAPI](https://fastapi.tiangolo.com) + FastMCP | HTTP surface and MCP tools |
| [Fly.io](https://fly.io) | Optional one-instance web host, volume-backed SQLite |
| [Astral](https://astral.sh) uv / Ruff | Package lock and lint |
