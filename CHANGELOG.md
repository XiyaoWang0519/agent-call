# Changelog

Notable changes to this project are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This repository has **no tagged GitHub release** yet. Package metadata in
`pyproject.toml` currently reports `0.1.0` as a development version, not as a
published release.

## Unreleased

### Changed

- Pre-commit Gitleaks now scans the working tree (`--no-git`) so local extra
  branches do not fail the hook. GitHub Actions still runs a full-history
  Gitleaks scan.
