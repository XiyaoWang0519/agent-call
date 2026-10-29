# Approved spoken facts and Decisions closing review

This implements the first recommended experiment: give Live selected facts directly and
evaluate bounded closing readiness without replacing the Responses delegation. The changes
have offline regression coverage and partial isolated real-phone evidence. Basic and
closing-follow-up passed, but ordinary latency and positive Decisions acceptance remain
unresolved; see [the phone acceptance record](decisions-live-acceptance.md). Active mode
is experimental and disabled by default.

## Explicit spoken facts

Supply a small selection in the approved call plan, for example:

```json
{
  "spoken_facts": [
    "The reservation is for four people.",
    "Acceptable arrival times are 6:00 to 7:00 PM on November 2, 2026."
  ]
}
```

These are fields inside `context`, alongside the existing owner, target, objective, and
authority fields. They participate in plan approval, the 16 KiB context budget, and the
same sensitive-data checks as other context fields. There may be at most eight facts,
240 characters each, and 2,048 UTF-8 bytes of serialized fact data total. Existing plans
default to an empty list. Live also receives hard constraints and prohibited actions to
avoid answering from a fact that conflicts with the approved restrictions.
If facts plus restrictions exceed a 4 KiB voice-context budget, the whole preload is
omitted and facts remain available through the backend; restrictions are never truncated.

Only put facts approved for direct disclosure here. Live can answer simple questions from
them without delegation. They do not authorize commitments or tools. The backend retains
the complete context, including `relevant_facts`, preferences, and allowed commitments;
those fields are not automatically copied into spoken facts. Current information, unclear
authority, and interpretation still require the backend. This is prompt-based fact access,
not a guarantee that Live will never delegate a simple question.

## Closing configuration

| Setting | Default | Behavior |
| --- | --- | --- |
| `CLOSING_DECISIONS_MODE` | `disabled` | `disabled`, `observe`, or `active` |
| `CLOSING_DECISIONS_TIMEOUT_SECONDS` | `0.8` | Total snapshot/read/request deadline, >0 and ≤2 seconds |
| `CLOSING_DECISIONS_MIN_CONFIDENCE` | `0.95` | Required confidence and selected probability, between 0.9 and 1 |

The shared existing OpenAI client and credential send `POST /v1/decisions`, with
`model: gpt-6-luna` and one named choice question, `closing_readiness`. The supplied
choices are `ready_to_close`, `keep_open`, and `uncertain`. Evidence contains the approved
context, full ordered persisted transcript, observed assistant text, and source revisions.
Evidence larger than 64 KiB falls back rather than silently omitting earlier requests.
The model is instructed to treat evidence as data and distinguish actual farewells from
quotes, drafts, promises, and unresolved requests.

- **Disabled:** existing Terra closing review; no Decisions request.
- **Observe:** one Decisions request per farewell candidate, in a background task alongside
  the existing Terra review. It cannot change call state or delay the Terra request.
- **Active:** a decisive current `ready_to_close` enters the shared application closing
  path; a decisive `keep_open` suppresses this fallback closing attempt. Refusal, uncertain,
  low confidence, invalid output, unavailable evidence, errors, and deadline expiry retain
  the Terra review. A stale or terminal result is discarded without initiating a stale
  fallback review. Normal Responses tools remain available throughout the call.

Confidence thresholds are provisional, not calibrated against labeled phone calls.
Use observation mode to gather evidence before enabling active mode. Observe logs report
choice, confidence, source revisions, and whether the result was stale. Join those records
with the existing backend/tool evidence by call ID to assess agreement; this implementation
does not automatically label disagreements or compute an accuracy score.

Both observe and active modes add billable Decisions requests, and timeouts can still
incur provider charges. Returned input-token counts are logged when available. Existing
per-call cost estimates do **not** include Decisions usage; reconcile these experiments
against provider usage/billing rather than treating the existing total as exhaustive.

## Application checks and transport

Input transcripts, outbound transcript revisions, acoustic callee speech epochs, and
backend control events invalidate in-flight results. Pending backend responses/tools,
owner questions, undelivered answers, holds, missing playback evidence, and terminal
calls cannot be bypassed. Duplicate closing requests cannot reset the existing reply
window. There is no synthetic Responses function-call ID or fabricated tool output.

Both the Responses tool and active Decision use the same application closing gate and
carrier-silence scheduler. The three-second reply opportunity remains intact, starts
from completed carrier playback, and is canceled by new callee speech or transcript
activity. A Decision never directly hangs up a provider call.

## Latency acceptance and rollout limits

Target ordinary replies at median ≤1 second and p95 ≤2 seconds, measured from actual
callee speech end to first meaningful audio received at the callee. API completion,
first text, and generated audio are not substitutes. Closing classification does not
speed up ordinary answers. Failed active reviews can add up to the configured deadline
before Terra; observation mode avoids that serial delay.

The `closing_decision` log records client request duration/status; the correlated
`closing_decision_result` log records the semantic outcome and staleness. These are
application timings, not isolated provider inference or audible response measurements.
Existing transport/media latency evidence remains necessary to separate turn detection,
transport, generation, tool handling, and playback.

Before production activation, use the isolated automated phone setup in
[the live handoff](live-phone-handoff.md) and [runbook](live-phone-runbook.md). Compare
paired plans with and without spoken facts and disabled/observe/active closing modes.
Cover ordinary and approved-fact answers, searches, owner questions, clean/noisy audio,
genuine interruptions, completed playback, quoted goodbyes, post-goodbye follow-ups,
and carrier-verified termination. Record received audio, delegation frequency, false
closes, fallback rates, costs, and cleanup. Historical phone evidence does not validate
this implementation. Existing interruption latency acceptance remains unresolved.

Native hold/resume transcript triggers, fact-ID selection, IVR actions, primary Decisions
delegation, and dispatcher concurrency changes remain later experiments. This first slice
does not modify those paths or claim whole-suite phone acceptance.

API contract: [Decisions guide](https://developers.openai.com/api/docs/guides/decisions)
and [create reference](https://developers.openai.com/api/reference/resources/decisions/methods/create).
