# Conversational audio validation

## Acceptance scope

The noise target is ordinary non-speech background sound: hiss, colored noise, and
short irregular sounds. Intelligible competing conversation is a separate stress
case, not the acceptance gate for this change. This does not waive speech latency,
factual grounding, genuine interruption handling, or completed farewell playback.

## Changes

- Listen to the callee's greeting or menu before speaking, with a 1.5-second silence
  fallback. Keep automatic replies disabled and the agent muted until asynchronous
  AMD classifies the answer, so ordinary speech cannot leak into a voicemail recording.
  Continue immediately if the greeting was already committed during classification.
- Preserve the far-field input filter across session updates. Default to server VAD
  with 300 ms silence and threshold 0.5; semantic VAD remains configurable. Initial
  and activation acknowledgements must match an explicitly configured noise filter.
- Let the voice model speak its farewell directly. A separate text-only response
  classifies completion during playback without adding speech or a conversational
  tool round trip. Account for that response's tokens separately from audio ownership.
  Failed sends, provider rejections, invalid results, and missing results receive one
  retry, with a three-second deadline per attempt. Retry only the latest uninterrupted
  spoken response; stale attempts cannot override a newer decision. If both attempts
  fail, retain the active call rather than guessing that the objective is complete.
- Disconnect after completed playback and a three-second reply window. New callee
  speech or interrupted playback invalidates pending closing decisions immediately.
- Distinguish real hold announcements from menus requesting an answer.

## Local validation on 2026-09-07

The audio measurements below were collected at `cff6381`, before the review fixes
restored the AMD activation gate and added closing-check retries and filter-echo
validation. They do not certify the revised opening latency: the revised path adds
provider classification time while avoiding a second listen delay after an already
heard greeting. The review fixes have separate regression coverage; no additional
phone call was made for those fixes.

The actual application, live Realtime model, tool handlers and SQLite state were
exercised with fixed synthetic speech and paced local G.711 input/output transport.
Phone dialing and PSTN/SIP network timing were not part of these audio trials.
Recordings, configurations, hashes and event logs are retained privately.

Three clean closing trials completed both farewells and accepted a late follow-up.
Final speech-end-to-heard-response delays were 1.26, 1.32 and 1.64 seconds, versus
4.36 seconds in the earlier example. Independent transcription of played audio
confirmed complete farewells. This sample is not a maximum-latency guarantee.

The focused non-speech fixtures reused the same foreground speech and added seeded
pink or white noise with short decaying random-frequency tones, at a nominal 10 dB
speech-to-noise ratio. They contained no background speech. All three runs had
valid playback clocks and zero speech-start events attributable to noise-only
intervals. Event attribution used input audio timestamps and allowed VAD prefix
padding; it did not mistake delayed delivery of a real speech event for noise.

| Condition | Noise-triggered interruptions | Deliberate interruption | Late follow-up |
|---|---:|---|---|
| Pink noise, first run | 0 | Detection arrived too late to cut playback | Missed before hangup |
| White noise | 0 | Playback interrupted | Answered; 1.86 s response gap |
| Pink noise, repeat | 0 | Playback interrupted | Answered; 1.18 s response gap |

The first pink-noise run is a failure of timely speech detection and late-reply
survival, even though noise itself caused no interruptions. The repeat recovered
those behaviors but took 4.10 seconds to answer the deliberate interruption.
White noise also had a 2.46-second first-farewell gap. These remain latency limits;
passing noise rejection does not establish uniformly fast responses. The three-second
reply window was not shortened to improve latency measurements.

Intelligible-background-chatter stress testing still caused unwanted interruptions
and delays. It remains outside the accepted ordinary-noise scope.

Software verification: 607 tests passed, two skipped, 88.54% application coverage;
Ruff formatting/lint, strict mypy, pre-commit hooks and MCP/OAuth smoke passed.
These results establish local application behavior and limited live-model audio
coverage, not production deployment or a complete real-phone certification.

Review-fix verification on the same date: 740 tests passed, two skipped, 88.65%
application coverage; Ruff formatting/lint, strict mypy, and MCP/OAuth smoke passed.
The regressions cover pending AMD and concurrent activation, filter-echo mismatches,
closing-check errors/timeouts/stale retries, and the migrated received-audio grading.
