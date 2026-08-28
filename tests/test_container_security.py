from __future__ import annotations

import re
import tomllib
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_SECRET_CONFIG_NAMES = frozenset({".env", ".env.local"})
_CONTEXT_EXCLUDED_PATHS = (
    ".git",
    ".git/objects/pack/pack-deadbeef.pack",
    ".git/refs/heads/main",
    ".env",
    ".env.local",
    ".env.production",
    ".venv",
    ".venv/lib/python3.13/site-packages/foo.py",
    "__pycache__/foo.pyc",
    "app/__pycache__/main.cpython-313.pyc",
    ".pytest_cache/v/cache/nodeids",
    ".mypy_cache/3.13/foo.data.json",
    ".ruff_cache/CACHEDIR.TAG",
    ".coverage",
    "htmlcov/index.html",
    "agent_call.db",
    "data/agent_call.db",
    "app/agent_call.db",
    "scripts/live_smoke.sh",
    "docs/implementation/evidence/implementation-record.txt",
    "tests/test_container_security.py",
)
_CONTEXT_INCLUDED_PATHS = (
    "Dockerfile",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "app",
    "app/main.py",
    "app/db/engine.py",
    "scripts/agent_call_console.py",
    "scripts/container-entrypoint.sh",
)


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


def _normalize_context_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _docker_glob_to_regex(pattern: str) -> re.Pattern[str]:
    pattern = pattern.replace("\\", "/")
    if pattern.startswith("/"):
        pattern = pattern[1:]
    pattern = pattern.rstrip("/")
    out: list[str] = ["^"]
    index = 0
    length = len(pattern)
    while index < length:
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
            continue
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        char = pattern[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            close = pattern.find("]", index + 1)
            if close == -1:
                out.append(re.escape(char))
            else:
                out.append(pattern[index : close + 1])
                index = close
        else:
            out.append(re.escape(char))
        index += 1
    out.append("$")
    return re.compile("".join(out))


def _parse_dockerignore(text: str) -> list[tuple[bool, re.Pattern[str]]]:
    rules: list[tuple[bool, re.Pattern[str]]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ignore = True
        if line.startswith("!"):
            ignore = False
            line = line[1:].strip()
        if not line:
            continue
        rules.append((ignore, _docker_glob_to_regex(line)))
    return rules


@lru_cache(maxsize=1)
def _dockerignore_rules() -> list[tuple[bool, re.Pattern[str]]]:
    path = ROOT / ".dockerignore"
    assert path.is_file(), ".dockerignore must exist"
    return _parse_dockerignore(path.read_text(encoding="utf-8"))


def _dockerignore_excludes(rel_path: str) -> bool:
    rel_path = _normalize_context_path(rel_path)
    parts = [part for part in rel_path.split("/") if part]
    for length in range(1, len(parts) + 1):
        prefix = "/".join(parts[:length])
        ignored = False
        for ignore, regex in _dockerignore_rules():
            if regex.fullmatch(prefix):
                ignored = ignore
        if ignored:
            return True
    return False


def _is_secret_config_path(rel_path: str) -> bool:
    name = Path(_normalize_context_path(rel_path)).name
    return name in _SECRET_CONFIG_NAMES or name.startswith(".env.")


def test_dockerignore_exists():
    assert (ROOT / ".dockerignore").is_file()


def test_dockerfile_uses_explicit_copy_not_entire_context():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not line.upper().startswith("COPY "):
            continue
        tokens = line.split()
        if any(token.startswith("--from=") for token in tokens):
            continue
        paths = [token for token in tokens[1:] if not token.startswith("--")]
        assert len(paths) >= 2, line
        sources = paths[:-1]
        assert sources, line
        for source in sources:
            assert source not in {".", "./", "*"}, (
                f"Dockerfile must use explicit COPY sources, not the entire context: {line}"
            )


def test_dockerignore_keeps_local_dockerfile_copy_sources():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copy_sources = _dockerfile_copy_sources(dockerfile)
    assert copy_sources, "Dockerfile has no local COPY sources"
    missing = [src for src in copy_sources if _dockerignore_excludes(src)]
    assert missing == [], missing
    assert not _dockerignore_excludes("app/main.py")


def test_dockerignore_excludes_git_secrets_caches_and_databases():
    included = [path for path in _CONTEXT_INCLUDED_PATHS if _dockerignore_excludes(path)]
    excluded = [path for path in _CONTEXT_EXCLUDED_PATHS if not _dockerignore_excludes(path)]
    assert included == [], included
    assert excluded == [], excluded


def test_runtime_image_copy_skips_secret_bearing_config():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copy_sources = _dockerfile_copy_sources(dockerfile)
    copied_secrets = [src for src in copy_sources if _is_secret_config_path(src)]
    assert copied_secrets == [], copied_secrets
    secret_paths = (
        ".env",
        ".env.local",
        ".env.production",
        "app/.env",
        "app/.env.local",
        ".env.example",
    )
    leaked = [path for path in secret_paths if not _dockerignore_excludes(path)]
    assert leaked == [], leaked
