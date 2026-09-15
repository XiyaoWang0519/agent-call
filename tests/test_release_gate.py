"""Static guards for the shared merge/release verification gate (R00).

These tests are intentionally cheap and offline: they only read workflow and
script text. They exist so the deploy job cannot quietly run a weaker check set
than merge CI (for example dropping the ``scripts/`` lint scope or the coverage
floor), which the original review flagged as F12.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

VERIFY_SCRIPT = ROOT / "scripts" / "verify.sh"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "fly-deploy.yml"

REQUIRED_GATES = (
    "ruff format --check app tests scripts",
    "ruff check app tests scripts",
    "mypy app",
    "pytest -q --cov=app",
)


def test_verify_script_runs_every_required_gate() -> None:
    text = VERIFY_SCRIPT.read_text(encoding="utf-8")
    for gate in REQUIRED_GATES:
        assert gate in text, f"scripts/verify.sh is missing gate: {gate}"


def test_both_workflows_call_the_shared_gate() -> None:
    for workflow in (CI_WORKFLOW, DEPLOY_WORKFLOW):
        text = workflow.read_text(encoding="utf-8")
        assert "scripts/verify.sh" in text, (
            f"{workflow.name} must call the shared verification entrypoint"
        )
        # The old inline variant must not linger alongside the shared call.
        assert "ruff format --check app tests\n" not in text
        assert "pytest -q --cov=app" not in text
