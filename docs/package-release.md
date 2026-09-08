# Package release

Version `0.1.0` is the first wheel and source-distribution release. The artifacts are attached to the [GitHub release](https://github.com/XiyaoWang0519/agent-call/releases/tag/v0.1.0); it is not published to PyPI. The instructions below install release or locally built artifacts; installation does not prove a working phone call.

## Build the candidate

From the repository checkout, with Python 3.12+ and uv installed:

```bash
uv sync --all-groups --frozen
uv build
```

The expected artifacts are `dist/agent_call-0.1.0-py3-none-any.whl` and `dist/agent_call-0.1.0.tar.gz`. Resolve the wheel to an absolute path before changing directories.

## Install with uv

Install the wheel as an isolated tool:

```bash
uv tool install /absolute/path/to/agent_call-0.1.0-py3-none-any.whl
```

Replace the example path with the actual built wheel. If the executable is not on your shell's PATH, run `uv tool update-shell` and open a new terminal. To replace an earlier local candidate, use `uv tool install --reinstall` with the new wheel's absolute path.

## Install with pip

Use a dedicated virtual environment with Python 3.12 or newer:

```bash
python3.12 -m venv ~/.venvs/agent-call
source ~/.venvs/agent-call/bin/activate
python -m pip install /absolute/path/to/agent_call-0.1.0-py3-none-any.whl
```

Keep that virtual environment active in every terminal where you run `agent-call`. This installation does not require a source checkout at runtime.

## Verify the installed artifact locally

Use a new working directory outside the checkout so imports cannot accidentally come from source:

```bash
mkdir agent-call-home
cd agent-call-home
agent-call --help
agent-call setup --help
agent-call start --help
agent-call doctor --dummy
agent-call serve --profile evaluation --host 127.0.0.1 --port 8000
```

In another terminal, activate the virtual environment if using pip, change to the same `agent-call-home` directory, and run:

```bash
curl -fsS http://127.0.0.1:8000/healthz
agent-call smoke-prepare
```

Expect a healthy server and `OK prepare-only smoke`. Evaluation must refuse call starts with `live_calls_disabled`. These checks exercise the installed CLI, app startup, MCP discovery, and plan preparation without making a call.

Stop the evaluation server before running `agent-call start` for automatic local tunneling and guided configuration. It uses `~/.agent-call` by default. The first helper download requires GitHub HTTPS access; later starts verify and reuse the private cached helper. Alternatively, use `agent-call setup` with a separately managed HTTPS origin. Setup writes `.env.local` in the current directory, generates independent service/OAuth secrets, hashes the chosen owner login password, and enables browser OAuth by default. It refuses to overwrite existing configuration. Continue with the [real-call setup instructions](../README.md#2-configure-real-calls). The current directory remains the configuration and data directory for manual `setup`, `doctor`, and `serve`. Managed `start` instead uses `~/.agent-call` or its explicit `--directory`.

For candidate validation, repeat the isolated installation with the source distribution as well. Check the wheel and source-distribution file lists for accidental credentials, transcripts, databases, recordings, or local evidence. Verify that setup produces a private configuration file, does not echo secrets, and leaves an existing file untouched. Record actual outcomes and failures; the commands in this guide are a checklist, not a claim that every check passed.

## Publish a future registry release

1. Set the intended version in `pyproject.toml`, update the changelog and release notes, and rebuild both artifacts from the reviewed revision.
2. Run the repository checks: `uv run ruff format --check app tests scripts`, `uv run ruff check app tests scripts`, `uv run mypy app`, and `uv run pytest --cov=app`.
3. Validate package metadata with `uvx twine check dist/agent_call-0.1.0*`, inspect artifact contents, and repeat the isolated wheel and source-distribution checks above. Use the chosen version in these paths if it changes.
4. Confirm the package name is available on the intended registry and configure trusted publishing or a scoped publishing credential. Record the exact commit and artifact checksums for the release.
5. Publish only after release approval, then verify the uploaded files and install the exact version from the registry in a fresh environment. Do not treat a local build, tag, or upload attempt as proof of a published release.
6. Update the README installation instructions only after the registry installation succeeds. Preserve the documented browser compatibility and phone-validation limits.

After a verified future PyPI publication, the intended installation commands will be `uv tool install agent-call==0.1.0` or `python -m pip install agent-call==0.1.0` inside a virtual environment. **These are future registry instructions, not currently available installation paths.** No publishing action is performed by this guide.

Package validation does not establish audible response latency, real interruptions, complete playback, post-goodbye follow-ups, or safe phone termination. Voice changes also need the evidence described in the [live-phone runbook](live-phone-runbook.md) and [audio validation guide](call-audio-validation.md), with unresolved latency and incomplete scenarios disclosed.
