from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _dockerfile_copy_sources(dockerfile: str) -> list[str]:
    sources: list[str] = []
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not line.upper().startswith("COPY "):
            continue
        tokens = line.split()
        if any(token.startswith("--from=") for token in tokens):
            continue
        paths: list[str] = []
        for token in tokens[1:]:
            if token.startswith("--"):
                continue
            paths.append(token)
        if len(paths) < 2:
            continue
        sources.extend(paths[:-1])
    return sources


def _path_is_copied(src: str, copy_sources: list[str]) -> bool:
    src_norm = src.rstrip("/")
    for copied in copy_sources:
        copied_norm = copied.rstrip("/")
        if copied_norm == src_norm:
            return True
        if src_norm.startswith(copied_norm + "/"):
            return True
        if copied_norm.endswith("/") and src_norm.startswith(copied_norm):
            return True
    return False


def test_dockerfile_copies_hatch_force_include_sources():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = (
        pyproject.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("force-include", {})
    )
    assert force_include, "pyproject.toml has no hatch wheel force-include paths to copy"
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copy_sources = _dockerfile_copy_sources(dockerfile)
    missing = [src for src in force_include if not _path_is_copied(str(src), copy_sources)]
    assert missing == [], (
        "Dockerfile must COPY hatch force-include sources before uv sync: " + ", ".join(missing)
    )
    sync_index = dockerfile.find("uv sync")
    assert sync_index != -1
    for src in force_include:
        src_text = str(src)
        copy_index = dockerfile.find(src_text)
        if copy_index == -1:
            parent = str(Path(src_text).parts[0])
            copy_index = dockerfile.find(parent)
        assert 0 <= copy_index < sync_index, f"{src_text} must be copied before uv sync"


def test_runtime_container_uses_non_root_user():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()

    assert 'ENTRYPOINT ["/usr/local/bin/container-entrypoint"]' in dockerfile
    assert "--uid 10001" in dockerfile
    assert "--gid 10001" in dockerfile


def test_container_repairs_legacy_volume_ownership_before_dropping_privileges():
    entrypoint = (Path(__file__).parents[1] / "scripts" / "container-entrypoint.sh").read_text()

    assert "chown --recursive --no-dereference app:app" in entrypoint
    assert 'exec gosu app "$@"' in entrypoint
