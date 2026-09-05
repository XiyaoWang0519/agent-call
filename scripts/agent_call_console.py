"""Console-script trampoline for `agent-call`.

Editable uv/hatch installs expose `app` through a hidden `.pth` file. Python 3.11+
skips those files when launching console scripts (`safe_path`), so this module is
installed as a real site-packages file and puts the project on `sys.path` first.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_app_on_path() -> None:
    try:
        import app.cli  # noqa: F401
    except ImportError:
        pass
    else:
        return
    here = Path(__file__).resolve()
    for parent in (Path.cwd(), *here.parents):
        if (parent / "app" / "cli.py").is_file() and (parent / "pyproject.toml").is_file():
            root = str(parent)
            if root not in sys.path:
                sys.path.insert(0, root)
            return


def main() -> None:
    _ensure_app_on_path()
    from app.cli import main as cli_main

    raise SystemExit(cli_main())


if __name__ == "__main__":
    main()
