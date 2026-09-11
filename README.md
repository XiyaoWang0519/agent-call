

# Agent Call

[![CI](https://github.com/XiyaoWang0519/agent-call/actions/workflows/ci.yml/badge.svg)](https://github.com/XiyaoWang0519/agent-call/actions/workflows/ci.yml)
[![Secret scan](https://github.com/XiyaoWang0519/agent-call/actions/workflows/secret-scan.yml/badge.svg)](https://github.com/XiyaoWang0519/agent-call/actions/workflows/secret-scan.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Agent Call lets an AI assistant make phone calls on your behalf. Give it a task, review the call plan, and explicitly confirm before it dials. You can follow the call, provide answers, stop it, and retrieve the result and transcript.

Developed with Codex, Agent Call uses OpenAI GPT-Live SIP (`gpt-live-1`) with a delegated `gpt-5.6-terra` task backend for the phone conversation and Twilio to reach the recipient. You run the service yourself, using your own OpenAI and Twilio accounts.

**Works with ChatGPT Work and Claude web, verified with real question-and-answer calls. It does not work in regular ChatGPT mode.** See [client setup, test results, and verification limits](docs/browser-clients.md).

## What you need

| Requirement | Try locally without calls | Make real calls |
| --- | --- | --- |
| Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/) or pip | Required | Required for local installation |
| OpenAI API project with Live SIP access | No | API key, project ID, and webhook signing secret |
| Twilio account and voice-capable caller number | No | Account SID, auth token, outbound SIP and conference support |
| Your phone number | No | Owner callback number, in international E.164 format |
| Public HTTPS address | No | A tunnel to your computer, or a hosted instance; used by webhooks and browser clients |
| ChatGPT Work or Claude web access with custom connectors | No | Required to use that chat interface; availability depends on account/workspace settings |

**Optional:** an [Exa](https://exa.ai) key enables in-call web search. Without it, the service runs with search disabled. Docker Compose is an alternative for local evaluation; Fly.io is an optional hosting provider. SQLite is included—no separate database service is needed. A managed Agent Call account is not required.

OpenAI API usage and Twilio calls are billed through your own accounts, separately from any chat subscription.

## 1. Install and try it locally

The `0.1.0` wheel and source distribution are attached to the [GitHub release](https://github.com/XiyaoWang0519/agent-call/releases/tag/v0.1.0). They are not currently published to PyPI. Download an artifact from that release, or build from this checkout using the [package release guide](docs/package-release.md):

```bash
uv tool install /absolute/path/to/agent_call-0.1.0-py3-none-any.whl
mkdir agent-call-home
cd agent-call-home
agent-call doctor --dummy
agent-call serve --profile evaluation --host 127.0.0.1 --port 8000
```

Replace the example wheel path with its actual location. If the shell cannot find `agent-call`, run `uv tool update-shell` and open a new terminal. A [pip virtual environment alternative](docs/package-release.md#install-with-pip) is available.

Your current directory is the configuration and data directory. Always run setup, doctor, and serve from the same `agent-call-home` directory. No API keys or `.env.local` file are needed for evaluation. Leave the server running. In another terminal, change to that same directory and run:

```bash
curl -fsS http://127.0.0.1:8000/healthz
agent-call smoke-prepare
```

A successful smoke check prints `OK prepare-only smoke`: it connects, lists the seven tools, and prepares a plan. **It does not make a call.** Evaluation mode blocks starting calls with `live_calls_disabled`.

Contributing from source? Clone this repository, run `uv sync --all-groups --frozen`, and prefix the commands above with `uv run`. For Docker evaluation, run `docker compose up --build` from the checkout and use the same health and smoke checks (`uv run agent-call smoke-prepare`). Docker Compose exposes the service on the host's loopback address and keeps SQLite in a named volume. See [troubleshooting](docs/troubleshooting.md) for errors.

## 2. Start with automatic local setup

Stop the evaluation server with Ctrl-C, then run:

```bash
agent-call start
```

On first launch, this command downloads and verifies its own pinned Cloudflare tunnel helper, obtains a temporary public HTTPS address without an extra tunnel account, and guides you through configuration. You do not need to install Docker, Git, ngrok, or cloudflared separately. Internet access is required for package dependencies, the helper download, and service connections.

Enter your OpenAI API key, project ID and webhook signing secret; Twilio account SID, auth token and voice number; your owner callback number, name and timezone; and a dedicated browser login password. Exa is optional. The wizard displays the exact OpenAI webhook URL to configure for `live.transport.incoming`. It generates the service tokens and OAuth keys and stores only a hash of the owner login password.

Configuration and SQLite data live in `~/.agent-call`, independent of the current working directory. `--directory /path/to/private-folder` selects a separate instance. Existing configuration and authentication keys are reused. The command starts both the service and tunnel and displays the browser connector URL. Keep that terminal open and the computer awake through calls and finalization. Ctrl-C waits for idle calls before stopping.

The temporary address can change on each start. The launcher updates its own `PUBLIC_BASE_URL`, but you must update the OpenAI project webhook and recreate browser connectors when the address changes. This account-free option is for local trials; use a stable HTTPS origin or a [hosted instance](docs/self-hosting.md#deploying-your-own-fly-app) for continuous use.

To try the same launcher with dialing disabled, pass `--profile evaluation`; setup still collects configuration, but no calls can start. Live mode is the default for `start`, and every call still requires a reviewed plan and explicit confirmation. Provider accounts, webhook setup and browser authorization cannot be supplied by pip itself.

For a manually managed HTTPS origin, the separate `agent-call setup` and `agent-call serve` commands remain available; they use configuration in the current directory. See [local browser setup](docs/local-browser-setup.md) and [advanced self-hosting](docs/self-hosting.md#configure). The default destination policy permits `+1` numbers.

## 3. Connect ChatGPT Work or Claude web

`agent-call start` enables OAuth for browser clients during first-run setup. Use this server URL in your chat client's custom connector settings:

```text
https://YOUR_HOST/connect/mcp/
```

- **ChatGPT Work:** enable developer mode if your account/workspace permits it, create a custom app or plugin, and select OAuth. Attach the connector in Work; regular ChatGPT Chat could not prepare calls in our test.
- **Claude web:** add a custom connector with a name and the remote MCP server URL.

Complete authorization on your own Agent Call page using the owner login password you chose during setup. For an existing instance, follow the [manual browser OAuth configuration](docs/browser-clients.md). That guide also records UI steps, account restrictions, and exactly which checks have passed. Successful local tests do not establish browser compatibility.

For a client that can send custom HTTP headers, the separate `/mcp/` endpoint requires both `Authorization: Bearer <MCP_BEARER_TOKEN>` and `X-Agent-User-Id: <ALLOWED_AGENT_USER_ID>` on every request. See [self-hosting](docs/self-hosting.md) for details.

## 4. Prepare your first call

Tell your connected assistant the configured owner name, callback number, and timezone. Then ask it to prepare a call with the recipient's number, your objective, and the information it is allowed to share. For example:

> Prepare a call to the repair shop at [phone number] to ask whether they can fix my bicycle this week. Only ask about availability; do not book anything. Show me the plan before dialing.

Review the recipient and plan before explicitly confirming. Preparation never dials; starting a confirmed call in the live profile can incur OpenAI and Twilio charges. Afterward, ask for the result and transcript.

For deployment acceptance, follow the [manual phone check](docs/live-sip-canary.md), which requires a person answering the owner's phone. Automated testing against dedicated test numbers has a separate [live-phone runbook](docs/live-phone-runbook.md).

## Project status and limits

This is an **early-stage, v0.x, single-owner project** under the MIT license. Run one service instance with persistent SQLite storage. Do not deploy or restart during an active call. You are responsible for consent, disclosure, and transcript retention for your use case.

This public self-hosted edition runs on your accounts and infrastructure, without a managed account, remote license check, or phone-home requirement. A separate managed service is forthcoming.

## Learn more

- [Self-hosting](docs/self-hosting.md): configuration, tunnels, deployment, rollback, and tuning
- [Browser clients](docs/browser-clients.md): ChatGPT Work and Claude web setup and verification status
- [Technical reference](docs/reference.md): call sequence, MCP tools, safety properties, and components
- [Architecture](docs/architecture.md): state machine, persistence, webhooks, and finalization
- [Package release](docs/package-release.md): build, install, verify, and prepare a release
- [Contributing](CONTRIBUTING.md): credential-free tests, lint, and development workflow
- [Implementation records](docs/implementation/) and [live testing handoff](docs/live-phone-handoff.md): deeper contributor and operator notes
- [Support](SUPPORT.md) · [Security](SECURITY.md) · [Code of conduct](CODE_OF_CONDUCT.md) · [Maintainers](MAINTAINERS.md) · [Changelog](CHANGELOG.md) · [License](LICENSE)

Maintainer deployment details are in [maintainer-deploy.md](docs/maintainer-deploy.md).
