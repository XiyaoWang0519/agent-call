# Refactor execution record: R00–R05

Status: implemented in the working tree, with a review-driven hardening pass on
R04/R05. This file records what changed, the baseline it was measured against,
and what remains **unproven or incomplete**. It does not claim an end-to-end
phone or performance result.

Baseline commit: `7de5e69be9083a6fb484845645f1b05b4bc67254`.
See [refactoring_plan.md](refactoring_plan.md) for the R00–R23 work packages.

## Per-package status

| Package | Status | Remaining before sign-off |
|---|---|---|
| R00 | Implemented (shared gate); **operator confirmation pending** | Confirm `production` environment required reviewers in GitHub |
| R01 | Partial | Full tool schemas frozen; subprocess network isolation not claimed; sleep-based race tests not fully migrated |
| R02 | Partial | Narrow `Protocol` ports and fake clock deferred (R06) |
| R03 | Implemented | Boundary tests added; no schema change |
| R04 | Implemented (review pass) | No distributed exactly-once claim |
| R05 | Implemented (review pass) | Readiness freshness budget is process-local |

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

After R00–R05 (including the review pass) the same gate reports **694 passed,
2 skipped, 88.73%** (floor 85%).

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

**Still open (R06):** replacing the concrete `LiveBridge`/`Finalizer` parameter
types with narrow `Protocol` ports, and introducing a controllable clock. New
tests use event barriers instead of fixed sleeps; migrating the remaining
sleep-based race tests is incremental by design.

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

1. the create runs in a shielded task; a late result is recovered on cancel;
2. the returned value is given to `commit`, which persists the SID; and
3. if either step is interrupted or fails, `abandon` receives the known value
   (or `None` for a definite failure) and removes the specific remote resource
   using identifiers already held.

Key properties:

- `_abandon_participant` persists the SID best-effort, then calls
  `remove_participant` for that exact leg. This works even when the database —
  and therefore `terminate_call` — is unavailable, and it does not complete the
  whole conference (which could disturb an owner handoff).
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
during shutdown, definite failure without cancel).

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

## Not done / still unverified

- No real phone call, SIP canary, live-phone run, or latency measurement was
  performed. No latency improvement is claimed.
- No wheel build or out-of-tree install (R22).
- No migration-ledger or old-reader compatibility work (R07); R03/R04/R05 add no
  schema changes.
- The `production` environment's GitHub approval rule is not verifiable from the
  repository.
- The webhook receipt-ordering decision (F11/R23b) is unchanged.
- R06+ structural extraction of `CallService` is not started.
- Narrow `Protocol` ports, fake clock, and full migration of sleep-based race
  tests to event barriers remain for later packages.
