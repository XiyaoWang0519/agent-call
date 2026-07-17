from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from os import environ
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastmcp import FastMCP
from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass
class Probe:
    probe_id: str
    question: str
    delay_seconds: float
    started_at: float
    wait_entered_at: float | None = None
    question_returned_at: float | None = None
    answer_received_at: float | None = None
    answer: str | None = None


PROBES: dict[str, Probe] = {}
BEARER_TOKEN = environ["PROBE_BEARER_TOKEN"]
mcp = FastMCP("Ask Poke No-Call Probe")


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"authorization", b"").decode("latin-1")
        if secrets.compare_digest(supplied, f"Bearer {BEARER_TOKEN}"):
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"detail":"unauthorized"}'})


def require_probe(probe_id: str) -> Probe:
    probe = PROBES.get(probe_id)
    if probe is None:
        raise ValueError("unknown probe_id")
    return probe


def timing(probe: Probe) -> dict[str, float | None]:
    def elapsed(end: float | None, start: float | None) -> float | None:
        if end is None or start is None:
            return None
        return round(end - start, 3)

    return {
        "start_to_wait_seconds": elapsed(probe.wait_entered_at, probe.started_at),
        "wait_to_question_seconds": elapsed(probe.question_returned_at, probe.wait_entered_at),
        "poke_answer_seconds": elapsed(probe.answer_received_at, probe.question_returned_at),
        "total_seconds": elapsed(probe.answer_received_at, probe.started_at),
    }


@mcp.tool(
    name="start_ask_poke_probe",
    description=(
        "Start a no-phone-call test of Poke's ability to remain in an MCP tool chain, long-poll "
        "for a question, and submit an answer. Always follow the returned next_action."
    ),
)
async def start_ask_poke_probe(question: str, delay_seconds: float = 5.0) -> dict:
    if not question.strip() or len(question) > 500:
        raise ValueError("question must contain 1-500 characters")
    if not 1 <= delay_seconds <= 25:
        raise ValueError("delay_seconds must be between 1 and 25")
    probe_id = uuid4().hex
    PROBES[probe_id] = Probe(
        probe_id=probe_id,
        question=" ".join(question.split()),
        delay_seconds=delay_seconds,
        started_at=time.time(),
    )
    return {
        "status": "started",
        "probe_id": probe_id,
        "next_action": (
            "Immediately call wait_for_ask_poke_probe with this probe_id and "
            "timeout_seconds=20. If it returns status=idle, call it again. When it returns a "
            "question, answer it using submit_ask_poke_probe_answer. Do not answer only in chat."
        ),
    }


@mcp.tool(
    name="wait_for_ask_poke_probe",
    description=(
        "Long-poll for a no-call probe question. On status=idle, call this tool again. On "
        "status=question, use submit_ask_poke_probe_answer with the same probe_id."
    ),
)
async def wait_for_ask_poke_probe(probe_id: str, timeout_seconds: float = 20.0) -> dict:
    probe = require_probe(probe_id)
    if probe.answer_received_at is not None:
        return {"status": "answered", "probe_id": probe_id, "timing": timing(probe)}
    if probe.wait_entered_at is None:
        probe.wait_entered_at = time.time()
    timeout_seconds = min(max(timeout_seconds, 0.0), 25.0)
    ready_at = probe.started_at + probe.delay_seconds
    remaining = max(0.0, ready_at - time.time())
    await asyncio.sleep(min(remaining, timeout_seconds))
    if time.time() < ready_at:
        return {
            "status": "idle",
            "probe_id": probe_id,
            "next_action": "Call wait_for_ask_poke_probe again with the same probe_id.",
        }
    if probe.question_returned_at is None:
        probe.question_returned_at = time.time()
    return {
        "status": "question",
        "probe_id": probe_id,
        "question": probe.question,
        "next_action": (
            "Answer this question now using submit_ask_poke_probe_answer. Pass the same probe_id "
            "and your answer. Do not answer only in chat."
        ),
    }


@mcp.tool(
    name="submit_ask_poke_probe_answer",
    description="Submit Poke's answer to a no-call probe and return exact end-to-end timing.",
)
async def submit_ask_poke_probe_answer(probe_id: str, answer: str) -> dict:
    probe = require_probe(probe_id)
    if probe.question_returned_at is None:
        raise ValueError("question has not been returned to Poke yet")
    if not answer.strip() or len(answer) > 4096:
        raise ValueError("answer must contain 1-4096 characters")
    if probe.answer_received_at is None:
        probe.answer_received_at = time.time()
        probe.answer = " ".join(answer.split())
    return {
        "status": "completed",
        "probe_id": probe_id,
        "answer": probe.answer,
        "timing": timing(probe),
    }


@mcp.tool(name="get_ask_poke_probe_result")
async def get_ask_poke_probe_result(probe_id: str) -> dict:
    probe = require_probe(probe_id)
    return {"probe": asdict(probe), "timing": timing(probe)}


mcp_http_app = mcp.http_app(
    path="/",
    transport="streamable-http",
    stateless_http=True,
    json_response=True,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with mcp_http_app.lifespan(_):
        yield


app = FastAPI(lifespan=lifespan)
app.mount("/mcp", BearerAuthMiddleware(mcp_http_app))


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/probe-results")
async def probe_results(request: Request) -> dict:
    supplied = request.headers.get("authorization", "")
    if not secrets.compare_digest(supplied, f"Bearer {BEARER_TOKEN}"):
        raise HTTPException(status_code=401, detail="unauthorized")
    return {
        "probes": [{"probe": asdict(probe), "timing": timing(probe)} for probe in PROBES.values()]
    }
