# Rollbacks

Version-specific recovery for the public self-host product. These procedures do
not authorize a production rollback by themselves. Live-call rules still apply:
do not deploy or restart while a call is active; acquire the deployment lease
first.

## Phase 1 (evaluation profile, doctor, Compose, fork-safe Fly template)

**What changed:** settings profile, `CallService.start` evaluation gate, CLI,
docs, Compose, user `fly.toml` template. No SQLite schema migration. Default
`AGENT_CALL_PROFILE` remains `live` when the operator uses `uvicorn app.main:app`
with a filled `.env.local`.

**Rollback:**

1. Confirm no active calls (deployment lease POST; HTTP 409 means wait).
2. Revert to the previous git tag / image. Docs and assets revert with the tag.
3. Do not restore a newer SQLite file over older application code if a later
   phase has shipped schema changes. Phase 1 does not require that.
4. Verify `GET /healthz` and that `prepare_phone_call` / `start_phone_call`
   behave as in the restored tag.

**Active-call rule:** the evaluation gate cannot start a call. Rolling back
Phase 1 while a live call is in progress on a `live` profile is still forbidden;
media would drop. Use the existing deployment-lease path.

**Proof that rollback preserves active-call rules:** Phase 1 does not alter
lease acquisition, drain rejection, or confirmation requirements on the `live`
profile. Regression tests for those paths remain in `tests/test_activation.py`,
`tests/test_policy_and_db.py`, and `tests/test_teardown_recovery.py`.

## Maintainer Fly overlay

Maintainer production configuration lives in `deploy/maintainer/fly.toml` and
`.github/workflows/fly-deploy.yml`. User `fly.toml` is a template and must not
be pointed at the maintainer app. Rolling back a fork's Fly app uses that fork's
app name and the prior image reference, never the maintainer app.

## Managed product

There is no managed production in this repository. There is nothing to roll
back for Phases 3–9 here.
