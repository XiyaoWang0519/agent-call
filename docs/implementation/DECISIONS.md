# Decisions

Accepted architectural decision records for the public `agent-call` repository.
Superseding decisions replace the named ADR; they do not silently rewrite history.

## ADR-001-edition-boundary (accepted)

**Status:** accepted

**Date:** 2026-08-24

**Phase:** 0 (recorded) / 1 (public implementation)

### Context

Agent Call is a single-owner self-hosted runtime. A managed product is planned, but
the current process-global `Settings`, SQLite schema, and static MCP tokens must not
be stretched into a multi-tenant SaaS with a `MANAGED=true` flag or tenant columns.

### Decision

1. This public MIT repository remains the **self-hosted product** and the source of
   the reusable call engine.
2. A **private** managed repository (`agent-call-cloud`, not created in this phase)
   will own accounts, provisioning, tenant authorization, billing, and orchestration.
3. The managed product consumes tagged public packages/images/contracts. It must not
   copy this tree or vendor an unreviewed `main` commit.
4. The public edition stays single-owner. It does not add a global `MANAGED=true`
   flag, workspace/tenant columns on SQLite, a remote license check, or crippleware.
5. Self-hosted Grok OAuth remains a single-operator pairing flow. It is not managed
   login and will not share issuer, keys, or tables with a future managed OAuth server.

### Consequences

- Phase 1 work (evaluation profile, doctor, Compose, fork-safe Fly template) stays
  in this repository and must leave the public app independently releasable.
- Phase 2+ package extraction and the private control plane are blocked on later
  phase entry criteria. They are not started here.
- ADRs 002–010 remain unresolved product/legal/vendor choices; see
  [ASSUMPTIONS.md](ASSUMPTIONS.md). They must not be invented in code.

### Rejected alternatives

| Alternative | Why rejected |
| --- | --- |
| Long-lived `self-host` / `managed` branches | Drift and unreliable security-patch propagation |
| Private folders in the public repo | Accidental disclosure; weak CI/artifact boundary |
| `tenant_id` / `if managed` throughout `app/` | Contaminates the self-host product; isolation depends on every call site |
| Copying the call engine into a private repo | Safety fixes diverge |

## Later ADRs

ADRs 002–010 are **not accepted**. They are listed as blockers in
[ASSUMPTIONS.md](ASSUMPTIONS.md) until a product owner and, where required,
qualified counsel record an approved outcome.
