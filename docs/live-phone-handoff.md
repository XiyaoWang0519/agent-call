# Live phone testing: start here in a new session

Use this guide to resume the working local setup or reproduce it in another checkout.
The [runbook](live-phone-runbook.md) covers all scenarios and provider setup; the
[design](automated-live-testing.md) describes coverage goals beyond what has passed live.

## What has actually passed

On **2026-09-05**, `basic` passed **24 checks on a real Twilio phone call**, without a
human answering or speaking. Run ID: `run_dfbdac8eed4a72960587a9b5`.

1. The agent greeted the automated test desk.
2. The desk requested a web search of the official Python pathlib documentation.
   The real `search_web` tool succeeded, and the agent explained filesystem paths.
3. The desk requested a long seasons explanation and heard it begin.
4. While the agent was speaking, the desk interrupted with “what is two plus two?”
   Received audio showed 0.38 seconds of overlap and no voiced tail after the
   1.2-second interruption allowance. The agent answered “four.”
5. The desk asked for goodbye and hangup. The agent said goodbye and invoked
   `end_call`. All three call legs and the conference ended without forced cleanup.

Received audio lasted 82.375 seconds. This validates **basic only**, not the entire
22-scenario suite, a mobile carrier route, or a physical handset. Pytest and the local
MCP smoke script use mocked providers and cannot establish real voice acceptance.

The passing setup used `gpt-realtime-2.1` for the agent being tested. The counterpart
is a script voiced by `gpt-4o-mini-tts`, with independent `gpt-4o-mini-transcribe`
English ASR and `gpt-4.1-mini` semantic grading. It is not a second Realtime agent.
Read current settings before reporting a later run's models.

## Resume the existing machine

Work from the repository root. Check for these **ignored, private** files without
printing their contents:

| Path | Purpose |
| --- | --- |
| `.env.live-test-app` | Isolated application settings, credentials and database path |
| `.env.live-phone` | Harness settings, test numbers, instance ID and shared auth tokens |
| `.live-phone/SETUP.md` | Machine-specific notes, current URLs and previous results |
| `.live-phone/local.py` | Optional machine-local `check`, `app`, `harness`, `reaper` launcher |
| `.live-phone/app.db` | Test application database, separate from production |
| `.live-phone/runs.db` | Harness reservations and owned call/conference resources |
| `.live-phone-results/` | Downloaded reports and recordings |

These files are not included in a fresh clone. Do not overwrite them with example
values, export production secrets unnecessarily, or purchase replacement numbers just
because a new session cannot see the configuration. The existing resources are named
`agent-call-local-tests` in both Twilio (subaccount) and OpenAI (project). Discover
actual account IDs and numbers privately from configuration; do not hardcode them.

Confirm current processes and live calls before restarting anything. Historical PIDs,
ports, tunnel URLs and successful runs are not proof of current readiness. Never kill
a process by copying a PID from a prior session. Keep the independent reaper running
through call completion. Never delete reservations to bypass an unfinished run.

## Start the services

Install dependencies with `uv sync --all-groups --frozen`. On the original machine,
`.venv/bin/python .live-phone/local.py check` validates matching local settings without
calling providers or dialing. A successful config check alone is not live readiness.

If the private launcher is absent, this equivalent application command loads only the
explicit test settings, rather than booting from the default production dotenv files:

```bash
uv run python - <<'PY'
import uvicorn
from dotenv import dotenv_values
from app.settings import Settings
from app.main import create_app

settings = Settings.from_environ(dict(dotenv_values('.env.live-test-app')))
settings.require_runtime_configuration()
assert settings.live_calls_enabled and settings.live_test_instance_id
uvicorn.run(create_app(settings), host='127.0.0.1', port=8090)
PY
```

Run the harness and independent reaper in **two other terminals**:

```bash
uv run python -m scripts.live_phone --env-file .env.live-phone serve --port 8091
```

```bash
uv run python -m scripts.live_phone --env-file .env.live-phone reap --watch
```

The optional private launcher runs the same roles with `app`, `harness` and `reaper`.
Each is a long-running process. Do not start duplicate instances on the same database.
For a new machine, first provision the settings described in the
[one-time setup](live-phone-runbook.md#one-time-setup) and
[self-hosting guide](self-hosting.md). The app must use live profile, a separate SQLite
database, `OWNER_DISPLAY_NAME=Automated test owner`, and the harness's automated owner
number. Use a separate OpenAI project so its SIP webhook cannot reach production.

## Restore the public routes

The app and harness need **different HTTPS origins**. For interactive local testing,
start each tunnel in its own terminal:

```bash
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8090 --protocol http2
```

```bash
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8091 --protocol http2
```

Quick tunnel URLs change when restarted. Update all of these together, while idle:

| Setting or provider route | Destination |
| --- | --- |
| App `PUBLIC_BASE_URL` | Current app HTTPS origin |
| Harness `LIVE_TEST_APP_URL` | Same app origin |
| Harness `LIVE_TEST_PUBLIC_URL` | Current harness HTTPS origin |
| OpenAI **test project** webhook, `realtime.call.incoming` | App origin + `/webhooks/openai` |
| Twilio automated callee and owner Voice URL, POST | Harness origin + `/incoming` |

Retain signature checks and the existing webhook signing secret when editing the URL.
Do not rotate credentials merely because a tunnel changed. Do not change production
webhooks. Restart only affected idle test services after changing their configuration.
Quick tunnels and terminal processes are not permanent background infrastructure.

## Verify and run the basic test

Both public `/healthz` endpoints must return 200. The harness's preflight additionally
checks authenticated `/diagnostics/live-test`, instance identity, live-call capability,
number hashes, number ownership and destination webhooks before dialing. Health checks
do not verify model quota, signed event delivery, search availability or actual audio.

Read `LIVE_TEST_INSTANCE_ID` from the private harness config, then run:

```bash
uv run python -m scripts.live_phone --env-file .env.live-phone run \
  --scenario basic --confirm-instance YOUR_TEST_INSTANCE_ID
```

This places a **real billable call** to the configured test number. Follow the user's
authorized scope and spending limits; the argument confirms the instance, not an
unlimited budget. Existing authorization need not be requested again for each call.
Do not start `--suite full` when the request only covers one basic test.

A run prints its ID and PASS/FAIL. Exit 0 means all checks passed, 1 means assertions
failed, and 2 means configuration/transport failure or an unresolved run. A lost HTTP
response is not proof that no call started. Query authenticated `GET /runs` and
`GET /runs/RUN_ID`, then reconcile that run before issuing another start. The server
continues accepted work if the CLI disconnects. Preserve failed evidence; retry only
after diagnosing the failure or identifying a transient provider issue.

## Evidence and recordings

The CLI downloads `.live-phone-results/RUN_ID/report.html`, `report.json`, `junit.xml`,
`callee-received.wav` and `callee-sent.wav`; owner files appear for transfer scenarios.

- **Received at callee** = the agent under test speaking.
- **Sent by callee** = the scripted test desk speaking.
- Neither mono file alone contains both sides of the conversation.

To produce one playable stereo conversation, interleave the two mono WAVs from the
**same run**, preserving their existing timeline and padding the shorter one with
silence. Do not concatenate, trim silence, or normalize each turn's timing: that hides
overlap and latency. Put received audio on the left and sent audio on the right.
`full-conversation.wav` was created this way for the passing historical run; it is an
optional local artifact, not an automatic CLI output.

A pass requires all `report.json.checks` true, including successful search tool result,
acoustic interruption, agent `end_call`, stored result and cleanup with
`verified=true` and `forced=[]`. An independent semantic grader must also pass.
Never describe a forced hangup as successful agent hangup. Record the revision,
scenario, run ID, model settings, failed checks and cleanup status when handing off.
Keep keys, transcripts, databases and recordings out of Git and PR attachments.

## Known failures and fixes

| Symptom | What to check |
| --- | --- |
| WebSocket 403 after the phone answers | Twilio signature canonicalization: configured HTTPS/WSS origin and exact path, optionally trailing slash. Preserve signature, account, SID and one-use ticket checks; never bypass auth to make a call pass. |
| Caller talks over the greeting | Partial ASR is not a completed turn. Normal replies wait for one second of acoustic silence; `interrupt` deliberately bypasses that wait. |
| English word transcribed in another alphabet | Harness ASR uses `language="en"`; never supply the expected answer as an ASR prompt. |
| Agent refuses the interruption exercise | The scenario objective must authorize both the longer explanation and the replacement question; neither should conflict with the packet's constraints. |
| `initial_session_update_timeout` before callee dialing | A live attempt encountered a Realtime readiness failure. Check sideband logs, connection and provider health; do not count it as a conversation failure or readiness success. Confirm cleanup before retrying. |
| Correct answer but test timeout | Inspect received audio, utterance timing and step boundaries. Do not remove assertions or increase deadlines merely to hide a service regression. |
| CLI transport error or unfinished reservation | Inspect persisted run and provider state; keep the reaper alive. Do not blindly repeat POST /runs or delete runs.db. |

## Cost and validation notes

The successful 82-second basic call was estimated at **US$0.28–0.30**, before tax,
including agent, test counterpart, search and telephony. This was not a settled invoice.
The three dedicated numbers separately rent for **US$3.45/month** at the rate checked
on 2026-09-05. Failed attempts and reruns add usage; these historical figures are not
future quotes or spending caps. Recheck rates and actual provider records when asked.

The app's `cost.total_cost_usd` is incomplete for the test harness: it does not include
all test-side speech/grading, transcription and conference/media/inbound-leg charges.
A null Twilio price can mean billing is pending; it does not mean free. Reconcile cost
per run rather than assigning a whole day's usage to one call.

The final basic implementation passed 38 focused harness tests and 521 tracked tests
(two skipped), plus Ruff and application mypy. Docs-only edits do not require another
paid call. Changes to media, scenarios or assertions need focused regressions and a
new live result before claiming that the changed behavior works. The full suite still
needs broader live validation.
