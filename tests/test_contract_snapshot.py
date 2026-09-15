"""R01: frozen external contracts.

These snapshots make an accidental rename, route change, schema change, or tool
error-code removal fail loudly. The MCP tool schemas are stored in
``tests/snapshots/mcp_tools.json`` so the full input/output schema is compared,
not just the tool name and annotations. A genuine change requires regenerating
the snapshot, which is the point.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from app.errors import ERROR_MESSAGES, ErrorCode
from app.main import create_app
from app.mcp_tools import register_tools
from app.models import AnswerCallQuestionRequest
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
TOOL_SNAPSHOT = ROOT / "tests" / "snapshots" / "mcp_tools.json"

EXPECTED_HTTP_ROUTES = {
    "/webhooks/openai",
    "/webhooks/twilio/media/{call_id}/{plan_id}",
    "/webhooks/twilio/amd",
    "/webhooks/twilio/conference",
    "/webhooks/twilio/participant-status",
    "/webhooks/twilio/announce-dtmf",
    "/diagnostics/live-test",
    "/calls/{call_id}",
    "/calls",
    "/healthz",
    "/readyz",
    "/mcp",
    "/internal/deployment-lock",
}

_DOCS_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

EXPECTED_ERROR_CODES = {
    "call_busy",
    "call_ending",
    "call_not_found",
    "confirmation_mismatch",
    "confirmation_required",
    "deployment_in_progress",
    "invalid_answer_submission",
    "invalid_call_state",
    "live_calls_disabled",
    "plan_not_found",
    "plan_unavailable",
    "supervision_unavailable",
    "unknown_question",
}

# A lifecycle transition must go through a named promote/claim/finish method. The
# generic updater still accepts `state` for historical fixtures, so this scan is what
# keeps new production code from growing another anonymous state write.
_RAW_STATE_UPDATE = re.compile(r"update_call\([^)]*\bstate\s*=")


async def _registered_tools(get_service=None):
    mcp = FastMCP("contract-snapshot")
    register_tools(mcp, get_service or (lambda: None))
    return await mcp.list_tools()


def _tool_snapshot(tools) -> dict[str, dict]:
    return {
        tool.name: {
            "annotations": {
                key: value for key, value in dict(tool.annotations or {}).items() if key != "title"
            },
            "input_schema": tool.parameters,
            "output_schema": tool.output_schema,
        }
        for tool in sorted(tools, key=lambda item: item.name)
    }


def test_mcp_tool_schemas_are_frozen() -> None:
    current = _tool_snapshot(asyncio.run(_registered_tools()))
    expected = json.loads(TOOL_SNAPSHOT.read_text(encoding="utf-8"))
    assert current == expected


def test_tool_errors_carry_stable_codes_for_missing_calls() -> None:
    class MissingService:
        async def get_result(self, call_id: str):
            raise LookupError(call_id)

        async def get_snapshot(self, call_id: str):
            raise LookupError(call_id)

    tools = {tool.name: tool for tool in asyncio.run(_registered_tools(lambda: MissingService()))}

    async def invoke(name: str):
        return await tools[name].fn(call_id="call_missing")

    for name in ("get_call_result", "get_phone_call"):
        with pytest.raises(ToolError) as exc_info:
            asyncio.run(invoke(name))
        assert json.loads(str(exc_info.value))["code"] == "call_not_found"


def _all_route_paths(app) -> set[str]:
    paths: set[str] = set()
    for route in app.routes:
        original = getattr(route, "original_router", None)
        candidates = original.routes if original is not None else [route]
        for candidate in candidates:
            path = getattr(candidate, "path", None)
            if isinstance(path, str) and path not in _DOCS_PATHS:
                paths.add(path)
    return paths


def test_http_route_paths_are_frozen(settings: Settings) -> None:
    app = create_app(settings)
    assert _all_route_paths(app) == EXPECTED_HTTP_ROUTES


def test_tool_error_codes_are_frozen() -> None:
    # The registry is the contract: adding or renaming a code means editing both the
    # enum and this set, which is the point.
    assert {code.value for code in ErrorCode} == EXPECTED_ERROR_CODES
    assert set(ERROR_MESSAGES) == set(ErrorCode)


def test_no_adhoc_error_code_literals_outside_the_registry() -> None:
    code_pattern = re.compile(r'"code":\s*"([a-z_]+)"')
    found: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        if path.name == "errors.py":
            continue
        found.update(code_pattern.findall(path.read_text(encoding="utf-8")))
    assert found == set()


def test_app_code_never_writes_call_state_through_generic_update() -> None:
    assert [
        path
        for path in (ROOT / "app").rglob("*.py")
        if _RAW_STATE_UPDATE.search(path.read_text(encoding="utf-8"))
    ] == []


def test_answer_source_declarations_are_required_by_the_request_model() -> None:
    # F06: resolution/sources_checked are validated at the MCP boundary. This
    # pins the not_found attestation rule independently of the core call path.
    with pytest.raises(ValidationError, match="conversation_history"):
        AnswerCallQuestionRequest(
            call_id="call_x",
            question_id="q_1",
            answer="I could not find it",
            resolution="not_found",
            sources_checked=["agent_memory"],  # type: ignore[list-item]
        )
