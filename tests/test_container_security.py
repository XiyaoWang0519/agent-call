from __future__ import annotations

from pathlib import Path


def test_runtime_container_uses_non_root_user():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()

    assert 'ENTRYPOINT ["/usr/local/bin/container-entrypoint"]' in dockerfile
    assert "--uid 10001" in dockerfile
    assert "--gid 10001" in dockerfile


def test_container_repairs_legacy_volume_ownership_before_dropping_privileges():
    entrypoint = (Path(__file__).parents[1] / "scripts" / "container-entrypoint.sh").read_text()

    assert "chown --recursive --no-dereference app:app" in entrypoint
    assert 'exec gosu app "$@"' in entrypoint
