# Private Grok Bot integration

This is a **private, single-owner** setup so Grok Bot can use the existing Agent
Call MCP server. It is not a marketplace plugin, not a managed service, and not
a Grok Voice change. Voice remains OpenAI Realtime SIP plus Twilio.

Official sources used (retrieved 2026-08-21):

- [Grok Bot overview](https://docs.x.ai/grok-bot/overview) — Bots are persistent
  AI teammates on a cloud computer. They can use **connectors/MCP where
  available**. This is **not** Grok Build.
- [Connectors](https://docs.x.ai/grok/connectors) — custom MCP: go to
  [grok.com/connectors](https://grok.com/connectors) → **New Connector** →
  **Custom** → enter the MCP server URL and complete required authentication.
- [Custom MCP tunneling](https://docs.x.ai/grok/connectors/custom-mcp-tunneling)
  — Grok's servers must reach a public HTTPS URL. Localhost is rejected. Auth
  still applies after the tunnel URL is set; official text names **OAuth or API
  keys**.
- [Use the computer and apps](https://docs.x.ai/grok-bot/computer-and-apps) —
  in the Grok Bot app, connectors appear as **Plugins**. Path:
  **Settings → Plugins** → **Add**, then type `@` to attach a connector.
- [Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)
  — a skill is a reusable instruction set saved in the Grok Bot app (ask the
  Bot to save it, or **Teach a task**). Enable private skills under
  **Settings → Plugins → Yours**. Type `/` to reference a saved skill.
- [Settings](https://docs.x.ai/grok-bot/settings-and-notifications) —
  **Marketplace** discovers connectors and packaged skills; **Yours** lists
  installed plugins and private skills.

## Grok Bot is not Grok Build

| Surface | What it is | Skills / MCP |
| --- | --- | --- |
| **Grok Bot** | Desktop/iOS teammate on a cloud VM | **Settings → Plugins**; save a skill in chat; `@` connectors, `/` skills |
| **Grok** chat | grok.com conversations | [grok.com/connectors](https://grok.com/connectors) custom MCP |
| **Grok Build** | Terminal coding agent | `.grok/skills`, `grok mcp add --header`, plugin marketplaces |

Do **not** install this repository's files into `.grok/skills` or
`~/.grok/skills` and expect Grok Bot to load them. That layout is Grok Build.
Official Grok Bot docs do **not** document a repository-importable skill file
or a third-party plugin package format. The private phone-call skill in this
directory is a **copy-and-paste definition**.

## Authentication (do not weaken)

OpenClaw, Hermes, and other dual-header clients keep using `/mcp/`:

```text
Authorization: Bearer <MCP_BEARER_TOKEN>
X-Agent-User-Id: <ALLOWED_AGENT_USER_ID>
```

That gate is unchanged. Grok Bot's Custom Connector UI accepts a server name
and URL and does not provide a secure way to inject those two headers. For
Grok, enable the **optional, self-hosted, single-owner** OAuth 2.1 endpoint
instead. This is not a managed SaaS identity system, not multi-tenant, and not
an external IdP (no GitHub, Google, Auth0, WorkOS, Clerk, Supabase, Cursor, or
xAI login).

**Implemented locally; pending live Grok OAuth verification.**

Leave `AGENT_PUSH_ENABLED=false` for Grok Bot. `wait_for_call_event` is the
canonical live-call loop.

## 1. Expose the Grok MCP URL (Streamable HTTP)

With OAuth enabled, the Grok connector URL is `https://YOUR_HOST/grok/mcp/`
(trailing slash). Transport is Streamable HTTP. Legacy `/mcp/` remains for
OpenClaw and Hermes.

**Already deployed:** use your instance's public HTTPS origin. Forks must use
their own host, not the maintainer's `agent-call.fly.dev`, unless they operate
that app.

**Local process:** boot with dummy or real credentials, then tunnel. Official
Grok tunneling examples use ngrok or Cloudflare. Agent Call uses Streamable
HTTP, which Cloudflare quick tunnels support; their SSE transport does not.

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
ngrok http 8000
# or: cloudflared tunnel --url http://localhost:8000
```

Set `PUBLIC_BASE_URL` to the tunnel origin (no trailing slash) before any live
call. Prepare-only MCP checks do not need Twilio or OpenAI to succeed.

Free-tier tunnel URLs change on restart; remove the old Grok connector and add
the new URL.

## 2. Enable OAuth and add a Grok Bot custom MCP connector

On the Agent Call host (see [self-hosting](../self-hosting.md#optional-grok-oauth-self-hosted-single-owner)):

1. Generate an owner secret. Keep the original in a password manager.
2. Hash it with `uv run python scripts/hash_grok_oauth_owner_secret.py`.
3. Set `GROK_MCP_OAUTH_ENABLED=true` plus the hash, signing key, and storage
   encryption key. Access tokens last one hour; refresh tokens last up to 90
   days and rotate on every use, so expiry of the access token does not require
   another login. Public `/register` is capped at 64 clients (unused clients
   older than 30 days are eligible for eviction). OAuth audit rows are kept for
   90 days and capped at 2048 newest records. Unauthenticated DCR can still
   cause bounded churn but cannot grow those tables without bound. See
   [self-hosting](../self-hosting.md#optional-grok-oauth-self-hosted-single-owner).
4. Confirm no active calls, then deploy only after explicit approval.

In the **Grok Bot** desktop app (official in-app path):

1. Open **Settings → Plugins**.
2. Choose **Add** (custom / other MCP when offered).
3. Name: whatever you want.
4. Server URL: `https://YOUR_HOST/grok/mcp/`
5. Do not paste the owner secret, MCP bearer, or user id into Grok chat.
6. Complete the browser authorization page hosted by **this** Agent Call
   instance. Enter the owner secret there.
7. Enable the connector. Installed connectors are account-wide.
8. In a Bot conversation, type `@` and attach the Agent Call connector.

Equivalent Grok chat path: [grok.com/connectors](https://grok.com/connectors) →
**New Connector** → **Custom** → same URL. Grok discovers OAuth from the MCP
server.

## 3. Verify the seven tools

After the connector connects, confirm Grok Bot discovered **exactly** these
tools:

| Tool | Role |
| --- | --- |
| `prepare_phone_call` | Validate policy, persist a plan. Does not dial. |
| `start_phone_call` | Dial only after a new explicit confirmation. |
| `wait_for_call_event` | Canonical live-call loop. |
| `get_phone_call` | Snapshot. |
| `answer_call_question` | Mid-call answer the user actually supplied. |
| `end_phone_call` | Manual stop. |
| `get_call_result` | Final result after a terminal state. |

Automated, non-billable discovery (no Grok UI, no Twilio/OpenAI network):

```bash
scripts/live_smoke.sh
```

That script boots dummy credentials, initializes Streamable HTTP MCP, lists the
seven tools, persists a `plan_id` via `prepare_phone_call`, and rejects requests
missing either credential.

## 4. Install the private phone-call skill

Grok Bot has **no documented importable skill file**. Use the copy-and-paste
definition in [PHONE_CALL_SKILL.md](PHONE_CALL_SKILL.md).

1. Open the Bot that should place calls.
2. Paste the skill body from that file and ask:

   > Save this as a private skill called “Agent Call phone call”. Keep every
   > confirmation and safety rule. Enable it for this Bot.

3. If `/` does not list it: **Settings → Plugins → Yours** → enable it for the
   current Bot.
4. Reference it with `/` before a call request.

Optional Bot description (not a substitute for the skill):

> Place owner-requested phone calls only through the Agent Call MCP connector.
> Always prepare first, show the exact confirmation summary, and wait for a new
> explicit confirmation before starting. Never guess numbers or call twice
> after a failure.

Grok Bot Auto-review can separately prompt **Allow once** / **Deny** for tool
calls. That UI approval is **not** Agent Call confirmation. The Bot must still
wait for a **new user message** that clearly confirms the prepared summary
before calling `start_phone_call`. Consider a **Require Approval** Auto-review
rule for starting a phone call
([approvals](https://docs.x.ai/grok-bot/approvals-security-and-privacy)).

## 5. Prepare-only connection test (non-billable)

Do this **before** any real call. It must not call `start_phone_call`.

**Automated (required local check):** `scripts/live_smoke.sh` — dummy
credentials, temp SQLite, no provider charges.

**In Grok Bot (manual, still non-billable if you do not confirm):** attach the
connector with `@`, invoke `/Agent Call phone call`, and send:

> Prepare a test call to my configured test number. Show me the exact
> confirmation summary. Do not place the call. Wait for a separate message
> from me before starting.

Confirm:

- `prepare_phone_call` ran.
- A `plan_id` was returned (authority basis / `requested_by_owner` supplied).
- The Bot pasted the **exact** `confirmation_summary`.
- The Bot **stopped** and did not call `start_phone_call`.

Then reply **decline** / **do not call** so the prepared plan expires unused.

## Recommended live Grok Bot request (billable)

Only after the connector, skill, and prepare-only test work, and only when you
intend to spend Twilio + OpenAI Realtime money:

> Prepare a test call to my configured test number. After I separately confirm,
> call me, ask me to say the nonce AGENT-4821, acknowledge it, end the call,
> and report the final result. Do not place the call until I explicitly confirm
> the exact prepared summary.

Supply the real E.164 target, owner callback (`OWNER_PHONE_E164`), timezone,
and authority basis in the thread. Do not ask the Bot to invent them.

Expected sequence:

`prepare_phone_call` → show exact summary → **you send a new confirmation
message** → `start_phone_call` with that unchanged summary →
`wait_for_call_event` loop → `get_call_result` after a terminal state.

## Temporary manual testing: Twilio Dev Phone

This is a **manual, billable** path. It is not part of automated verification
and must not run in CI. Do not run `scripts/run_sip_canary.py` from this guide.

Twilio Dev Phone lets you answer an inbound call in a desktop browser. Official
warning: **using Dev Phone overwrites the selected number's webhooks**. Do not
point it at a number already used in production.

Requirements:

- A **spare US `+1` Twilio number**, different from `TWILIO_CALLER_ID`.
- Twilio CLI. Run `twilio dev-phone` and answer in the browser.
- Agent Call destination policy defaults to `+1` only. Mainland China `+86`
  outbound is **not** supported by this service (and is not a supported Twilio
  destination for this test).
- Outbound calls still come from `TWILIO_CALLER_ID`. The Dev Phone number is
  the **callee** (`target.phone`), not the caller ID.

Steps:

1. Buy or pick a spare `+1` number that is **not** the production caller ID.
2. Run `twilio dev-phone`, select that spare number, keep the browser tab open.
3. In Grok Bot, prepare a call whose `target.phone` is that spare E.164 number.
4. Confirm the exact summary in a **new** message only when you are ready to be
   billed and to answer in Dev Phone.
5. Complete the nonce check, then read the final `get_call_result` output.
6. Restore or clear the spare number's webhooks when finished.

Partial failures (AMD machine, timeout, extractor failure, missing nonce) must
be reported as-is. Never retry `start_phone_call` automatically.

## Limitations and handoff

1. **OAuth is local until a live Grok connector finishes the flow.** Label:
   Implemented locally; pending live Grok OAuth verification. After deploy,
   add the connector, complete browser authorization, run `tools/list`, then
   `prepare_phone_call` only.
2. **No importable skill file.** Skills are saved in the Grok Bot app, not
   loaded from this git repo. Copy [PHONE_CALL_SKILL.md](PHONE_CALL_SKILL.md).
3. **No marketplace package.** Do not invent a Grok Build `.grok` plugin for
   Grok Bot.
4. **Grok Voice is out of scope.** This integration does not change the voice
   provider.
5. **Real calls are billable.** Automated checks never dial. Dev Phone and the
   recommended Grok Bot request do.
6. **OAuth does not replace call safety.** An authenticated connector still
   cannot start a call without a valid prepared plan and exact explicit
   confirmation.

Remaining work that only you can do: generate the owner secret and hash,
configure deployment secrets, confirm no active calls, deploy after explicit
approval, add the Grok connector, complete browser authorization, run
`tools/list`, and run `prepare_phone_call` only.
