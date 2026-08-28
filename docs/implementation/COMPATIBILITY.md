# Compatibility

Versions and contracts that this public repository currently ships.

Until `1.0`, minor versions may change unstable extension ports. The seven MCP
tools, prepare/start confirmation rules, and destination-policy invariants are
not extension ports.

## Public package

| Field | Value |
| --- | --- |
| Distribution name | `agent-call` (single tree; no extracted wheels yet) |
| Version | `0.1.0` (development; no tagged GitHub release) |
| Python | `>=3.12` |
| License | MIT |

Phase 2 may publish `agent-call-core` / provider wheels. That has not started.

## MCP

| Field | Value |
| --- | --- |
| Tools | `prepare_phone_call`, `start_phone_call`, `wait_for_call_event`, `get_phone_call`, `answer_call_question`, `end_phone_call`, `get_call_result` |
| Legacy auth | Bearer + `X-Agent-User-Id` on `/mcp/` |
| Optional OAuth | Self-hosted owner-secret OAuth on `/grok/mcp/` (disabled by default) |
| Evaluation | Same seven tools; `start_phone_call` returns `live_calls_disabled` |
| Operator CLI | `uv run agent-call {serve,doctor,smoke-prepare}` (`python -m app` equivalent) |

## Persistence

| Field | Value |
| --- | --- |
| Engine | SQLite (`sqlite:///` only) |
| Tenant columns | none |
| Managed Postgres adapter | not in this repository |

## Policy and price

| Field | Value |
| --- | --- |
| Default country allowlist | `["+1"]` (process env / secrets only; not dotenv) |
| Plan TTL | 600 seconds (literal) |
| Max call | 600 seconds (literal) |
| Price fields | estimated USD knobs on `Settings`; not a public invoice contract |

## Events

Self-host has no external event bus. Call events remain SQLite-backed sequence
rows consumed by `wait_for_call_event`.

## Images

No signed GHCR/SBOM provenance pipeline in Phase 1. The public `Dockerfile`
builds the self-host runtime image locally or on the operator's host.
