# Plan: `ask_poke` — mid-call questions from the voice agent to Poke

**Status:** draft, grounded in `main` @ `d3e3000` ("Add Exa web search support for in-call voice agent", post-OpenAI-rollback).
All file:line references below are to that commit.

## 1. Goal

Give the in-call voice agent (OpenAI Realtime, `gpt-realtime-2.1`) a reliable, serialized channel to ask
Poke a question mid-call and relay the answer to the callee — instead of guessing, refusing, or relying on
Poke's ordinary ~2s `get_call_result` polling to notice anything.

Three new surfaces:

| Surface | Caller | Purpose |
|---|---|---|
| `ask_poke(question, reason)` | Realtime model (function tool) | Persist a pending question; model waits for the answer |
| `wait_for_call_event(call_id, after_sequence, timeout_seconds)` | Poke (MCP tool) | Long-poll for new questions / terminal state |
| `answer_call_question(call_id, question_id, answer)` | Poke (MCP tool) | Deliver the answer, exactly once, correlated to the question |

Correlation chain: `question_id` → stored OpenAI `tool_call_id` → `function_call_output` via
`RealtimeBridge.send_tool_result` (`app/openai_realtime.py:554-581`) → `response.create` so the model
speaks the answer.

---

## 2. Gating questions — verify BEFORE building (Phase 0)

Both of these can kill the feature. Neither is answerable from the codebase; both need live experiments.

### 2.1 Does Poke keep executing MCP calls after `start_phone_call`?

Today Poke's only documented post-start behavior is polling `get_call_result` until terminal
(`app/mcp_tools.py:53-64` docstring: *"Then poll get_call_result until terminal"*;
`StartPhoneCallOutput.poll_after_seconds = 2`, `app/models.py:109-115`). We do not know:

- whether Poke polls continuously for the whole call or suspends,
- whether Poke's MCP client tolerates a tool call held open for ~20s (long-poll),
- what Poke's client-side tool timeout is.

**Experiment (temporary, behind a debug flag, removed before merge):**

1. Log a timestamped line in `MCPAuthMiddleware` (`app/security.py:17-50`) for every MCP request
   (tool name + monotonic time). Run a real call; chart Poke's call cadence over the call lifetime.
2. Add a temporary `debug_wait(seconds)` MCP tool that just `await asyncio.sleep(min(seconds, 25))`
   and returns. Ask Poke (via its normal flow) to call it with 5/10/20/25s. Find the client timeout.
3. Test the wakeup path: with `POKE_PUSH_ENABLED=true`, POST a mid-call message to
   `https://poke.com/api/v1/inbound/api-message` (same mechanism as `Finalizer._maybe_push`,
   `app/finalizer.py:194-208`) and observe whether Poke reacts by calling back into MCP.

**Decision rule:** if Poke long-polls fine → long-poll is primary, push is a wake-up accelerant.
If Poke's client times out fast or suspends → push becomes the primary wakeup and
`wait_for_call_event` degrades to a short-timeout poll Poke calls after being woken. The MCP tool
shapes below work for both; only the default `timeout_seconds` changes.

### 2.2 Does the realtime websocket stay "alive" while a function call is outstanding?

Liveness is defined as *any inbound frame on the OpenAI sideband websocket*: the reader calls
`on_activity(call_id)` on every frame (`app/openai_realtime.py:305-306`), which feeds
`_note_call_activity` → batched `last_event_at` writes (`app/call_state.py:193-208`, `:239-264`).
The watchdog runs every 5s and terminates any nonterminal call whose activity is older than
`watchdog_stale_seconds = 15` with reason `"watchdog_stale"` (`app/call_state.py:2399-2451`).

If the model emits `ask_poke` and then everything goes quiet (callee silent, no audio deltas, no
transcription events), **the watchdog kills the call at ~15s — before a single 20s long-poll expires.**

**Experiment:** commit `0aed56f` added realtime event logging — use it. Place a real call, have the
model call `search_web`, and while the callee stays silent, log which event types (if any) OpenAI sends
during and after an outstanding/slow function call. Also observe a fully-silent ACTIVE call: how long
before frames stop?

**Decision rule:** regardless of the answer, implement the watchdog carve-out in §7 (it is cheap and
bounded). The experiment tells us whether the carve-out is belt-and-suspenders or load-bearing.

---

## 3. Core design decision: do NOT copy the `search_web` pattern

`search_web` is handled **inline**: `_handle_tool_call` awaits `self.exa.search(...)` directly
(`app/call_state.py:1037`). That is acceptable only because Exa is bounded at
`exa_search_timeout_seconds ≤ 10` (`app/settings.py`, default 3.0): while it runs, the per-call FIFO
dispatcher (`_dispatch_events`, `app/openai_realtime.py:322-332`) is blocked, and every other realtime
event for that call queues behind it (512-slot queue, overflow is **fatal** —
`REALTIME_EVENT_QUEUE_MAXSIZE`, `app/openai_realtime.py:29`, `RealtimeEventQueueOverflow` at `:312-315`).

A Poke round-trip is human-scale (10–45s). Blocking the dispatcher that long would stall transcripts,
`end_call`, everything — and risk fatal queue overflow. Therefore:

> **`ask_poke` is persist-and-return.** The `_handle_tool_call` branch durably records the question and
> returns immediately *without* sending a `function_call_output`. The model's function call is left
> open. The answer (or timeout) is later delivered **out-of-band** by a separate code path calling
> `send_tool_result(call_id, tool_call_id, output, continuation_instructions=...)` — exactly the frame
> pair (`conversation.item.create` with `function_call_output` + `response.create`) the bridge already
> sends atomically via the cancellation-shielded `_send_batch` (`app/openai_realtime.py:355-401`).

Holding the function call open is protocol-legal: `function_call_output` items may be created any time
after the `function_call` item exists. Serialization comes for free from the realtime protocol — the
model does not produce another function call on that response chain until the first output arrives and a
`response.create` is issued. The DB constraint (§4) is defense-in-depth, not the primary mechanism.

Note on conversation flow while waiting: automatic responses stay enabled
(`enable_automatic_responses`, `app/openai_realtime.py:512-552` region), so if the callee speaks during
the wait, the model can still respond ("they're just checking that now"). The prompt (§9) forbids it
from inventing the answer during this window.

---

## 4. Data model (`app/db.py`)

### 4.1 New table

Append to `SCHEMA` (`app/db.py:19-123`) — schema creation is idempotent `CREATE TABLE IF NOT EXISTS`
run on every `initialize()` (`_run_migrations`, `app/db.py:253-311`); no version table exists or is
needed:

```sql
CREATE TABLE IF NOT EXISTS call_questions (
    question_id     TEXT PRIMARY KEY,          -- uuid4, service-generated
    call_id         TEXT NOT NULL REFERENCES calls(call_id),
    tool_call_id    TEXT NOT NULL,             -- OpenAI function call_id (correlation key)
    sequence_number INTEGER NOT NULL,          -- per-call, monotonic from 1
    question        TEXT NOT NULL,
    reason          TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
                    -- 'pending' | 'answered' | 'expired' | 'cancelled'
    answer          TEXT,
    asked_at        TEXT NOT NULL,
    deadline_at     TEXT NOT NULL,             -- asked_at + ask_poke_answer_timeout_seconds
    resolved_at     TEXT,
    UNIQUE (call_id, sequence_number),
    UNIQUE (call_id, tool_call_id)
);
-- Hard invariant: at most one pending question per call.
CREATE UNIQUE INDEX IF NOT EXISTS idx_call_questions_one_pending
    ON call_questions(call_id) WHERE status = 'pending';
```

### 4.2 New DB helpers

All follow existing idioms — `BEGIN IMMEDIATE` on the single writer connection, atomic
`UPDATE … RETURNING` claims (compare `claim_termination`, `app/db.py:863-888`), sequence allocation
copied from `add_transcript_turn` (`app/db.py:998-1046`: `SELECT COALESCE(MAX(sequence_number),0)+1`
inside the write transaction, `IntegrityError` → rollback → typed failure):

| Helper | Semantics |
|---|---|
| `create_question(call_id, tool_call_id, question, reason, deadline_at)` | `BEGIN IMMEDIATE`; verify call exists and `state='active'`; verify no pending row (partial unique index is the backstop); allocate `sequence_number = MAX+1`; insert; return row. Failure modes returned as typed codes: `question_pending`, `call_not_active`. |
| `claim_question_answer(call_id, question_id, answer)` | `UPDATE call_questions SET status='answered', answer=?, resolved_at=? WHERE question_id=? AND call_id=? AND status='pending' RETURNING *`. `None` → not pending; caller distinguishes answered/expired/cancelled/missing by a follow-up read. **This claim is the exactly-once arbiter** between Poke's answer and the timeout task. |
| `claim_question_expiry(question_id)` | Same shape, `status='pending' → 'expired'`. Loser of the race gets `None` and does nothing. |
| `cancel_pending_questions(call_id)` | `UPDATE … SET status='cancelled' WHERE call_id=? AND status='pending' RETURNING *`. Called on termination and startup recovery. |
| `cancel_all_pending_questions()` | Blanket variant for `recover_startup` (every nonterminal call is being torn down anyway). |
| `get_question(question_id)` / `get_questions_after(call_id, after_sequence)` | Plain reads on the read connection. |

Answer size: bound `answer` at 4KB in the MCP layer (pydantic), mirroring the spirit of
`EXA_TOOL_OUTPUT_MAX_BYTES = 16KB` output capping (`app/exa_search.py:94-133`) — the answer is relayed
into a `function_call_output`, so it must stay small.

---

## 5. Realtime side: the `ask_poke` tool

### 5.1 Tool schema (`app/openai_realtime.py`)

Add a 5th entry in `build_accept_payload` (`app/openai_realtime.py:83-176`), alongside `search_web`
(`:122-144`):

```python
{
    "type": "function",
    "name": "ask_poke",
    "description": (
        "Ask the owner's assistant (Poke) one question it can answer from the owner's "
        "information — account details, preferences, confirmations not in your approved "
        "context. Tell the callee you are checking BEFORE calling this. You will receive "
        "the answer or a timeout as the function result. Never guess while waiting."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 5, "maxLength": 500},
            "reason":   {"type": "string", "maxLength": 200},
        },
        "required": ["question"],
        "additionalProperties": False,
    },
}
```

Gate the entry on `settings.ask_poke_enabled` (§8) so the tool is simply absent when the flag is off.

### 5.2 Model changes (`app/models.py`)

- Extend `RealtimeFunctionTool.name` `Literal` (`app/models.py:235-241`) with `"ask_poke"` — without
  this, `AcceptPayload` validation rejects the new tool.
- New `AskPokeRequest(BaseModel)` with `extra="forbid"`, modeled on `WebSearchRequest`
  (`app/models.py:272-283`): `question: str` (strip, 5–500 chars), `reason: str | None` (≤200).
  `extra="forbid"` blocks model-side parameter smuggling exactly as
  `test_search_web_rejects_model_control_of_provider_parameters` verifies for search.
- New `QuestionStatus` str-enum if useful, plus latency stages `ASK_POKE_ASKED`,
  `ASK_POKE_RESOLVED` in `LatencyStage`.

### 5.3 Dispatch branch (`app/call_state.py`)

New branch in `_handle_tool_call` (`app/call_state.py:1000-1145`), inserted before the unknown-tool
`else` (`:1138-1145`):

```
"ask_poke":
  1. Parse/validate → AskPokeRequest.
     Invalid → _send_nontransfer_tool_result(..., {"status": "error", "error": "invalid_question"}).
  2. Reject-fast guards (each returns an immediate tool result so the model recovers gracefully):
     - settings.ask_poke_enabled is False        → {"status":"error","error":"ask_poke_disabled"}
     - call_id in self._voice_end_pending        → {"status":"error","error":"call_ending"}
     - questions asked this call >= settings.ask_poke_max_questions_per_call
                                                 → {"status":"error","error":"question_limit_reached"}
  3. AWAIT db.create_question(...) — durability-first, same rationale as the advisory-outcome
     ordering in _send_nontransfer_tool_result (app/call_state.py:966-989): the question must be
     durable before anything observable happens. create_question returning `question_pending` /
     `call_not_active` → immediate error tool result as above.
  4. Persist tool receipt (db.record_tool_call) WITHOUT sending output — same persist-only shape as
     the transfer_to_owner branch (app/call_state.py:1116-1137), NOT _send_nontransfer_tool_result.
  5. Register in-memory pending state: self._pending_questions[call_id] = PendingQuestion(
        question_id, tool_call_id, deadline_monotonic)   # consumed by watchdog carve-out (§7)
  6. Notify waiters (§6.3) so any parked wait_for_call_event returns immediately.
  7. If settings.poke_push_enabled: self._spawn(push_question_to_poke(...)) — fire-and-forget POST
     to https://poke.com/api/v1/inbound/api-message with the question text + call_id + question_id,
     reusing the Finalizer._maybe_push pattern (app/finalizer.py:194-208; extract the HTTP bit into a
     shared helper, e.g. app/poke_push.py, so finalizer and this path share it). Failures swallowed.
  8. Arm the deadline: self._spawn(self._question_deadline(call_id, question_id), must_finish=False).
  9. Return — NO function_call_output yet. Dispatcher is free.
```

Total dispatcher hold time: one SQLite write transaction (~ms), not a network round-trip.

### 5.4 Answer delivery (out-of-band)

Triggered by `answer_call_question` (§6.2). Runs via `self._spawn(..., must_finish=True)` on the
CallService, not in any MCP request handler beyond the claim itself:

```
_deliver_question_answer(call_id, question_row):
  1. output = {"status": "answered", "answer": row.answer}
  2. await _guarded_send_tool_result(call_id, row.tool_call_id, output,
        continuation_instructions=
          "Poke answered your question. Relay the relevant part to the callee naturally, "
          "in one or two sentences. Do not read metadata or mention Poke by name.")
     _guarded_send_tool_result (app/call_state.py:914-941) already swallows benign
     sideband-closed errors, so an answer racing teardown degrades to a no-op.
  3. self._note_call_activity(call_id)  — the model is about to speak; refresh liveness eagerly.
  4. Clear self._pending_questions[call_id]; notify waiters (Poke may already be parked in
     wait_for_call_event for the *next* event).
```

### 5.5 Timeout delivery

```
_question_deadline(call_id, question_id):
  1. await asyncio.sleep(settings.ask_poke_answer_timeout_seconds)
  2. row = await db.claim_question_expiry(question_id)
     - None → Poke won the race (answered) or call was torn down (cancelled). Done.
  3. output = {"status": "timeout",
               "error": "no_answer_from_poke",
               "guidance": "Owner's assistant did not respond in time."}
     await _guarded_send_tool_result(call_id, row.tool_call_id, output,
        continuation_instructions=
          "You could not confirm this information. Tell the callee you cannot confirm it right "
          "now. Do NOT guess or invent an answer. Offer to take a message or proceed without "
          "it. Only offer transfer_to_owner if it is already authorized for this call.")
  4. Clear pending state, notify waiters, _note_call_activity.
```

The `claim_question_answer` / `claim_question_expiry` pair on `status='pending'` guarantees exactly one
`function_call_output` per `tool_call_id` — sending two outputs for one function call is a protocol
error, so this claim is the linchpin. A late `answer_call_question` after expiry gets a typed
`question_expired` result and **no** realtime frame is sent.

---

## 6. Poke side: two new MCP tools (`app/mcp_tools.py`)

Registered in `register_tools` (`app/mcp_tools.py:19`), same `get_service()` closure pattern.
This changes the tool count from five to seven — update the README's "exactly five tools" sentence and
the two assertions in `tests/test_security.py` (`test_exactly_five_mcp_tools` at `:281`,
`test_authorized_mcp_endpoint_exposes_exact_tool_set` at `:50`).

### 6.1 `wait_for_call_event(call_id, after_sequence=0, timeout_seconds=20) -> dict`

```
1. Clamp timeout_seconds to [0, settings.wait_for_call_event_max_seconds]  (default cap 25).
2. Loop:
   a. snapshot = call state (db.get_call). Unknown call_id → ToolError.
   b. events = db.get_questions_after(call_id, after_sequence)   # includes resolved ones,
      so Poke can observe expiry/cancellation it missed.
   c. If events or state is terminal (or TERMINATING) or timeout exhausted → return.
   d. Park on the per-call notifier (§6.3) for the remaining time; on wake, re-loop.
3. Return shape:
   {
     "call_id": ...,
     "state": "<CallState value>",
     "terminal": bool,                      # per TERMINAL_STATES, app/models.py:38-43
     "events": [
       {"sequence": n, "type": "question", "question_id": ..., "question": ...,
        "reason": ..., "status": "pending|answered|expired|cancelled",
        "asked_at": ..., "deadline_at": ...}
     ],
     "next_after_sequence": <max sequence returned, or after_sequence>,
     "next_action": "<canned guidance string, same style as StartPhoneCallOutput.next_action>"
   }
   On terminal: next_action says "call get_call_result".
```

Poke's whole loop becomes: `wait_for_call_event` → question? `answer_call_question` → repeat with
advanced cursor → terminal? → `get_call_result`. It subsumes the 2s status polling during the call.
Timeout with no events returns an empty `events` list (cheap to re-enter), never an error.

Long-poll safety: single uvicorn worker, no `--workers` flag (`Dockerfile:16`), fully async — a parked
`await` holds no thread. FastMCP is mounted `stateless_http=True, json_response=True`
(`app/main.py:42-47`), so the held request is a plain HTTP request; single-instance deployment is
already a hard constraint (README/AGENTS.md: one Fly machine, volume-local SQLite), so no cross-instance
fan-out problem exists. Verify Fly/Render proxy idle timeouts > 25s in Phase 0 (both are expected to be;
confirm, don't assume).

### 6.2 `answer_call_question(call_id, question_id, answer) -> dict`

```
1. Validate answer: non-empty, ≤ 4096 bytes (pydantic).
2. row = await db.claim_question_answer(call_id, question_id, answer)
   - Row returned → service._spawn(_deliver_question_answer(...), must_finish=True);
     return {"status": "accepted", "question_id": ...}.
   - None → read the row and return a typed non-exception result (idempotency, not errors):
       already answered (any answer text)          → {"status": "already_answered"}
       expired                                     → {"status": "expired",
                                                      "detail": "timeout already sent to the agent"}
       cancelled / call terminal                   → {"status": "call_ended"}
       unknown question_id or call_id mismatch     → ToolError("unknown question")
3. Never blocks on the realtime send — the MCP response returns as soon as the claim commits.
```

Duplicate answers are idempotent by construction (second claim finds `status='answered'`). Out-of-order
answers (answering q1 after q2 was asked) are impossible in practice — q2 cannot exist until q1's output
was sent — but the claim-by-`question_id` handles it anyway.

### 6.3 In-memory notifier (`app/call_state.py`)

```python
self._event_notifiers: dict[str, asyncio.Event] = {}   # call_id → Event

def _notify_call_event(self, call_id):        # called on: question created, question resolved,
    ev = self._event_notifiers.pop(call_id, None)       # terminal transition
    if ev: ev.set()

async def _wait_call_event(self, call_id, timeout):     # used by wait_for_call_event
    ev = self._event_notifiers.setdefault(call_id, asyncio.Event())
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(ev.wait(), timeout)
```

Hook `_notify_call_event` into `_finish_claimed_termination` (`app/call_state.py:2001-2172`) right after
the terminal state commits, so parked long-polls return promptly when a call ends. Pure in-process
state — correct because the deployment is single-instance by construction.

---

## 7. Watchdog carve-out (`app/call_state.py:2399-2451`)

Problem (§2.2): a genuinely quiet wait can exceed `watchdog_stale_seconds = 15`.

Fix, bounded so it cannot mask a dead call indefinitely: in `_watchdog_once`, before the staleness
check for a call, consult `self._pending_questions.get(call_id)`:

```python
pending = self._pending_questions.get(call_id)
if pending and time.monotonic() < pending.deadline_monotonic + WATCHDOG_QUESTION_GRACE_SECONDS:
    continue   # question outstanding and within its deadline (+ small grace) — not stale
```

- `WATCHDOG_QUESTION_GRACE_SECONDS ≈ 5` (one watchdog tick) covers the delivery frames after resolution.
- The carve-out window is exactly `ask_poke_answer_timeout_seconds + 5` — after that, normal staleness
  logic resumes, so a call that died *during* a question is still reaped, at most ~35–50s late.
- In-memory only (`_pending_questions`), consistent with the watchdog's existing in-memory
  double-checking (`activity_before` re-check at `:2421-2440`). After a restart the map is empty, which
  is correct: `recover_startup` kills every nonterminal call anyway.
- The `end_call` 15s fallback (`_voice_end_fallback`, sleep at `app/call_state.py:1168`) is untouched —
  `ask_poke` is rejected while `_voice_end_pending` is set (§5.3 guard 2).

The `max_call_seconds = 600` ceiling (Twilio-enforced, `app/call_state.py:674-679`) is intentionally NOT
extended: question round-trips spend the same 10-minute budget as everything else.

---

## 8. Settings (`app/settings.py`)

Follow the bounded-`Field` pattern (`exa_search_timeout_seconds`), **not** the `Literal[...]` pattern —
the `Literal` fields (`watchdog_stale_seconds`, `max_call_seconds`, …) are not actually tunable via env.

| Setting | Type | Default | Env var |
|---|---|---|---|
| `ask_poke_enabled` | `bool` | `False` (flip after canary) | `ASK_POKE_ENABLED` |
| `ask_poke_answer_timeout_seconds` | `float, gt=0, le=120` | `30.0` | `ASK_POKE_ANSWER_TIMEOUT_SECONDS` |
| `ask_poke_max_questions_per_call` | `int, ge=1, le=20` | `5` | `ASK_POKE_MAX_QUESTIONS_PER_CALL` |
| `wait_for_call_event_max_seconds` | `float, gt=0, le=25` | `20.0` | `WAIT_FOR_CALL_EVENT_MAX_SECONDS` |

The 30s answer deadline is a **callee-experience** number (dead-air tolerance), not a Poke-convenience
number. Phase 0 findings may tighten it. Nothing new is mandatory at boot, so
`require_runtime_configuration()` (`app/settings.py:139-154`) is unchanged; add the vars to
`.env.example`, `fly.toml`, `render.yaml` (commented out / `false`).

---

## 9. Prompt changes (`app/prompts.py`)

`realtime_instructions` (`app/prompts.py:8-86`) `# Tools` section gets an `ask_poke` paragraph in the
same style as the `search_web` guidance (`:66-74`). Keep it tight — the hard budget is
`REALTIME_INSTRUCTIONS_MAX_BYTES = 20KB` (`:5`) minus up to 16KB of approved context
(`CONTEXT_PACKET_MAX_BYTES`, `app/models.py:18`), i.e. ~4KB of prose headroom shared with everything
else. Content:

- Use `ask_poke` for facts only the owner/their assistant would know (account numbers already on file,
  confirmations, preferences) that are **not** in the approved context and **not** web-searchable —
  `search_web` remains the tool for public facts.
- **Before** calling it, tell the callee naturally that you'll check ("Let me check that for you —
  one moment"). While waiting, keep responding to the callee but never guess or invent the pending
  answer.
- On a `timeout` result: say you cannot confirm it right now, offer to take a message or continue
  without it; `transfer_to_owner` only if already authorized.
- One question at a time; don't re-ask the same question.

Also (conditional): only include the paragraph when `ask_poke_enabled` — pass the flag into
`realtime_instructions` or build the section list conditionally, mirroring however the tool schema is
gated. `tests/test_prompt_limits.py` asserts exact substrings (`:54-66`) and the final byte limit
(`test_realtime_instructions_enforce_final_byte_limit`, `:93-100`) — extend both.

The post-call extractor is unaffected functionally, but `EXTRACTOR_INSTRUCTIONS`
(`app/prompts.py:89-95`) should gain one line: treat mid-call Poke answers relayed by the agent as
agent-asserted, same evidentiary tier as the advisory outcome ("do not treat as ground truth").

---

## 10. Lifecycle integration

| Hook point | Change |
|---|---|
| `_finish_claimed_termination` (`app/call_state.py:2001-2172`) | After the terminal state commits: `await db.cancel_pending_questions(call_id)`; pop `_pending_questions[call_id]`; `_notify_call_event(call_id)`. The armed `_question_deadline` task then loses its expiry claim (row is `cancelled`) and exits silently. |
| `recover_startup` (`app/call_state.py:2224-2334`) | One blanket `await db.cancel_all_pending_questions()` before/alongside the nonterminal-call sweep — every live call is being force-terminated anyway (`"startup_recovery"`), and the sideband/tool_call_id are unrecoverable. Post-restart `answer_call_question` then returns `{"status": "call_ended"}`; `wait_for_call_event` sees terminal state. |
| `stop()` / `_spawn` | Deadline tasks are ordinary `_spawn` work — `self._stopping` already turns new spawns into no-ops (`app/call_state.py:113-122`); delivery tasks use `must_finish=True` so an accepted answer is not dropped by a concurrent graceful shutdown. |
| `end_call` interplay | `ask_poke` rejected while `_voice_end_pending` (§5.3). Conversely, if the model calls `end_call` while a question is pending (it shouldn't — prompt — but models drift): allow it; termination cancels the question via the hook above. |

---

## 11. Failure matrix

| Scenario | Behavior | Enforced by |
|---|---|---|
| Poke answers in time | Exactly one `function_call_output` (answer), model relays it | `claim_question_answer` |
| Poke answers after timeout | MCP returns `{"status":"expired"}`; **no** realtime frame | claim race: expiry won |
| Timeout fires after answer claim | Deadline task gets `None` from `claim_question_expiry`, exits | claim race: answer won |
| Duplicate `answer_call_question` | Second call returns `{"status":"already_answered"}` | status read after failed claim |
| Wrong `question_id` / mismatched `call_id` | `ToolError("unknown question")` | claim `WHERE question_id=? AND call_id=?` |
| Second `ask_poke` while one pending | Immediate tool result `{"status":"error","error":"question_pending"}`; call continues | `create_question` + partial unique index |
| Question limit exceeded | `{"status":"error","error":"question_limit_reached"}` | §5.3 guard 3 |
| Call torn down mid-question (callee hangs up, watchdog, owner) | Question `cancelled`; parked long-polls wake with terminal state; late answer → `call_ended` | §10 termination hook |
| Service restart mid-question | Call force-terminated by recovery; question `cancelled` before traffic | §10 recovery hook |
| Realtime send fails during delivery (sideband closed) | Swallowed, logged; question stays `answered` (durable) | `_guarded_send_tool_result` (`:914-941`) |
| Silent callee during wait | Watchdog carve-out until deadline + grace; then normal reaping | §7 |
| Poke never calls any MCP tool | Timeout path fires at 30s; model says it can't confirm; push (if enabled) was the wakeup attempt | §5.5 + §5.3 step 7 |
| Model invents an answer instead of waiting | Prompt forbids; canary transcript review; timeout guidance repeats "do NOT guess" | §9 |

---

## 12. Tests

Reuse the existing harness: `service` fixture + fakes (`tests/conftest.py` — `FakeRealtime` records
`tool_results` and `tool_result_continuations`, `:124-239`), `seed_call` (`:301-337`),
`wait_background()` (`:363-365`), and the `_tool_event(tool_call_id, name, arguments)` raw-event helper
from `tests/test_tool_transfer_safety.py:14-25`.

New file `tests/test_ask_poke.py` (service-level):

1. **Ask persists, no immediate output** — feed `_tool_event("tc_1", "ask_poke", {...})` on an ACTIVE
   call; assert a pending `call_questions` row with `sequence_number=1` and correct `tool_call_id`;
   assert `FakeRealtime.tool_results` got **nothing** for `tc_1`.
2. **Answer delivers correlated output + continuation** — call `answer_call_question`; after
   `wait_background()`, assert `tool_results[-1] == (call_id, "tc_1", {"status":"answered",...})` and
   the continuation instruction was passed; row `answered`.
3. **Timeout exactly-once** — shrink `ask_poke_answer_timeout_seconds` to ~0.05; assert timeout output
   sent once; then `answer_call_question` → `{"status":"expired"}` and `len(tool_results)` unchanged.
4. **Answer/timeout race** — monkeypatch to fire both concurrently; exactly one output for `tc_1`
   (assert on the claim, and on `tool_results` count).
5. **Duplicate answer idempotent**; **unknown question_id → ToolError**; **mismatched call_id →
   ToolError**.
6. **Second ask while pending** → immediate `question_pending` error result; call stays ACTIVE
   (mirror `test_search_web_failure_continues_call_with_safe_error` shape).
7. **Question limit** → error result at N+1.
8. **Termination cancels** — terminate mid-question; row `cancelled`; deadline task sends nothing;
   late answer → `call_ended`.
9. **Recovery cancels** — seed a pending row + nonterminal call, run `recover_startup` (pattern from
   `tests/test_teardown_recovery.py`); row `cancelled`.
10. **Watchdog carve-out** — pending question + stale `last_event_at`: `_watchdog_once` does not
    terminate; advance past deadline + grace with the question somehow still pending: it does.
    (Extends `tests/test_activity_heartbeat.py` idioms.)
11. **ask_poke while `_voice_end_pending`** → `call_ending` error result.
12. **Disabled flag** — tool absent from `build_accept_payload` (extend
    `tests/test_openai_payload.py`); dispatch branch returns `ask_poke_disabled` if called anyway.

`wait_for_call_event` tests (same file or `tests/test_wait_for_call_event.py`):

13. Immediate return when events already exist past the cursor; cursor advance semantics.
14. Parked wait wakes on new question (< timeout); wakes on terminal transition; returns empty
    `events` on timeout without error.
15. Timeout clamped to `wait_for_call_event_max_seconds`.
16. Unknown call_id → ToolError.

Update existing assertions:

17. `tests/test_security.py:50` and `:281` — five → seven tools.
18. `tests/test_prompt_limits.py` — new substring assertions + byte limit still passes.
19. `tests/test_openai_payload.py` — accept payload includes `ask_poke` when enabled.

Live canary (manual, after Phase 0):

20. Real call where the model asks Poke **two consecutive questions**; verify strict q1→a1→q2→a2
    ordering in `call_questions.sequence_number`, transcripts, and no watchdog termination; verify
    timeout behavior by deliberately not answering a third question.

---

## 13. Delivery phases

| Phase | Content | Exit criteria |
|---|---|---|
| **0 — Verify runtime assumptions** | §2 experiments: Poke cadence logging, `debug_wait` tolerance probe, push-wakeup probe, OpenAI frame-cadence-during-outstanding-tool-call observation, Fly/Render proxy idle timeout check | Documented answers to §2.1/§2.2; long-poll vs push-primary decision made |
| **1 — Persistence + service core** | §4 schema/helpers, §5.3 dispatch branch, §5.4/5.5 delivery+timeout, §6.3 notifier, §7 watchdog carve-out, §10 lifecycle hooks, §8 settings — all behind `ask_poke_enabled=false` | Tests 1–12 green; full suite green |
| **2 — MCP surface** | §6.1/6.2 tools, README five→seven, security tests | Tests 13–19 green |
| **3 — Model surface** | §5.1 tool schema, §5.2 models, §9 prompts | Payload/prompt tests green |
| **4 — Canary** | `ASK_POKE_ENABLED=true` on one deployment; scripted calls incl. test 20; transcript review for invented answers / dead-air quality | Two-consecutive-question canary passes; no watchdog kills; timeout path sounds acceptable |
| **5 — Enable + docs** | Flip default guidance, README/AGENTS.md docs for the new tools, remove Phase 0 debug instrumentation | — |

---

## 14. Open questions (carried into Phase 0)

1. **Poke's MCP client timeout** — determines the default `timeout_seconds` for `wait_for_call_event`
   (20s vs something much smaller with push as the wakeup).
2. **OpenAI frame cadence during an outstanding function call** — determines whether the §7 carve-out
   is load-bearing or precautionary.
3. **Dead-air filler**: if canary shows callees hanging up during ~20s+ waits, add a v2 mid-wait
   nudge (at ~12s, inject a `conversation.item.create` system message + `response.create` telling the
   model to reassure the callee). Deliberately out of v1 scope — automatic responses to callee speech
   already mitigate, and the filler adds a new frame-ordering concern.
4. **Poke push payload shape** — `finalizer._maybe_push` sends `{"message": <json>}`; confirm the
   inbound API renders a mid-call question in a way that actually prompts Poke to act (vs. just
   showing the user a message).
