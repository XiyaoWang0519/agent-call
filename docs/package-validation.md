# Version 0.1.0 local candidate validation

Validated on 2026-09-07 on macOS arm64. This is a local release candidate; no PyPI upload, GitHub release, production deployment, or additional phone call was performed during packaging.

## Results

- Full repository suite on Python 3.13: **816 passed, 2 skipped; 88.72% app coverage**.
- Ruff formatting and lint, strict mypy, and all pre-commit hooks passed.
- `uv build` built the source distribution, then built the wheel from that source distribution.
- Fresh Python 3.12 environments outside the checkout: `uv tool install` from the wheel and actual `python -m pip install` from the source distribution both passed with independently resolved runtime dependencies.
- Both installed commands passed help, setup help, dummy doctor, evaluation startup, HTTP health, and MCP discovery/plan preparation. Evaluation servers were stopped after validation. No live calls were made.
- The installed wheel completed interactive setup through a real terminal with fixture credentials. Secret input was hidden, configuration was written, and the owner password was stored only as a hash. Unit tests additionally cover non-terminal refusal, hidden-input failure, cancellation, existing files/symlinks, optional Exa, input validation, private permissions, and secret-free output.
- Twine metadata checks passed for both artifacts. Gitleaks found no leaks in the extracted source distribution. Both archives contain 61 entries; the allowlist excludes operational documents, runtime evidence, environment files, recordings, and databases.
- Packaged application files were compared byte-for-byte with the final working tree.

Windows and Linux installations were not exercised. Local package verification does not prove a new user's provider configuration or phone path; see the separate [browser call evidence](browser-clients.md). Public registry name allocation and publishing credentials remain unverified.

## Managed local startup

A second fresh Python 3.12 virtual environment initially contained only pip. Installing the wheel brought in all runtime dependencies. `agent-call start --profile evaluation` then downloaded and verified the pinned official cloudflared helper, acquired an account-free HTTPS address, completed interactive setup with fixture credentials, and passed public health checks. Through that public origin, a protocol client completed OAuth discovery, dynamic registration, PKCE, owner consent, token exchange, all seven MCP tools, and persisted plan preparation. Ctrl-C acquired the idle deployment lease and stopped both owned processes. Restarting with saved configuration and the verified cached helper also passed the public protocol flow and cleanup.

An initial restart hit the former 30-second public-readiness deadline. The launcher now allows 90 seconds and prints a readiness message; the subsequent restart passed. This does not establish an uptime guarantee for temporary tunnels. Two trial-script problems (issuer trailing-slash comparison and undrained terminal output) were corrected during verification; they were not product authentication failures.

The managed trial used fixture provider credentials and never dialed. This verifies the installed local package and public protocol path, not a ChatGPT/Claude browser conversation or a new real phone call. The standalone manual `setup`/`serve` workflow remains available.

## Artifact checksums

```text
874a56dd7573445ce6440290c49b8eeb6126f71c19cfa15fd45d1422275d6450  agent_call-0.1.0-py3-none-any.whl
679d4fcad0ec633c7ee92372459829ab99c9b63149f885d92b6d9974f34442e5  agent_call-0.1.0.tar.gz
```

Artifacts and a machine-readable local verification record are in the ignored `dist/` directory. Rebuilding can change archive hashes; validate and record the exact files intended for publication. Follow [package release](package-release.md) for installation and publication preparation.
