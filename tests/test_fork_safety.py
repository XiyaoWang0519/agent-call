from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# User-facing golden paths and default deploy templates. Maintainer overlay,
# migration notes, and AGENTS.md may still name the production app when clearly
# labeled maintainer-only. The GitHub deploy workflow is inspected separately.
GOLDEN_PATH_FILES = (
    ROOT / "README.md",
    ROOT / "docs" / "self-hosting.md",
    ROOT / "docs" / "troubleshooting.md",
    ROOT / "fly.toml",
    ROOT / "compose.yaml",
    ROOT / ".env.example",
    ROOT / "scripts" / "dev_server.sh",
    ROOT / "scripts" / "live_smoke.sh",
    ROOT / "render.yaml",
)

FORBIDDEN = (
    re.compile(r"agent-call\.fly\.dev"),
    re.compile(r"app\s*=\s*['\"]agent-call['\"]"),
    re.compile(r"flyctl\s+deploy[^\n]*--app\s+agent-call"),
    re.compile(r"flyctl\s+deploy[^\n]*-a\s+agent-call"),
    re.compile(r"https://agent-call\.fly\.dev"),
)

REQUIRED_COMMANDS = (
    "uv run agent-call doctor --dummy",
    "uv run agent-call doctor --prepare-only",
    "uv run agent-call doctor --live-ready",
    "uv run agent-call smoke-prepare",
    "docker compose up",
)


def test_golden_paths_do_not_target_maintainer_fly_app():
    hits: list[str] = []
    for path in GOLDEN_PATH_FILES:
        assert path.is_file(), f"missing golden-path file {path.relative_to(ROOT)}"
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            if pattern.search(text):
                hits.append(f"{path.relative_to(ROOT)}: {pattern.pattern}")
    assert hits == []


def test_user_fly_template_is_not_maintainer_app():
    text = (ROOT / "fly.toml").read_text(encoding="utf-8")
    assert "YOUR_FLY_APP_NAME" in text
    assert "agent-call.fly.dev" not in text
    maintainer = (ROOT / "deploy" / "maintainer" / "fly.toml").read_text(encoding="utf-8")
    assert "upstream-maintainer-only" in maintainer.lower()
    assert "app = 'agent-call'" in maintainer


_JOB_GUARD = "github.repository == 'XiyaoWang0519/agent-call'"
_SECRET_BEARING = (
    "secrets.DEPLOY_GUARD_TOKEN",
    "secrets.FLY_API_TOKEN",
    "agent-call.fly.dev",
    "flyctl deploy",
    "/internal/deployment-lock",
)


def test_fly_deploy_workflow_is_upstream_maintainer_only():
    path = ROOT / ".github" / "workflows" / "fly-deploy.yml"
    text = path.read_text(encoding="utf-8")
    assert "upstream-maintainer-only" in text.lower()
    assert re.search(
        r"(?m)^    if: github\.repository == 'XiyaoWang0519/agent-call'\s*$",
        text,
    ), "job-level guard must restrict the deploy job to the upstream repository"
    assert text.count("jobs:") == 1
    assert text.count("\n    steps:") == 1
    guard_at = text.index(_JOB_GUARD)
    steps_at = text.index("\n    steps:")
    assert guard_at < steps_at
    for needle in _SECRET_BEARING:
        assert needle in text, needle
        assert text.index(needle) > guard_at, f"{needle} is not covered by the job guard"


def test_golden_path_docs_name_required_commands():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    self_hosting = (ROOT / "docs" / "self-hosting.md").read_text(encoding="utf-8")
    combined = readme + "\n" + self_hosting
    missing = [command for command in REQUIRED_COMMANDS if command not in combined]
    assert missing == []


def test_compose_is_loopback_dummy_and_non_root():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "127.0.0.1:8000:8000" in compose
    assert "AGENT_CALL_PROFILE: evaluation" in compose
    assert "healthcheck:" in compose
    assert "agent_call_data:/data" in compose
    assert "OPENAI_API_KEY" not in compose
    assert "TWILIO_AUTH_TOKEN" not in compose
    assert "privileged: true" not in compose
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--uid 10001" in dockerfile
