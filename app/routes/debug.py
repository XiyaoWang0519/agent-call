from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.security import require_debug_token

router = APIRouter(tags=["debug"], dependencies=[Depends(require_debug_token)])

# Exposure policy for GET /calls/{call_id}: only these call-row fields are safe to
# surface in the debug "canary_evidence" audit block.
DEBUG_AUDIT_CALL_FIELDS = {
    "openai_accept_status",
    "transcription_verified",
    "semantic_vad_verified",
    "sideband_open",
    "callee_joined",
    "callee_dialed",
    "amd_result",
    "answered_by",
    "answer_handling",
    "opening_sent",
    "voicemail_sent",
    "tool_call_count",
    "tool_continuation_observed",
    "interruption_observed",
    "termination_reason",
    "advisory_outcome",
    "twilio_ai_call_sid",
    "twilio_callee_call_sid",
    "conference_sid",
    "openai_call_id",
}

# Exposure policy for GET /calls: only these call-row fields are safe to surface
# in the debug call listing.
DEBUG_SAFE_CALL_FIELDS = {
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


@router.get("/calls/{call_id}")
async def get_call(call_id: str, request: Request):
    try:
        snapshot = await request.app.state.call_service.get_snapshot(call_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="call not found") from exc
    row = await request.app.state.call_service.get_call_record(call_id)
    if row is None:
        raise HTTPException(status_code=404, detail="call not found")
    transcript = await request.app.state.call_service.get_transcript_records(call_id)
    latency_events = await request.app.state.call_service.get_latency_event_records(call_id)
    return {
        **snapshot.model_dump(mode="json"),
        "canary_evidence": {key: row.get(key) for key in DEBUG_AUDIT_CALL_FIELDS},
        "latency_events": latency_events,
        "transcript": [turn.model_dump(mode="json") for turn in transcript],
    }


@router.get("/calls")
async def list_calls(request: Request):
    rows = await request.app.state.call_service.list_call_records()
    return [{key: row.get(key) for key in DEBUG_SAFE_CALL_FIELDS} for row in rows]
