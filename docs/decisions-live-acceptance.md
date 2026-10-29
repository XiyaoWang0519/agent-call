# PR 109 isolated phone acceptance — October 28, 2026

**Not ready for review.** The real `basic` and `closing-follow-up` scenarios passed,
but ordinary-answer latency acceptance and positive Decisions closing acceptance have
not passed. Keep PR 109 in draft and the production Decisions default disabled.

## Environment and evidence

All calls used the existing dedicated `agent-call-local-tests` resources: isolated
Twilio subaccount, three automated numbers, OpenAI project, application database,
receiver harness, and independent reaper. No production deployment, restart, webhook,
phone number, or personal destination was used. Public test tunnels and the existing
test webhooks were restored while provider resources were idle.

The tested product uses `gpt-live-1`, voice `marin`, and a hosted `gpt-5.6-terra`
backend with low reasoning. `CLOSING_DECISIONS_MODE=active` was enabled only in the
private isolated app configuration. Test speech uses `gpt-4o-mini-tts`; initial
observation used mini transcription, then `gpt-4o-transcribe`. Semantic grading uses
`gpt-4.1-mini` without supplying expected answers to ASR.

Product commit `5a4bc9b` passed basic. Product source was unchanged for the final
closing-follow-up run, which used harness commit `f8bd1c4` with ASR pre-roll. Earlier
failed development attempts used intermediate working trees and are not promoted to
acceptance evidence. The subsequent numeric `6pm` observer-matching fix has local
regression coverage; no passing phone run is claimed for that harness revision.

Reports, sent/received WAVs, transcripts, database contents, provider identifiers,
and credentials remain private. Local reports are at
`.live-phone-results/<run-id>/report.json` and `report.html`; they are not PR attachments.

## Passing real-phone scenarios

| Scenario | Run | Result |
| --- | --- | --- |
| `basic` | `run_2ba6cbf759a3aa1c254d6077` | 27/27 checks, semantic pass, verified cleanup without force |
| `closing-follow-up` | `run_be4834aed6c94b98efc555a3` | 23/23 checks, semantic pass, verified cleanup without force |

Basic includes a successful web search, an audible seasons explanation interrupted by
the callee, the replacement arithmetic answer, received goodbye, application hangup,
and the receiver-observed reply opportunity. Its unchanged acoustic predicate passed
with 0.70 seconds of overlap in the 1.2-second window and 0.18 seconds of voiced tail
(the existing maximum is 0.20 seconds). These numbers are not a new p95 estimate.

Closing-follow-up includes an actual new arithmetic request after the first goodbye,
the correct answer, a second goodbye, completed playback, and cleanup without force.
Some unnecessary closing narration remained audible despite prompt guidance. Its
complete playback was retained; the reply window was not shortened to hide it.

An earlier intermediate `no-outcome-tool` run, `run_cda00467e1f10586c7ecda69`, also
passed 25/25 checks. It is development evidence rather than validation of the final
interruption implementation.

## Latency and observer failures

Acceptance remains median ≤1 second and p95 ≤2 seconds from actual source speech end
to first independently verified received answer audio. Source timing excludes leading
and trailing fixture silence. Received voice frames and playback share the receiver's
monotonic clock. Application and receiver clocks are not subtracted.

The initial clean fact run `run_627bc1a8928399087b4d49fa` yielded seven usable diagnostic
samples: 1.440, 1.816, 1.935, 2.089, 2.051, 1.742, and 1.892 seconds. Their median is
**1.892 seconds** and nearest-rank sample p95/max is **2.089 seconds**. The eighth
answer failed receiver ASR matching, so this is an incomplete diagnostic series, not a
scenario pass or a population p95 estimate. Even this partial series misses the targets.

Final clean `run_9eb1140d552872e95aae8e37` and noisy
`run_2f3b9e9642517d2ef4cefd36` failed. The clean observer still transcribed the short
quantity answer and could not advance. The noisy run captured two
verified response samples, **1.336 and 1.925 seconds**, then failed because its numeric
time joined the numeral and meridiem, which did not match the original `\b6\b` expression. That expression is
now repaired without accepting other hours. Neither run completed all eight facts,
semantic grading, natural closing, and unforced cleanup; neither is a phone PASS.

Noise was deterministic broadband noise mixed into caller speech at 20 dB SNR, with
the original source silence and duration preserved. This does not validate continuous
room noise, unrelated background speakers, arbitrary accents, mobile carriers, or a
physical handset. These are real Twilio-to-Twilio SIP/conference calls.

The receiver now preserves 200 ms of audio before its voiced-energy threshold so
quiet consonants are not automatically removed from transcription chunks. Retained
audio re-transcribed with preceding context confirmed that some failed short answers
were spoken correctly. Short-answer ASR ambiguity still persists and must be resolved
before claiming complete fact-path coverage. No failed report was relabeled as a pass.

The primary SIP speech path runs independently of the harness ASR and grading calls.
Direct approved-fact replies have no application tool wait. Existing app telemetry
separately records backend/tool handling and carrier playback; the receiver measures
audible answer latency. The present evidence does not isolate provider-internal turn
detection from frontend generation or one-way SIP transport. A faster Decisions result
cannot be claimed to fix that unresolved interval. A paired baseline with identical
cached stimuli and a complete received-answer series remains outstanding.

## Decisions permissions and fallback

The isolated key currently lacks `/v1/decisions` write permission. Actual requests
returned **HTTP 403**. The safe fallback to Terra was exercised during real closing;
this is failure-path evidence, not successful active Decision classification.

Only the Decisions write permission was prepared in the existing isolated key's UI.
It was **not saved**: the browser's confirmation policy requires action-time approval
for expanding security-sensitive access. The explicit permission request is pending.
No new key was created, no key was rotated, and no unrelated permission was changed.

Positive readiness/keep-open Decisions, confidence calibration, and a meaningful
disabled/observe/active phone comparison remain unverified. Decisions token charges
remain excluded from the app's existing per-call cost estimate.

## Repairs and cleanup

The PR now includes:

- Silent sideband yield for sustained observed carrier overlap, with transcript support,
  short-spike filtering, per-turn deduplication, stale-turn rejection, and guards against
  late transcripts or silence hangover interrupting a normal new answer.
- Prompt guidance removing permission for listening sounds over an interruption and
  discouraging closing narration, without dropping queued audio or changing reply grace.
- Clean/noisy spoken-fact scenarios, actual source speech boundaries, independently
  matched received-audio latency gates, and preservation of failed interruption metrics.
- Stronger independent transcription, ASR pre-roll, rendered caller stimuli for the
  semantic grader, and acceptance of the correct numeric `6pm` transcription.
- Safe Decisions error-class/status logging without provider bodies or transcripts.

There were **17 billable development/acceptance calls** in this session: three full
scenario passes and fourteen failed attempts. Twelve failed attempts required forced
conference cleanup; those are explicitly not successful agent hangups. All seventeen
reports recorded provider cleanup as verified. No full 25-scenario suite was run.
Provider resource idleness was checked before every test-service restart.

Further paid retries were stopped once the remaining observer/latency and permission
limitations were established. Resume with the existing private configuration and reaper;
do not bypass the missing acceptance or change thresholds to promote this PR.

Final provider verification found zero queued, ringing, or in-progress calls, zero
active conferences, and zero unfinished harness reservations. The isolated app,
harness, independent reaper, and both temporary tunnels were then stopped. Private
configuration and all failed/passing artifacts were retained for a safe resume.

## Local validation

Ruff formatting/lint and strict mypy passed. The final offline suite passed
**787 tests, with 2 skips and 89.09% application coverage**. The local MCP smoke
script passed with dummy credentials and a temporary database, including bearer and
OAuth flows. These local results do not override the incomplete phone acceptance.

Official steering contract: [server-side controls](https://developers.openai.com/api/docs/guides/voice-server-controls).
