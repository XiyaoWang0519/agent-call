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

Rollback for the maintainer app still uses the deployment lease and a prior
image, as documented in `AGENTS.md`. Forks use [self-hosting.md](self-hosting.md)
with their own app name.
