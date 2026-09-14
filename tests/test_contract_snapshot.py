"""R01: frozen external contracts.

These snapshots make an accidental rename or removal of an MCP tool, HTTP route,
or tool error code fail loudly. They are intentionally explicit: a genuine change
requires editing this file, which is the point.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastmcp import FastMCP
from pydantic import ValidationError

from app.evaluation import LIVE_CALLS_DISABLED_CODE
from app.main import create_app
from app.mcp_tools import register_tools
from app.models import AnswerCallQuestionRequest
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = {
    "prepare_phone_call": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": False,
        "idempotentHint": False,
    },
    "start_phone_call": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
        "idempotentHint": False,
    },
    "get_call_result": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
        "idempotentHint": False,
    },
    "end_phone_call": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": True,
        "idempotentHint": True,
    },
    "get_phone_call": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": False,
        "idempotentHint": True,
    },
    "wait_for_call_event": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": False,
        "idempotentHint": True,
    },
    "answer_call_question": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": False,
        "idempotentHint": False,
    },
}

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

# Tool-facing error codes emitted as {"code": ...}. Kept alongside the
# unstructured ToolError("unknown question")/ToolError(str(exc)) forms that R06
# will converge later.
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
}


def test_mcp_tool_names_and_annotations_are_frozen() -> None:
    async def collect() -> dict[str, dict[str, object]]:
        mcp = FastMCP("contract-snapshot")
        register_tools(mcp, lambda: None)
        tools = await mcp.list_tools()
        return {
            tool.name: {
                key: value for key, value in dict(tool.annotations or {}).items() if key != "title"
            }
            for tool in tools
        }

    assert asyncio.run(collect()) == EXPECTED_TOOLS


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
    code_pattern = re.compile(r'"code":\s*"([a-z_]+)"')
    found: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        found.update(code_pattern.findall(path.read_text(encoding="utf-8")))
    found.add(LIVE_CALLS_DISABLED_CODE)
    assert found == EXPECTED_ERROR_CODES


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
