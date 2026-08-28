# Phase status

**Current public work:** Phase 1 — self-hosted product release and two golden paths.

**Planning baseline:** private (not in this public repository). Public work follows [ADR-001](DECISIONS.md) and [ASSUMPTIONS.md](ASSUMPTIONS.md).

**Builder record date:** 2026-08-28

## Entry / exit

| Phase | Entry | Exit | Status |
| --- | --- | --- | --- |
| 0 | Plan accepted as baseline | Ten ADRs approved; no TBD affecting schema or provider spend | **Partial.** ADR-001 accepted. ADRs 002–010 and Section 19 questions are unresolved blockers (see [ASSUMPTIONS.md](ASSUMPTIONS.md)). No managed-provider spend. |
| 1 | ADR-001 accepted | Section 3.1 public DX gates that can be proven in this repo; evaluation/dummy boot; doctor; prepare-only smoke; fork-safe Fly template | **In progress (this increment implemented; not tagged)** |
| 2–9 | Prior phase exit + listed ADRs | See program plan | **Not started.** Do not begin. |

## Phase 1 record

| Field | Value |
| --- | --- |
| Commit SHA | pending verification on this sanitized branch |
| Public artifact versions/digests | none published (no signed wheel/image in this increment) |
| Database migration version | unchanged SQLite schema; no migration |
| Enabled flags | `AGENT_CALL_PROFILE` is unset by default in Settings (`effective_profile` is `live`). `agent-call serve` uses that Settings object for bind policy and refuses an unset profile when any core runtime credential is present (process env or dotenv). Dummy/Compose set `evaluation` explicitly. |
| Tests and exit codes | pending re-verification on this sanitized branch |
| Evidence | [evidence/](evidence/) |
| Known limitations | Five-person usability study, non-maintainer Fly live deploy, signed GHCR/SBOM provenance, and macOS CI are out of scope. Docker Compose CLI is present (v2.36.0) and `docker compose config -q` succeeds, but the **daemon is not running** (`Cannot connect to the Docker daemon`); Compose build/up, `/healthz` through Compose, smoke-prepare against Compose, and named-volume persistence after restart were **not exercised**. Source dummy boot evidence from the prior increment remains. Clean-machine, fresh non-maintainer Fly deployment, and rollback gates have not been run. |
| Rollback | [ROLLBACKS.md](ROLLBACKS.md) Phase 1 |
| Unresolved findings | ADRs 002–010; Section 19 owner questions |

## Forbidden in this increment

- Phase 2 engine-port extraction or wheel publish
- Creating `agent-call-cloud`
- Inventing launch jurisdiction, pricing, retention, or provider contracts
- Adding `tenant_id` / `MANAGED=true`
- Live SIP canary, number purchase, production deploy, webhook change, provider spend
