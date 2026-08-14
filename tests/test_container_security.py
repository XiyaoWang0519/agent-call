from __future__ import annotations

from pathlib import Path


def test_runtime_container_uses_non_root_user():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()

    assert "USER app" in dockerfile
