# Upstream-maintainer-only production overlay

Public users and forks should ignore this file. The user Fly template is
`fly.toml` with `app = 'YOUR_FLY_APP_NAME'`.

Maintainer production configuration lives in
`deploy/maintainer/fly.toml` (`app = 'agent-call'`,
`PUBLIC_BASE_URL = 'https://agent-call.fly.dev'`).
`.github/workflows/fly-deploy.yml` is upstream-maintainer-only: the deploy job
runs only when `github.repository == 'XiyaoWang0519/agent-call'` and deploys
that overlay:

```bash
flyctl deploy --config deploy/maintainer/fly.toml --app agent-call --ha=false --remote-only
```

The workflow serializes production deployments, waits up to 10 minutes for
active calls via `/internal/deployment-lock` on the maintainer host, keeps
`--ha=false`, and checks `/healthz`. It needs repository secrets `FLY_API_TOKEN`
and `DEPLOY_GUARD_TOKEN`.

Infrastructure cutover notes remain in [agent_call_migration.md](agent_call_migration.md).
Do not run that cutover while a call is active.

## Release gate and production approval

Merge CI and this deploy workflow both run the same entrypoint,
`bash scripts/verify.sh` (format, lint over `app tests scripts`, strict mypy,
and `pytest --cov=app` with the configured coverage floor). Do not inline a
narrower command list in either workflow: the review baseline (F12) found the
deploy job previously skipped `scripts/` lint and the coverage gate.

Environment protection is **not** stored in this repository. Operators must
confirm in GitHub that the `production` environment declares required
reviewers (or an equivalent manual-approval rule) before relying on
`.github/workflows/fly-deploy.yml` as a human-gated release. A push to `main`
triggers the workflow, so merge and release are otherwise coupled. During a
refactor, prefer merging to a branch that does not auto-deploy, or keep a
release branch/marker; never assume a repo file can prove the approval rule is
configured.

Rollback for the maintainer app still uses the deployment lease and a prior
image, as documented in `AGENTS.md`. Forks use [self-hosting.md](self-hosting.md)
with their own app name.
