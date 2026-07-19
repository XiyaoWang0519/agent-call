from __future__ import annotations

import os
import sys
from pathlib import Path

RUNTIME_UID = 10001
RUNTIME_GID = 10001
SQLITE_PREFIX = "sqlite:///"


def database_path(database_url: str, *, working_directory: Path) -> Path | None:
    """Resolve the configured SQLite file without importing application settings."""
    if not database_url.startswith(SQLITE_PREFIX):
        return None
    path = Path(database_url[len(SQLITE_PREFIX) :])
    return path if path.is_absolute() else working_directory / path


def prepare_database_files(path: Path) -> None:
    """Make the mounted SQLite location writable by the unprivileged runtime user."""
    parent = path.parent.resolve()
    if parent == Path("/"):
        raise RuntimeError("refusing to change ownership of the filesystem root")
    parent.mkdir(parents=True, exist_ok=True)
    candidates = (parent, path, Path(f"{path}-shm"), Path(f"{path}-wal"))
    for candidate in candidates:
        if candidate.exists():
            if candidate.is_symlink():
                raise RuntimeError(f"refusing to change ownership through symlink: {candidate}")
            os.chown(candidate, RUNTIME_UID, RUNTIME_GID)


def drop_root_privileges() -> None:
    """Drop the root identity retained only to prepare a mounted data volume."""
    if os.geteuid() != 0:
        return
    path = database_path(
        os.environ.get("DATABASE_URL", "sqlite:///./poke_call.db"),
        working_directory=Path.cwd(),
    )
    if path is not None:
        prepare_database_files(path)
    os.setgroups([])
    os.setgid(RUNTIME_GID)
    os.setuid(RUNTIME_UID)


def main() -> None:
    drop_root_privileges()
    command = sys.argv[1:] or [
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        os.environ.get("PORT", "8000"),
        "--proxy-headers",
        "--forwarded-allow-ips=*",
    ]
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
