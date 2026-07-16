from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.datastructures import FormData

from app.security import verify_twilio_request

router = APIRouter(prefix="/webhooks/twilio", tags=["twilio-webhooks"])


def _form_dict(form: FormData) -> dict[str, str]:
    return {key: str(value) for key, value in form.multi_items()}


async def _validated_call_id(request: Request) -> str:
    call_id = request.query_params.get("call_id")
    plan_id = request.query_params.get("plan_id")
    if not call_id or not plan_id:
        raise HTTPException(status_code=400, detail="missing call mapping")
    call = await request.app.state.call_service.resolve_webhook_call(call_id, plan_id)
    if call is None:
        raise HTTPException(status_code=400, detail="invalid call mapping")
    return call_id


@router.post("/amd")
async def amd_callback(
    request: Request, form: FormData = Depends(verify_twilio_request)
) -> Response:
    call_id = await _validated_call_id(request)
    await request.app.state.call_service.handle_amd(
        call_id, str(form.get("AnsweredBy") or "unknown")
    )
    return Response(status_code=204)


@router.post("/conference")
async def conference_callback(
    request: Request, form: FormData = Depends(verify_twilio_request)
) -> Response:
    call_id = await _validated_call_id(request)
    await request.app.state.call_service.handle_conference_event(call_id, _form_dict(form))
    return Response(status_code=204)


@router.post("/participant-status")
async def participant_status_callback(
    request: Request, form: FormData = Depends(verify_twilio_request)
) -> Response:
    call_id = await _validated_call_id(request)
    leg = request.query_params.get("leg")
    if leg not in {"agent", "callee", "owner"}:
        raise HTTPException(status_code=400, detail="invalid participant leg")
    await request.app.state.call_service.handle_participant_status(call_id, leg, _form_dict(form))
    return Response(status_code=204)
