# GPT-Live migration

The voice path uses `gpt-live-1` with a `gpt-5.6-terra` Responses backend. The
frontend listens, speaks, and handles interruptions; the backend reasons over the
approved context and executes application-authorized tools. There is one integration
in `app/openai_live.py`, with no Realtime fallback.

## Configuration and deployment

Use a project enabled for Live SIP. Subscribe its signed webhook to
`live.transport.incoming` at `/webhooks/openai`, retaining the matching project key,
project ID, and signing secret. Realtime event subscriptions are insufficient.
Twilio still connects the conference to OpenAI using encrypted SIP. A signed,
authenticated Twilio monitor stream observes the callee's inbound and outbound audio;
the public origin must support WebSockets at `/webhooks/twilio/media/...`.

The supported voice settings are `LIVE_MODEL=gpt-live-1`, `LIVE_VOICE=marin`,
`LIVE_BACKEND_MODEL=gpt-5.6-terra`, and `LIVE_BACKEND_REASONING_EFFORT=low`.
Remove obsolete `REALTIME_MODEL`, `MINI_MODELS_ENABLED`, `INPUT_TRANSCRIPTION_*`,
`INPUT_NOISE_REDUCTION`, `TURN_DETECTION_MODE`, `SERVER_VAD_*`, and
`SEMANTIC_VAD_EAGERNESS` overrides.
The independent post-call extractor is unchanged. See `.env.example` for the actual
setting names and defaults.

Never deploy while calls are active. Acquire the deployment lease first and keep
production at one Fly Machine. Roll back to the previous image before debugging a
broken deployment; preserve its matching provider subscription and configuration.
This migration's local work does not establish production deployment acceptance.

## Protocol and completion boundaries

Live sessions are accepted and attached using `/v1/live/sessions`. Backend function
items arrive inside `response.event`; results use `response.item.create`, followed
by one `response.create` after all function outputs and backend completion arrive.
That backend command does not initiate a voice turn. Ordinary speech uses native
Live full duplex, without VAD commits or Realtime cancellation events.
An accepted final closing result is retained without starting another backend turn:
the farewell has already been spoken and the application owns the reply window.

Neither a backend completion nor a transcript proves audible playback. Application
closing waits for observed carrier audio to drain, then preserves three seconds for
a reply. New callee speech cancels pending closure. Missing carrier evidence fails
closed rather than pretending the goodbye finished. Final Live usage is confirmed
only by `session.closed`; a bounded timeout remains explicitly unconfirmed. Carrier
hangup and sideband finalization run concurrently, so delayed usage never extends
the callee's silent wait after the reply window.

If native delegation omits the closing handoff, an observed farewell candidate asks
the configured backend to review the conversation with `response.create`. That
candidate never directly hangs up. Pending backend work is not interrupted, ordinary
answers do not request this review, and the backend must still provide an authorized
closing request supported by the actual transcript. Full-duplex overlap preserves
assistant fragments; an old farewell without any fresh assistant speech cannot
close a new callee turn. The backend still assesses whether the farewell is final.

Closing actions are correlated to their backend response and callee turn. Live may
reuse a delegation ID across responses; a stale action cannot close a newer turn.

New cost records use Live duration plus separate backend tokens. Historical Realtime
cost columns remain readable for old calls, without enabling a legacy voice path.

## Validation

Real-phone evidence is recorded by the isolated harness described in
[the handoff](live-phone-handoff.md). Historical Realtime passes do not validate this
integration. Require received audio, search results, interruption timing, spoken
goodbye, the full reply window, and provider-verified cleanup without forced hangup.
The ordinary-answer target is median at most one second and p95 at most two seconds
from callee speech end to the first received answer audio. Measure interruption
separately; the basic harness allows 1.2 seconds to stop the interrupted answer.

On 2026-09-10, isolated real calls established acceptance, backend readiness, signed
monitor binding, audible conversation, and real web search. The `no-outcome-tool`
scenario passed all 26 checks in `run_6d02283a0b00ddad5f29723a`: spoken fact recall,
received goodbye, application-initiated closing, the reply window, final Live usage,
and verified provider cleanup without forced hangup. It included a repeated farewell;
the pass does not establish polished conversational timing.

The earlier `closing-follow-up` run `run_c7f882dafd58e72d08e16214` passed its
checks, including answering an unexpected question after goodbye, but exposed a
roughly 14-second silent disconnect delay from serial usage finalization. Carrier
hangup now runs concurrently with sideband finalization. The subsequent
`run_fa10ef36237ff8bd304f24b0` was interrupted by a network outage and is not
acceptance evidence.

On the final implementation, `run_a44f364dd77835882121ae48` answered the follow-up
and ended through `voice_model_end_call`, with final Live usage and all provider
resources completed without forced cleanup. Received media measured 3.11 seconds
between last voiced audio and remote stop. **This run remains FAIL:** independent
ASR transcribed the short arithmetic answer as `四`, so the harness never advanced
to its final goodbye request. The model transcript says “Four”; this discrepancy
does not justify overriding the failed scenario. Repeated closing narration also
remains a conversational quality limitation.

Local validation on the final code: Ruff formatting and lint, strict mypy, and
662 tests passed (2 skipped), with 88.43% application coverage. The local MCP smoke
also passed; it does not establish phone acceptance. No active, queued, or ringing
calls or active conferences remained in the isolated Twilio account after testing.

A subsequent six-call [configured-versus-minimal comparison](gpt-live-comparison.md)
found no consistent speed gain from stripping prompts and tools. It isolated
speech-only behavior and does not replace the failed full-basic evidence below.

**Production acceptance remains blocked by latency.** Basic runs
`run_a827ebf4a097481a2a11ab3a` and `run_425c82e2c7f9d4291cd40e49` answered the
replacement arithmetic question but failed the unchanged 1.2-second interruption
allowance. A sustained-overlap instruction cue did not resolve this and was removed.
Earlier failures also exposed readiness/signature and test-speech-fixture problems,
which were corrected; their failed reports remain available privately. Do not
interpret forced cleanup in those attempts as agent hangup.

These results do not establish the ordinary-turn median/p95 targets, noisy-call
acceptance, a mobile carrier route, a physical handset, or the full feature suite.
The production app, secrets, and webhook have not been migrated by these local tests.

Official protocol references: [Live overview](https://developers.openai.com/api/docs/guides/live),
[migration](https://developers.openai.com/api/docs/guides/live-migration), and
[prompting](https://developers.openai.com/api/docs/guides/live-prompting).
