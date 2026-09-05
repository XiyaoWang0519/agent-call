# Run unattended phone tests

The source checkout includes `python -m scripts.live_phone`: a separate FastAPI
phone counterpart, an MCP runner, received-audio transcription and grading, and a
durable cleanup reaper. The production server does not import or host the harness.
The existing human-operated `run_sip_canary.py` remains available.

## One-time setup

1. Provision a disposable Agent Call deployment using the same revision being tested,
   one process and its own SQLite volume. Use live OpenAI/Exa credentials and an
   isolated Twilio account or subaccount. Retain normal signed webhooks and MCP auth.
2. Configure three distinct owned Twilio numbers: the application caller ID, automated
   callee, and automated owner. Set the test application's `OWNER_PHONE_E164` to the
   automated owner and `OWNER_DISPLAY_NAME` to `Automated test owner`. Never use a
   personal phone for these endpoints.
3. Give the test app a unique `LIVE_TEST_INSTANCE_ID` (at least 16 characters).
   Enable `ASK_AGENT_ENABLED=true` and `HOLD_DETECTION_ENABLED=true` for the full suite.
   The authenticated `/diagnostics/live-test` endpoint proves the instance identity,
   configured caller/owner hashes and required feature flags before dialing.
4. Host the harness checkout behind a dedicated public HTTPS origin with WebSocket
   support. Set both automated numbers' Voice URL to `https://HARNESS/incoming`, POST.
   Do not attach a TwiML Application or SIP trunk overriding that URL. The harness
   verifies number ownership and webhook routing before every run.
5. Copy `docs/live-phone.env.example` to `.env.live-phone` and fill it with authorized
   test configuration. The harness explicitly reads this file; it does not inherit
   the application's `.env.local`. The provider credentials must belong to the same
   isolated account as all three numbers. No numbers or credentials are provisioned
   automatically by this command.

Install locked dependencies, then run the phone service:

```bash
uv sync --all-groups --frozen
uv run python -m scripts.live_phone --env-file .env.live-phone serve --host 0.0.0.0 --port 8091
```

Run the independent reaper in a separately supervised process with the same configuration
and persistent artifact directory. It must remain running if the phone service stops:

```bash
uv run python -m scripts.live_phone --env-file .env.live-phone reap --watch
```

The service also runs a cleanup watcher. The external reaper handles expired reservations
after service crashes. Both operate only on registered call/conference resources and
reconcile ambiguous starts by their single-use plan. Cleanup failure keeps the lease
closed to new calls. Never delete `runs.db` to force a new run while calls are unresolved.
Incoming legs have provider time limits; application-side limits remain in force as a
last backstop. Keep the application available for final resource discovery.

## Basic conversation acceptance

Start with one real call covering conversation, web search, interruption, and hangup:

```bash
uv run python -m scripts.live_phone --env-file .env.live-phone run \
  --scenario basic --confirm-instance YOUR_TEST_INSTANCE_ID
```

`basic` checks an audible greeting, successful `search_web` execution and an accurate
spoken explanation of Python's pathlib module. It then asks for a long explanation,
waits to hear it begin, interrupts with an arithmetic question, and requires the agent
to stop speaking within 1.2 seconds and answer the replacement question. Finally it
requires an audible goodbye, the agent's `end_call` tool, a persisted result and verified
provider termination without forced cleanup. Ordinary replies wait for acoustic silence;
only the interruption step intentionally overlaps speech. The synthetic scripts and
independent ASR are English. The 240-second scenario deadline is not a latency benchmark.

## Run a suite

List scenarios without credentials, dialing, or API usage:

```bash
uv run python -m scripts.live_phone list
```

These commands use paid Twilio and OpenAI services. The instance confirmation explicitly
authorizes the configured automated destinations and scenario selection. There are no
per-call prompts, personal-phone defaults, or retries of a failed call-start request.

```bash
uv run python -m scripts.live_phone --env-file .env.live-phone run \
  --suite smoke --confirm-instance YOUR_TEST_INSTANCE_ID

uv run python -m scripts.live_phone --env-file .env.live-phone run \
  --suite full --confirm-instance YOUR_TEST_INSTANCE_ID

uv run python -m scripts.live_phone --env-file .env.live-phone run \
  --scenario transfer --scenario transfer-busy --confirm-instance YOUR_TEST_INSTANCE_ID
```

The full suite currently contains 22 scenarios covering all seven in-call tools and all
seven public MCP tools. Smoke selects conversation, interruption, IVR, and MCP termination.
Each scenario has a deadline. The full configured worst-case reservation budget is
4,230 seconds, below the default 5,400-second suite limit. Typical runs can finish sooner.
This is a duration authorization, not a dollar cap: conference participants, inbound and
outbound legs, OpenAI speech/transcription/grading, and Exa can incur separate charges.
Configure provider-side budgets as well. Run serially; Agent Call allows one active call.

The runner supplies synthetic owner fixture data for `ask-agent`. The not-found variant
models empty synthetic memory and conversation fixtures; it never searches real owner
accounts or claims that real personal sources were consulted.

## Interpret the results

The CLI downloads `report.html`, `report.json`, `junit.xml`, and separate sent/received
WAV files into `.live-phone-results/RUN_ID/`. Open `report.html` locally to hear the audio.
The server keeps its copy in `LIVE_TEST_ARTIFACTS`. Protect both directories and apply
retention externally. Artifacts are authenticated over HTTP and excluded from Git;
the harness never publishes them or adds them to a PR.

Exit 0 means every required assertion passed; 1 means completed tests failed; 2 indicates
configuration/transport failure or an unresolved run. Missing required feature flags
block the run before dialing. An independent transcription model hears only received
audio, without the expected nonce as a prompt. Deterministic assertions and a separate
semantic grader must both pass. The semantic grader's model is currently `gpt-4.1-mini`.
No language-model score overrides missing audio, wrong digits, scenario failure or cleanup.

If CLI output is lost, read active status with the harness bearer:
`GET /runs` lists unfinished reservations, and `GET /runs/RUN_ID` reports a known run.
Starting again while a reservation is unfinished returns HTTP 409. Do not blindly retry
a timed-out POST. The server continues an accepted run even if the CLI disconnects.

An application state of `transferred` does not finish the test: both automated phones
exchange spoken facts after the AI leaves, then disconnect. The report requires confirmed
provider cleanup and fails if the reaper had to force termination. App and harness
monotonic clocks are never subtracted from each other.

## Coverage boundaries and calibration

Automated transport and orchestration tests run locally with mocked paid providers.
The first live acceptance run is still required to validate routing, actual Twilio
DTMF delivery, AMD behavior, and acoustic thresholds. No live result is implied by unit
tests or a green PR check. Initial interruption budgets (1.2 s), voice energy threshold,
and hold silence allowance are provisional and must be calibrated with received audio.
The detector uses in-band DTMF; separate Twilio digit events are retained as diagnostics.

This suite does not yet automate a genuinely unanswered ringing endpoint, destructive
process/network fault injection, maximum-duration/maximum-hold calls, or every failure
permutation in the design matrix. Signature/replay protection, OAuth, duplicate/late
answers, extraction failure, and recovery have existing deterministic integration tests.
These are separate evidence from real-phone coverage. A full suite pass means the 22
listed scenarios passed, not that every proposed matrix row ran live.

Twilio-to-Twilio calls exercise real provider audio but do not prove a mobile carrier
or physical handset route. Speech fixtures use TTS and simple tone-based hold audio;
they do not represent every accent, room, codec, or IVR. Add scenarios and rerun to measure
variability; the runner does not retry failures until they appear green.

The ordinary CI job runs the credential-free tests. A trusted job may invoke the exact
suite command above against a provisioned harness. Keep live secrets out of fork jobs;
there is no automatic deployment or recurring paid workflow enabled by this change.
