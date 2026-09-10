# Live interruption comparison, 2026-09-10

In this six-call speech-only diagnostic, removing the application conversation prompt
and tools did not consistently reduce interruption or answer latency. This is a small
sample, not a statistical ranking or a production acceptance pass.

## Method

- Same isolated Twilio account, caller/callee numbers, SIP conference route, small jitter
  buffer, OpenAI project, GPT-Live model (`gpt-live-1`), voice (`marin`), and hosted
  Responses backend (`gpt-5.6-terra`, low reasoning) throughout.
- Configured: current migration voice/backend instructions and seven tools. Minimal:
  256-character voice instructions, a short backend prompt, no tools. The configured
  voice prompt was 3,573 characters. The same activation message and application
  monitoring remained in both variants. This isolates prompt/tool configuration; it
  does not remove the SIP route or every application service.
- Identical cached speech recordings: greeting, request for a thirty-second seasons
  explanation, then an arithmetic interruption after sustained received speech.
  Independent ASR verified the sent script. Each test receiver disconnected after
  the measurement window, deliberately excluding goodbye/backend-closing behavior.
- Order: configured, minimal, minimal, configured, configured, minimal. No web search
  was performed; this is narrower than the full basic acceptance scenario.
- Received audio frames and playback events share the receiver monotonic clock.
  Speech onset/end are derived from the source fixture, excluding its 40 ms leading
  silence and trailing silence. Stop time is the last voiced frame in the interrupted
  burst before at least 500 ms of silence; transcripts were checked to distinguish
  that burst from an acknowledgment or the arithmetic answer. These are transport
  endpoint observations with packet/jitter uncertainty, not physical handset measurements.
- The original 1.2-second check was evaluated unchanged, including its 0.2-second
  voiced-tail tolerance. It counts any speech during the interruption, including
  acknowledgments. No threshold was relaxed.

## Results

| Variant | Stop original speech, seconds (three trials) | Median stop | Median answer latency | Original acoustic check |
| --- | --- | --- | --- | --- |
| Configured | 1.42, 0.85, 0.64 | 0.85 s | 1.20 s | 2/3 |
| Minimal | 1.21, 1.04, 0.82 | 1.04 s | 1.26 s | 3/3 |

Answer latency runs from the interrupting question's actual source speech end to the
first received answer audio. All six independently transcribed answers were correct.
The three observations per variant do not establish p95 latency or repeatability.

## Interpretation

- There were no delegation or Responses backend events in any of the six calls.
  Waiting for Terra, a tool, or a database-mediated backend result therefore does
  not explain these measured interruptions.
- Configured results varied from 0.64 to 1.42 seconds. That variability is larger
  than the difference between the two medians. The results do not support stripping
  the application prompt/tools as a reliable interruption optimization.
- The slow configured trial continued its seasons speech for about 1.42 seconds,
  then separately said “Okay” while the caller was still speaking. The original
  all-audio check counts both. Its failure was partly acknowledgment overlap, but
  the old-topic stop itself still exceeded the 1.2-second target.
- Reflected Live audio was captured alongside received telephone audio. For the
  slow trial, the last voiced packet belonging to the original burst reached the
  sideband about 1.37 seconds after stimulus playback started; the last received
  voiced frame arrived at 1.46 seconds. These observations do not suggest a large
  additional application-output queue. They are not exact server-generation or
  one-way carrier-delay measurements: reflection packets contain 200 ms of audio
  and the sideband and media follow different network paths.
- This comparison does not isolate Live model decision time from SIP/network timing,
  nor compare Realtime contemporaneously. Earlier Realtime results remain historical.
  A transport-controlled Live/Realtime comparison and repeated full-basic runs would
  be needed to explain that broader difference.
- The minimal configuration is only an experimental control. One minimal run invented
  an appointment-related opening after approved context was removed; its semantic
  grading failed. It is not a proposed production configuration.

## Evidence and completion boundary

| Trial | Variant | Run ID |
| --- | --- | --- |
| 1 | configured | `run_68c8f043ae63da615e1a2d38` |
| 2 | minimal | `run_260bee303deb9014952ca9fa` |
| 3 | minimal | `run_e3aac6d6c0f0979e1e3954dc` |
| 4 | configured | `run_58526e5820796f082374383a` |
| 5 | configured | `run_7155518bbf7912dbf403a632` |
| 6 | minimal | `run_8e905b4bfa0a1d91a1f15bf1` |

All six calls finalized with provider cleanup verified and no forced cleanup.
The receiver intentionally ended each diagnostic call. All isolated calls and
conferences were verified idle afterward. Production was unchanged.

The standard harness reports remain FAIL: this private diagnostic emits
`comparison_interruption` measurements rather than the acceptance runner's
`interruption_verified` event. Some reports also retain semantic-grading failures.
The acoustic-check counts above are the unchanged numeric predicate applied to
the recorded measurements; they are not promoted full-scenario PASS results.

Private artifacts are under `.live-phone/<run-id>/` (reports and sent/received WAVs)
and `.live-phone/comparison/` (manifest, metrics, raw sideband evidence, stimulus
hash, provenance and analysis scripts). The diagnostic launcher is
`.live-phone/comparison.py`; it patches only isolated test processes. No product
code or acceptance thresholds were changed for this comparison.

See [the migration record](gpt-live-migration.md) for remaining production limits.
