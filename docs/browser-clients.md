# ChatGPT and Claude web

Agent Call's first release targets ChatGPT Work and Claude web.
They connect to the same self-hosted server over Streamable HTTP with OAuth 2.1.
This is a single-owner service: use your own Twilio/OpenAI accounts and deployment.
Exa is optional; without it, the voice agent has no web-search tool.

Running the installed package on your computer? See [local browser setup](local-browser-setup.md)
for the required HTTPS tunnel and the distinction between Fly and local-package verification.

## Verification status

Verified on 2026-09-07 against an isolated Fly deployment, using dedicated
OpenAI/Twilio test credentials and a public AI demo. Both supported browser
clients completed the requested questions after the initial call-flow fixes.

| Client | OAuth and tool discovery | Prepare-only workflow | Real-call verification |
| --- | --- | --- | --- |
| Claude web (Free) | All seven tools discovered | Persisted plan and exact confirmation summary | Completed all requested questions and retrieved the final result; recording exposed a clipped closing summary before the playback fix |
| ChatGPT Work (Pro) | All seven tools discovered | Persisted plan and exact confirmation summary | Completed all questions, audible summary and goodbye, natural hangup, and final-result retrieval after the playback fix |
| Regular ChatGPT Chat (Pro) | All seven tools discovered | OpenAI safety checks blocked preparation; no plan created | Not available in this workflow |

The longer retest allowed eight minutes, with a six-minute conversational target.
Both clients obtained answers about after-hours service, a follow-up question,
outbound support, and initial setup. Neither call reached the watchdog limit.
Claude's callee leg lasted 142 seconds; Work's lasted 134 seconds. Independent
recordings showed that both spoken summaries were cut short, despite complete
server-generated transcripts. That finding led to the playback-drain fix below.

Claude declined another call to the same public demo after this fix because it
considered repeated testing inappropriate for that third-party line. We did not
bypass that refusal. Its completed question-and-answer flow is verified; a full
post-fix audio ending through Claude is not yet independently verified.

The final Work retest on commit `64dd292` passed the full recorded flow: all
requested answers, a follow-up, the complete spoken summary, thanks and goodbye,
the demo's acknowledgment, natural `voice_model_end_call`, and final-result
retrieval in Work. The callee leg was 173 seconds (187 seconds in the application
result); both provider call legs and the conference were completed. The independent
watcher did not force a stop. Dual-channel recording transcription confirms the
last goodbye is actually audible, rather than merely present in the generated
transcript. Across this retest sequence there were three real calls: one per
client before the playback fix, then one Work call after it. No additional Claude
call was placed after its refusal. The isolated host was returned to evaluation
mode, live provider credentials removed, and the prior test-project webhook
restored after all calls were idle.

These are bounded smoke tests, not a guarantee of every conversation or client
policy. The final Work recording still includes an initial delay and repeated
opening, so this is not evidence of consistently smooth turn-taking. Mid-call
owner questions, browser interruption recovery, and all failure
paths are not established by these demo inquiries. The calls ran without Exa.
Private recordings, independent transcription of both audio channels, browser
responses, and server evidence are retained outside Git.

Claude initially declined a fictional-business scenario and accepted a transparent
AI-demo inquiry. During the first run, its browser connection dropped while
monitoring; after reconnecting, tool discovery was needed to load
`get_call_result`. The later question-and-answer retest returned the result in the
same browser turn. `get_phone_call` returns metadata rather than the finalized
summary and transcript.

Local OAuth, authentication, and tool-contract tests are automated. Browser testing
caught and fixed a consent-page CSP issue that blocked the external OAuth callback.
Claude and ChatGPT Work also returned `live_calls_disabled` for evaluation-only
start attempts. Regular ChatGPT exposed no error code or detailed reason beyond:
“This tool call was blocked by OpenAI's safety checks. Please double check what you
are sending.” That blocked request was not retried.

## Debugging the first live browser runs

The initial two calls used the browser OAuth branch before it incorporated the
separately developed call-flow fixes. That build still waited for asynchronous
answering-machine detection before enabling normal replies, and generated an
opening before completing the unmute operation. The combined branch now activates
without that detection wait, unmutes before enabling replies, and listens for the
callee's greeting before generating an opening. Regression tests cover greetings
during activation, late voicemail classification, and unmute/termination races.
Both browser clients subsequently completed the requested question-and-answer
flow on this combined build. Independent recordings nevertheless caught truncated
closing summaries: the generated transcript included words that had not finished
playing before hangup. The audio-drain fallback now allows up to 60 seconds
(previously 12), still preferring the actual playback-stopped event and retaining
the final reply window. Regression tests cover both long playback and a missing
playback-stopped event. See the latest verification status above for live coverage.

The Work test's 150-second stop was imposed by the external test watcher, not an
Agent Call service timeout. It received several substantive answers before that
cutoff interrupted the last setup question. The Claude test ended naturally but
obtained neither of its requested service answers.

Both original extracted results incorrectly labeled the objective `completed`.
The summarizer now assesses each material requirement against cited transcript
turns. Finalization rejects an overall completion label when that assessment is
missing, uncertain, or includes unmet requirements. A normal `call_status`,
successful `finalization_status`, or `transcript_complete` flag does not establish
that the user's objective succeeded. The default result extractor is now `gpt-5.4-mini-2026-03-17`: nano still
misattributed answers when replaying these transcripts, while mini identified the
unanswered setup question and the incomplete Claude flow. This is evidence from
two saved calls, not a broad model benchmark. Default extraction cost estimates
use $0.75 input / $4.50 output per million tokens ([model pricing](https://developers.openai.com/api/docs/models/gpt-5.4-mini)).
Original test records are preserved; replay
results are separate diagnostic artifacts, not replacement live-call evidence.

## Prepare the server

Follow [self-hosting](self-hosting.md#browser-oauth-self-hosted-single-owner).
Enable `MCP_OAUTH_ENABLED` and configure the owner-secret hash, signing key, and
storage encryption key. The connector URL is:

```text
https://YOUR_HOST/connect/mcp/
```

The browser client never receives your Twilio or OpenAI API keys. Enter the owner
secret only on your own Agent Call authorization page, never into chat or the
connector URL. The page identifies the client and callback receiving access.

For initial testing, run `AGENT_CALL_PROFILE=evaluation`: tools can prepare a plan,
but starting is rejected with `live_calls_disabled`. Keep the host's management
and legacy MCP tokens private even in evaluation mode.

## ChatGPT and ChatGPT Work

1. Open Settings → Security and login → Developer mode.
2. Open Plugins and select the create button.
3. Enter a name (for example Agent Call), a short description, and the server URL.
4. Choose OAuth. The server supports dynamic client registration (DCR); do not
   paste provider keys into advanced client-ID/client-secret fields.
5. Review and create the connection, then authorize it on your Agent Call host.
6. Attach the connector to a new conversation and test preparation first. Test
   Chat and Work separately; access and tool execution can differ by surface.

Account/workspace policy may restrict developer mode. Directory publication is
separate from installing a private development connector.

## Claude web

1. Open Customize → Connectors → Add → Add custom connector.
2. Enter a name and the same server URL. Keep the detected authentication setting
   **Always required** and OAuth client **No client ID — register one automatically**
   (DCR), then add the connector and select Connect.
3. Authorize on your Agent Call host and enable the connector in the conversation.
4. Start with a prepare-only test.

Claude's documented Free plan permits one custom connector; verify the available
slot in the account. A paid subscription is not assumed. Remote connections come
from Anthropic's infrastructure, so the server needs public HTTPS reachability.

## First preparation (no call)

Tell the client the configured owner display name, callback number and timezone,
the target number, and a clear objective. Explicitly ask it to prepare only.
These values must match the instance's owner policy. Use the provided evaluation
numbers only on an evaluation instance; never invent numbers for a real call.

Expected result: `prepare_phone_call` returns a persisted `plan_id` and a
`confirmation_summary`. The client must display that summary and wait for a new
user confirmation. A connector's generic tool permission is not call confirmation.

## Live call workflow

1. Prepare, display the exact confirmation summary, and wait for the user.
2. On explicit confirmation, start that plan once with the unchanged summary.
3. Keep calling `wait_for_call_event`, using its returned cursor and `next_action`.
   An empty timeout result means keep waiting, not that the call finished.
4. Answer pending questions only from available, authorized sources. Never claim
   to have searched a source that this client cannot access.
5. After terminal state, retrieve `get_call_result` and report the actual outcome.
6. Use `end_phone_call` when the user requests cancellation.

The server owns the phone conversation. Closing a browser tab is not a hangup
command. Mid-call questions still depend on a client that continues monitoring;
this must be tested in each browser, including interruptions and stopped turns.

## Release acceptance

For each client record connection, tool discovery, prepare-only behavior, fresh
confirmation, starting, continued monitoring, mid-call answers, explicit hangup,
final results, and provider cleanup. Include a call without Exa. Save private call
evidence outside Git. A mocked test must never be labeled a real phone pass.

## Sources

- [OpenAI connection testing](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- [OpenAI MCP authentication](https://developers.openai.com/plugins/build/auth)
- [Claude custom remote connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
