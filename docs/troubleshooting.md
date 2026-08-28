# Troubleshooting

Value-free diagnostics first: `uv run agent-call doctor --dummy`,
`uv run agent-call doctor --prepare-only`, or
`uv run agent-call doctor --live-ready`. Doctor never prints secrets, full
credentials, or full E.164 numbers.

## Dummy boot does not reach `/healthz`

- Confirm the process is on loopback: `uv run agent-call serve --profile evaluation --host 127.0.0.1 --port 8000`.
- Evaluation refuses a non-loopback bind unless CLI `--unsafe-bind` is set (the `AGENT_CALL_UNSAFE_BIND` env var is not honored).
- Bare `agent-call serve` with core credentials in `.env.local` or the process environment and no `AGENT_CALL_PROFILE` is refused; pass `--profile live` (this will place billable calls) or `--profile evaluation`.
- Compose publishes `127.0.0.1:8000` only. `curl http://127.0.0.1:8000/healthz` from the same machine.
- Lifespan abort: every live-profile boot needs `Settings.require_runtime_configuration`. Evaluation fills dummy values; live does not.
- Do not set `ALLOWED_COUNTRY_CODES` in `.env` / `.env.local`. A bare `+1` crashes startup.

Expected health body: `{"status":"ok"}`.

`uv run python -m app ...` is equivalent to `uv run agent-call`.

## `doctor --live-ready` fails

Run this only after the live-profile server is up and `PUBLIC_BASE_URL` is a
reachable HTTPS origin (tunnel or your own deployment).

- `missing` / `blank`: set the named variable; the tool will not echo the value.
- `PUBLIC_BASE_URL_format`: must be an HTTPS origin with no path or query.
- `PUBLIC_BASE_URL_reachability`: DNS/TLS failed, `/healthz` was not 200, or
  `/webhooks/openai` returned 404.
- `DATABASE_URL`: an existing SQLite file gets a transactional write probe that
  rolls back and leaves the schema unchanged. A missing file or a zero-byte
  file is treated as a new location: doctor writes a temporary probe file in
  the parent directory and deletes it. A missing parent directory is a
  conservative failure; doctor does not create `DATABASE_URL` parents.
  Symlinks, directories, and non-SQLite files fail. Details never include
  the path.
- `TWILIO_ACCOUNT` / `OPENAI_API`: metadata/auth check failed. Those probes do
  not place calls.
- `EXA_API_KEY` / `OPENAI_WEBHOOK_SECRET` stay `UNVERIFIED` on purpose (no
  side-effect-free probe). That is not a configuration error; the command still
  exits 1 and does not claim complete live readiness.
- `*_format` on phones: E.164 (`+` and digits only).
- `caller_owner_distinct`: `TWILIO_CALLER_ID` must not equal `OWNER_PHONE_E164`.
- `profile` failure: `AGENT_CALL_PROFILE=evaluation` disables live calls. Unset it or set `live`.

## `start_phone_call` returns `live_calls_disabled`

The process is on the evaluation profile. That is expected for dummy Compose and
`agent-call serve --profile evaluation`. `prepare_phone_call` still works.
Live origination requires `AGENT_CALL_PROFILE=live` (the Settings default) and
real provider credentials.

## MCP `401` / `403`

`/mcp/` needs **both** `Authorization: Bearer <MCP_BEARER_TOKEN>` and
`X-Agent-User-Id: <ALLOWED_AGENT_USER_ID>`. Missing either credential is
rejected. Dummy bearer/user are filled only in evaluation mode; live mode uses
your `.env.local` values (doctor reports presence, not the values).

## Compose container restarts

- Image entrypoint drops to UID 10001 after repairing `/data` ownership.
- SQLite lives on the `agent_call_data` named volume. A compose restart should
  keep prepared plans. If `/data` is not writable, check the volume mount.

## Prepared plan missing after restart

- Source dummy boot without a persistent `DATABASE_URL` uses `./agent_call.db` in
  the working directory. Compose uses `sqlite:////data/agent_call.db` on the
  named volume.
- Evaluation `start_phone_call` does not consume the plan; it remains `prepared`.

## Fork deploy aimed at the wrong Fly app

The committed `fly.toml` uses `YOUR_FLY_APP_NAME`. Replace it with an app you
own. Do not copy `deploy/maintainer/`. See [self-hosting.md](self-hosting.md).
