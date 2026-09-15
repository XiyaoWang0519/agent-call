#!/usr/bin/env bash
# Shared verification entrypoint for both merge CI and the maintainer deploy job.
#
# Every gate lives here on purpose: the deploy workflow must not drift from CI
# (missing lint scope, missing coverage floor, extra unversioned steps). Run it
# from a clean, credential-free environment. It never places a phone call,
# contacts a provider, or touches production state.
#
# Usage: bash scripts/verify.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv run ruff format --check app tests scripts
uv run ruff check app tests scripts
uv run mypy app
uv run pytest -q --cov=app
