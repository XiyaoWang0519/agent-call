# Refactor execution record: R00–R06

Status: implemented in the working tree, with a review-driven hardening pass on
R04/R05 and the R06 typed-boundary slice. This file records what changed, the
baseline it was measured against, and what remains **unproven or incomplete**. It
does not claim an end-to-end phone or performance result.

Baseline commit: `7de5e69be9083a6fb484845645f1b05b4bc67254`.
See [refactoring_plan.md](refactoring_plan.md) for the R00–R23 work packages.

## Per-package status

| Package | Status | Remaining before sign-off |
|---|---|---|
| R00 | Implemented (shared gate); **operator confirmation pending** | Confirm `production` environment required reviewers in GitHub |
| R01 | Partial | Full tool schemas frozen; subprocess network isolation not claimed; sleep-based race tests not fully migrated |
| R02 | Partial | Narrow `Protocol` ports and fake clock deferred (R06) |
| R03 | Implemented | Boundary tests added; no schema change |
| R04 | Implemented (review pass) | No distributed exactly-once claim; targeted-cleanup retry is in-process only (durable ledger needs R07) |
| R05 | Implemented (review pass) | Readiness freshness budget is process-local |
| R06 | Implemented (typed-boundary slice) | `LiveBridge`/`Finalizer`/`TwilioBridge` narrow `Protocol` ports and a controllable clock remain open (deferred from R02) |

## R00 — Release boundary

- Added `scripts/verify.sh`, the single gate both merge CI and the maintainer
  deploy workflow call (format, lint over `app tests scripts`, strict mypy,
  `pytest --cov=app` with the coverage floor).
- `.github/workflows/ci.yml` and `.github/workflows/fly-deploy.yml` call
  `bash scripts/verify.sh`; the deploy job previously skipped `scripts/` lint
  and the coverage floor.
- `docs/maintainer-deploy.md` states that the `production` environment's
  required-reviewer rule is configured in GitHub, not in this repository, and
  that a push to `main` otherwise couples merge to release.
- Guarded by `tests/test_release_gate.py`.

**Still open:** the actual GitHub environment protection is not verifiable from
source. A human must confirm it.

## R01 — Behaviour baseline, contracts, and fault points

Baseline measured at the fixed commit, clean tree, Python 3.13.12, offline:

```bash
uv run ruff format --check app tests scripts   # pass
uv run ruff check app tests scripts            # pass
uv run mypy app                                # pass
uv run pytest -q --cov=app                     # 662 passed, 2 skipped, 88.43%
```

After R00–R05 (including the review passes) the same gate reported **699 passed,
2 skipped, 88.63%** (floor 85%). After R06 it reports **717 passed, 2 skipped,
88.90%** (floor 85%), with `app/errors.py` and `app/records.py` at 100%.

Added:

- `tests/test_no_network.py` plus an autouse guard in `tests/conftest.py`:
  non-loopback `connect` **and** `connect_ex` fail loudly unless a test opts out
  with `@pytest.mark.allow_network`. Known limitation: subprocesses and other
  socket entry points are not isolated by this guard.
- `tests/test_contract_snapshot.py` with `tests/snapshots/mcp_tools.json`:
  full MCP input/output schemas, annotations, HTTP route paths, and tool error
  codes, plus behaviour checks that missing calls raise `call_not_found`.
- `tests/test_release_gate.py`.

Repeat runs: the full suite was executed after each package and was green each
time; no flaky case was observed. This is not proof that the sleep-based race
tests are flake-free — they were not all rewritten to event barriers in this
batch, so that remains a known risk rather than a masked result.

### Fault-point table

| Point | Trigger | Expected observable |
|---|---|---|
| Before external create | capacity/deployment/plan rejection | no remote create; plan not consumed |
| Remote create racing cancellation | cancel after Twilio thread starts | late SID persisted; specific leg removed; call terminal |
| Cancel while persisting SID | cancel during ownership write | leg removed with the held SID; no orphan |
| Provider success + DB failure | persistence and DB termination unavailable | targeted provider cleanup still runs; call row is the recovery anchor |
| Cancel + remote definite failure | create fails after cancel | call terminalized (not left in prewarming) |
| Definite create failure (no cancel) | provider rejects | no SID, terminal reason set, no retry-create |
| Cancel during media-stream create | stream created after cancel | stream stopped; call terminalized |
| Late create during shutdown | `_stopping` set, create lands | must-finish cleanup still runs |
| Real `stop()` with an unreturned create | stop starts while provider create in flight | stop waits (must-finish owned task); late leg removed; caller gets `call_ending` |
| Valid agent leg lands after `ACTIVE` | initial agent REST result arrives after activation | leg adopted; call stays `ACTIVE`; nothing removed |
| Termination wins before adoption write | terminate commits while the adoption write is paused | adoption CAS fails; resource abandoned; caller gets `call_ending` |
| Targeted cleanup fails once | terminal/transferred call, `remove_participant`/`stop_audio_monitor` raises | bounded retry keeps ownership; conference left intact |
| Duplicate / out-of-order callback | repeated participant-status | single terminal transition |
| Restart with nonterminal rows | `recover_startup` | no second external create |
| Watchdog pass failure | `list_nonterminal_calls` raises | not-ready, new calls rejected, loop survives |
| Watchdog hung pass | pass never returns | not-ready after freshness budget; loop task not killed |
| Watchdog unexpected exit | task finishes early | not-ready, `/healthz` unchanged |

## R02 — Constructor injection and TestHarness

- `CallService.__init__` accepts `live=` and `finalizer=`; production
  construction in `app/main.py` is unchanged.
- `tests/conftest.py` builds the service through `TestHarness.build`, which
  explicitly owns the injected `twilio`/`live`/`finalizer`/`exa` doubles. The
  `_test_*` shadow attributes were removed.
- No public production setter was added to serve tests.

**Still open:** replacing the concrete `LiveBridge`/`Finalizer`/`TwilioBridge`
parameter types with narrow `Protocol` ports, and introducing a controllable
clock. R06 narrowed the *data* crossing the service boundary (records instead of
`Any` dicts) but deliberately did not rewrite the collaborator ports or the
telephony path. New tests use event barriers instead of fixed sleeps; migrating
the remaining sleep-based race tests is incremental by design.

## R03 — Atomic admission and termination-aware dial claim

- `Database.claim_plan_and_create_call` performs, inside one `BEGIN IMMEDIATE`:
  deployment-lock check, single-use plan consumption, single-live-call capacity
  check, and the call INSERT. It returns a `ClaimResult` (`claimed` /
  `plan_unavailable` / `call_busy` / `call_ending`). A rejected claim rolls back,
  so a transient `busy` does not burn the plan.
- `CallService.start` maps `busy` and `call_ending` to stable error codes.
- Added `Database.claim_callee_dial`, a one-shot CAS whose predicate includes
  lifecycle state and `termination_claimed=0`, used by `handle_sideband_open`.
- Capacity boundaries are covered: `TERMINATING` blocks with `call_ending`;
  `TRANSFERRED` and terminal `cleanup_pending` calls do not block.
- Covered by `tests/test_call_admission.py`.

**Note:** local admission control, not a distributed exactly-once guarantee. The
remote participant is created after the local claim.

## R04 — Owned remote-resource lifecycle (review pass)

`_run_owned_remote_create` protects the whole create → persist-or-cleanup window,
not only the first await:

1. the operation runs as a **must-finish background task registered before the
   provider call**, so `stop()` waits for an unreturned create to reach
   commit-or-abandon; caller cancellation is propagated into the owned task so
   its own cancel path abandons what the provider created;
2. after the create returns, the call is checked for viability — "not closed",
   not "still in setup". A delayed but valid agent leg can legitimately arrive
   after the call is `ACTIVE`, so only `TERMINATING`/terminal states (or a
   stopping service) reject it;
3. the returned value is given to a **conditional** `commit` (`adopt_participant`
   / `adopt_media_stream`) whose state predicate is the atomic linearization
   point against termination: if termination commits first, adoption returns
   false and the resource is abandoned instead of reporting a normal success;
   and
4. if any step is interrupted or fails, `abandon` receives the known value
   (or `None` for a definite failure) and removes the specific remote resource
   using identifiers already held.

Key properties:

- `_abandon_participant` persists the SID best-effort, then calls
  `remove_participant` for that exact leg. This works even when the database —
  and therefore `terminate_call` — is unavailable, and it does not complete the
  whole conference (which could disturb an owner handoff).
- `_cleanup_resource` retries a failed targeted cleanup with a short bounded
  backoff as must-finish work, so one provider hiccup does not silently end
  responsibility for a leg or stream. A terminal or already-`TRANSFERRED` call
  keeps its owner-callee conference; only the named resource is touched.
- A cancelled create whose remote call then fails definitively still
  terminalizes the already-created call row instead of leaving it in
  `prewarming`.
- The carrier media stream is stopped by SID and the call is terminalized; the
  existing `live_activation_failed` path for a non-cancelled monitor failure is
  preserved.
- Cleanup runs as must-finish work and cannot mask the original cancellation.
- Applied to the agent participant, callee participant, and carrier media
  stream.

Covered by `tests/test_dial_cancellation.py` (cancel + success, cancel + failure,
cancel while persisting, provider success + DB failure, media cancel, late create
during shutdown, real `stop()` with an unreturned create, late valid agent leg
after `ACTIVE`, adoption losing the race to termination, and targeted-cleanup
retry after the call is terminal/transferred).

**Known limitation:** the targeted-cleanup retry is bounded and in-process. If the
process dies during the retry window (all attempts exhausted or the process exits
mid-retry), the specific leg/stream is not retried by startup recovery, because
the recovery queries cover nonterminal calls, conference completion, and pending
finalization — not a named participant SID on an already-terminal call. The SID is
logged for manual reconciliation. A durable per-resource cleanup ledger requires
the R07 migration work; until then this is an explicit, operator-visible gap.

## R05 — Supervision and readiness (review pass)

- The watchdog loop survives transient pass failures (bounded backoff), and an
  unexpected exit flips a health flag via a done callback.
- `supervision_ready()` now also requires a fresh last-success timestamp once the
  watchdog has started, so a pass that hangs without raising eventually stops new
  call admission. The hanging pass itself is never killed, because it may be
  running must-finish cleanup.
- `start` rejects new calls with `supervision_unavailable`; `GET /readyz` reports
  503 when supervision is unhealthy or stale. `/healthz` stays a constant
  liveness signal.
- Covered by `tests/test_supervision_readiness.py`, including loop-level tests
  that the real watchdog refreshes freshness and that a hung pass marks not-ready.

## R06 — Narrow type contracts and business-named operations

Scope: replace `dict[str, Any]` and ad-hoc string errors at the *data boundary*
without touching the state machine, the SQL, the transaction boundaries, or the
wire format. No ORM, no new provider SDK types, no service extraction.

Added:

- `app/errors.py`: `ErrorCode` is the single registry of stable refusal codes
  (`EXPECTED_ERROR_CODES` in the contract test is now derived from it), with
  `ERROR_MESSAGES`, `CallRefusal(ValueError)`, and `error_payload`/`error_json`.
  `str(CallRefusal)` is the same JSON payload the transports already forwarded, so
  `json.loads(str(exc))["code"]` assertions and the frozen snapshot are unchanged.
  Policy codes (`invalid_e164` and friends) ride through unchanged; they are a
  separate domain registry.
- `app/records.py`: five use-case records, not one universal row wrapper.
  - `PlanRecord` (validated `ContextPacket`, plan state, authority, expiry)
  - `LifecycleRecord` (state-machine verdict plus the `claimed_terminating` /
    `is_finished_termination` predicates used by termination reconciliation)
  - `QuestionRecord` (+ `to_event()` for the `wait_for_call_event` shape)
  - `AnswerCommand` (the validated answer submission, built from
    `AnswerCallQuestionRequest` at the MCP entry point)
  - `ToolExecutionResult` (the ask_agent/live-tool payload shapes)
- Typed DB exits that wrap the existing dict methods, so the historical API and
  the typed view cannot drift: `get_plan_record`, `get_lifecycle_record`, and
  `create_question_record` / `claim_question_answer_record` /
  `claim_question_expiry_record` / `get_question_record` /
  `get_question_records_after` / `cancel_pending_question_records`. Each delegates
  to the original method; no SQL or transaction was moved or duplicated.
- Business-named lifecycle operations on the calls mixin:
  `promote_to_ready_to_activate` / `promote_to_activating` / `promote_to_active`,
  replacing the anonymous `cas_state(call_id, expected, replacement)`. A source
  scan in `tests/test_contract_snapshot.py` keeps new code in `app/` from writing
  `state=` through the generic `update_call` (the escape hatch stays for test
  fixtures that seed historical rows).
- Adopted at the read paths: `CallService.prepare`/`start`,
  `handle_openai_incoming`, `handle_sideband_open`, the ask_agent question
  registration/delivery/expiry loop, `answer_call_question`, and the two
  termination-reconciliation reads. `Finalizer` and `OwnerTransferCoordinator`
  now take `PlanRecord` instead of `dict[str, Any]` + `plan["context"]`.
- Converged the last unstructured MCP error: `ToolError("unknown question")` is
  now `{"code": "unknown_question", "message": "unknown question"}`. This is the
  one intentional wire addition in R06 (a code for a previously code-less error);
  the frozen-code set and snapshot were updated with it.
- `tests/test_typed_records.py`: dict/record parity on the same fixture, the
  termination-claim predicates, question answer/expiry/cancel adapters, historical
  missing-key tolerance, null provider IDs, enum wire values, `AnswerCommand`
  attestation mapping, tool-result shapes, and the error-registry payload shape.

Rollback: delete `app/records.py`/`app/errors.py`, revert the call sites to the
`dict` reads and the literal `ValueError` payloads, and drop the wrapper methods.
The database schema and the wire format are otherwise untouched; the one caveat is
the added `unknown_question` code, which a rolled-back client would see as a code
rather than a bare string.

Not in this package (as specified): no ORM, no state-machine change, no error-text
change without a contract-test update. The media-teardown path still carries the
raw call row because its provider handles are owned by R12/R16/R17.

## Not done / still unverified

- No real phone call, SIP canary, live-phone run, or latency measurement was
  performed. No latency improvement is claimed.
- No wheel build or out-of-tree install (R22).
- No migration-ledger or old-reader compatibility work (R07); R03/R04/R05 add no
  schema changes.
- The `production` environment's GitHub approval rule is not verifiable from the
  repository.
- The webhook receipt-ordering decision (F11/R23b) is unchanged.
- R07+ structural extraction of `CallService` use cases is not started. R06
  prepared the data boundary (records + adapters) but extracted no service.
- Narrow `Protocol` ports for `LiveBridge`/`Finalizer`/`TwilioBridge`, a fake
  clock, and full migration of sleep-based race tests to event barriers remain for
  later packages.
- R06 left the media-teardown/termination write path on the raw call row, and did
  not convert `owner_transfer`'s transfer-claim reconciliation to a record.
- R04 targeted-cleanup responsibility is a bounded in-process retry; a durable
  per-resource cleanup ledger (surviving process death) needs the R07 migration
  work. Until then an exhausted retry is logged for manual reconciliation.
