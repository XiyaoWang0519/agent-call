# Refactor execution record: R00–R05

Status: implemented in the working tree. This file records what changed, the
baseline it was measured against, and what remains unproven. It does **not**
claim an end-to-end phone or performance result.

Baseline commit: `7de5e69be9083a6fb484845645f1b05b4bc67254`.
See [refactoring_plan.md](refactoring_plan.md) for the R00–R23 work packages.

## R00 — Release boundary

- Added `scripts/verify.sh`, the single gate both merge CI and the maintainer
  deploy workflow call. It runs format, lint over `app tests scripts`, strict
  mypy, and `pytest --cov=app` (coverage floor included).
- `.github/workflows/ci.yml` and `.github/workflows/fly-deploy.yml` now call
  `bash scripts/verify.sh` instead of inlining different command lists. The
  deploy job previously skipped `scripts/` lint and the 85% coverage floor.
- `docs/maintainer-deploy.md` now states that the `production` environment's
  required-reviewer rule is configured in GitHub, not in this repository, and
  that a push to `main` otherwise couples merge to release.
- Guarded by `tests/test_release_gate.py`.

Operator action still required (not verifiable from source): confirm required
reviewers / manual approval on the `production` environment.

## R01 — Behaviour baseline and fault points

Baseline measured at the fixed commit, clean tree, Python 3.13.12, offline:

```bash
uv run ruff format --check app tests scripts   # pass
uv run ruff check app tests scripts            # pass
uv run mypy app                                # pass
uv run pytest -q --cov=app                     # 662 passed, 2 skipped, 88.43%
```

After R00–R05 the same gate reports **684 passed, 2 skipped, 88.35%** (new tests
added; coverage above the 85% floor).

Repeat runs: the full suite was also executed after each work package (R02, R03,
R04/R05) and was green each time; no flaky case was observed. This is not proof
that the existing sleep-based race tests are flake-free — they were not
rewritten to event barriers in this batch, so that remains a known risk rather
than a masked result. Known failures: none recorded.

Added:

- `tests/test_no_network.py` plus an autouse socket guard in
  `tests/conftest.py`: any non-loopback TCP connect in the suite fails loudly
  unless a test opts out with `@pytest.mark.allow_network`. This is the offline
  boundary R01 asked for.
- `tests/test_contract_snapshot.py`: frozen MCP tool names/annotations, HTTP
  route paths, and tool error codes.
- `tests/test_release_gate.py`.

Live payload and prompt byte budgets remain covered by the existing
`tests/test_bridge_payloads.py` and `tests/test_prompt_limits.py` rather than a
duplicated baseline file.

### Fault-point table

| Point | Trigger | Expected observable |
|---|---|---|
| Before external create | capacity/deployment/plan rejection | no remote create; plan not consumed |
| Remote create racing cancellation | cancel after Twilio thread starts | late SID persisted; call terminated once |
| Definite create failure | provider rejects | no SID, terminal reason set, no retry-create |
| DB commit ambiguity | raised await after commit | reconciling read adopts exact state |
| Duplicate / out-of-order callback | repeated participant-status | single terminal transition |
| Restart with nonterminal rows | `recover_startup` | no second external create |
| Watchdog pass failure | `list_nonterminal_calls` raises | not-ready, new calls rejected, loop survives |
| Watchdog unexpected exit | task finishes early | not-ready, `/healthz` unchanged |
| Missing audio frames / stale frames | watchdog stale path | existing carve-outs unchanged |

## R02 — Constructor injection and TestHarness

- `CallService.__init__` accepts `live=` and `finalizer=`; production
  construction in `app/main.py` is unchanged.
- `tests/conftest.py` now builds the service through `TestHarness.build`, which
  explicitly owns the injected `twilio`/`live`/`finalizer`/`exa` doubles. The
  `_test_*` shadow attributes were removed and tests read the injected public
  attributes instead.
- No public production setter was added to serve tests.

Deferred to later packages: replacing the concrete `LiveBridge`/`Finalizer`
parameter types with narrow `Protocol` ports (R06's typed contracts) and
introducing a fake clock. New tests already use event barriers instead of fixed
sleeps; migrating the existing sleep-based race tests is incremental by design.

## R03 — Atomic admission and termination-aware dial claim

- `Database.claim_plan_and_create_call` now performs, inside one
  `BEGIN IMMEDIATE`: deployment-lock check, single-use plan consumption,
  single-live-call capacity check, and the call INSERT. It returns a
  `ClaimResult` (`claimed` / `plan_unavailable` / `call_busy` / `call_ending`).
  A rejected claim rolls back, so a transient `busy` does not burn the plan.
- `CallService.start` maps `busy` and `call_ending` to stable error codes.
- Added `Database.claim_callee_dial`, a one-shot CAS with a lifecycle predicate
  (`callee_dialed=0 AND termination_claimed=0 AND state IN (prewarming,
  ready_to_activate)`). `handle_sideband_open` uses it instead of a separate
  read plus generic flag toggle, so a terminating call cannot re-acquire a dial
  right.
- Historical multi-call fixtures opt out via
  `enforce_single_call_capacity=False`; production admission keeps enforcement on.
- Covered by `tests/test_call_admission.py`.

Note: this is local admission control, not a distributed exactly-once guarantee.
The remote participant is still created after the local claim.

## R04 — Cancellation-safe creates and late-SID compensation

- Added `CallService._run_cancellation_safe_create`: it runs each remote create in
  its own shielded task. On caller cancellation it waits for the task to settle,
  recovers a late SID, and runs a must-finish compensation before re-raising. A
  definite provider error is still treated as a failure (no blind retry).
- Applied to the agent participant, callee participant, and carrier media stream
  (`media_stream_sid` persisted late; failure of the stream is still surfaced to
  `_activate` so its existing `live_activation_failed` teardown is preserved).
- Covered by `tests/test_dial_cancellation.py`.

## R05 — Watchdog supervision and readiness

- The watchdog loop now runs each pass through `_watchdog_iteration`, which
  never lets a transient failure kill the loop, backs off with a bounded delay,
  and flips a supervision health flag.
- `CallService.supervision_ready()` gates `start` with a
  `supervision_unavailable` error and backs `/readyz`.
- Added `GET /readyz` (503 when supervision is unhealthy or no service).
  `/healthz` is unchanged and remains a constant liveness signal, so a platform
  restart policy is not coupled to supervision state.
- An unexpected watchdog task exit is surfaced as not-ready via a done callback.
- Covered by `tests/test_supervision_readiness.py`.

## Not done / still unverified

- No real phone call, SIP canary, live-phone run, or latency measurement was
  performed. No latency improvement is claimed.
- No wheel build or out-of-tree install (R22).
- No migration-ledger or old-reader compatibility work (R07); R04/R05 add no
  schema changes.
- The `production` environment's GitHub approval rule is not verifiable from
  the repository.
- The webhook receipt-ordering decision (F11/R23b) is unchanged.
- R06+ structural extraction of `CallService` is not started.
