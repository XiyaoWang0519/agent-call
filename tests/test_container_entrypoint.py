from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app import container_entrypoint
from app.container_entrypoint import database_path, prepare_database_files


def test_database_path_resolves_relative_sqlite_url() -> None:
    assert database_path("sqlite:///./poke_call.db", working_directory=Path("/app")) == Path(
        "/app/poke_call.db"
    )


def test_database_path_preserves_absolute_sqlite_url() -> None:
    assert database_path("sqlite:////data/poke_call.db", working_directory=Path("/app")) == Path(
        "/data/poke_call.db"
    )


def test_database_path_ignores_unsupported_database_url() -> None:
    assert database_path("postgresql://example.invalid/db", working_directory=Path("/app")) is None


def test_prepare_database_files_refuses_filesystem_root() -> None:
    with pytest.raises(RuntimeError, match="filesystem root"):
        prepare_database_files(Path("/poke_call.db"))


def test_prepare_database_files_creates_and_reowns_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ownership_changes: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(
        container_entrypoint.os,
        "chown",
        lambda path, uid, gid: ownership_changes.append((Path(path), uid, gid)),
    )

    database = tmp_path / "data" / "poke_call.db"
    prepare_database_files(database)

    assert database.parent.is_dir()
    assert ownership_changes == [
        (
            database.parent,
            container_entrypoint.RUNTIME_UID,
            container_entrypoint.RUNTIME_GID,
        )
    ]


def test_drop_root_privileges_prepares_database_then_drops_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Any]] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./poke_call.db")
    monkeypatch.setattr(container_entrypoint.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        container_entrypoint,
        "prepare_database_files",
        lambda path: calls.append(("prepare", path)),
    )
    monkeypatch.setattr(
        container_entrypoint.os, "setgroups", lambda groups: calls.append(("groups", groups))
    )
    monkeypatch.setattr(container_entrypoint.os, "setgid", lambda gid: calls.append(("gid", gid)))
    monkeypatch.setattr(container_entrypoint.os, "setuid", lambda uid: calls.append(("uid", uid)))

    container_entrypoint.drop_root_privileges()

    assert calls == [
        ("prepare", tmp_path / "poke_call.db"),
        ("groups", []),
        ("gid", container_entrypoint.RUNTIME_GID),
        ("uid", container_entrypoint.RUNTIME_UID),
    ]


def test_drop_root_privileges_is_noop_for_unprivileged_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(container_entrypoint.os, "geteuid", lambda: 10001)
    monkeypatch.setattr(
        container_entrypoint,
        "prepare_database_files",
        lambda path: pytest.fail(f"unexpected database preparation: {path}"),
    )

    container_entrypoint.drop_root_privileges()
