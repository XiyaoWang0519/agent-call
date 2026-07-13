from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.security import require_debug_token

router = APIRouter(tags=["debug"], dependencies=[Depends(require_debug_token)])


@router.get("/calls/{call_id}")
async def get_call(call_id: str, request: Request):
    try:
        snapshot = await request.app.state.call_service.get_snapshot(call_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="call not found") from exc
    row = await request.app.state.call_service.db.get_call(call_id)
    transcript = await request.app.state.call_service.db.get_transcript(call_id)
    audit_keys = {
        "openai_accept_status",
        "transcription_verified",
        "semantic_vad_verified",
        "tool_call_count",
        "tool_continuation_observed",
        "interruption_observed",
        "termination_reason",
        "advisory_outcome",
    }
    return {
        **snapshot.model_dump(mode="json"),
        "canary_evidence": {key: row.get(key) for key in audit_keys},
        "transcript": [turn.model_dump(mode="json") for turn in transcript],
    }


@router.get("/calls")
async def list_calls(request: Request):
    rows = await request.app.state.call_service.db.list_calls()
    safe_keys = {
        "call_id",
        "plan_id",
        "state",
        "answered_by",
        "answer_handling",
        "created_at",
        "started_at",
        "answered_at",
        "ended_at",
        "duration_seconds",
        "termination_reason",
    }
    return [{key: row.get(key) for key in safe_keys} for row in rows]
