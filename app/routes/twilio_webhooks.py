from __future__ import annotations

import asyncio
import json

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from starlette.datastructures import FormData
from twilio.request_validator import RequestValidator

from app.models import DTMF_DIGITS_PATTERN
from app.security import verify_twilio_request
from app.settings import Settings

router = APIRouter(prefix="/webhooks/twilio", tags=["twilio-webhooks"])


@router.websocket("/media/{call_id}/{plan_id}")
async def carrier_media(websocket: WebSocket, call_id: str, plan_id: str) -> None:
    service = websocket.app.state.call_service
    settings = service.settings
    # Validate against the configured public origin, never the untrusted Host header.
    url = (
        f"{(settings.public_base_url or '').rstrip('/')}/webhooks/twilio/media/{call_id}/{plan_id}"
    )
    validator = RequestValidator(Settings.reveal(settings.twilio_auth_token))
    # Twilio Voice may sign the WSS URL with a trailing slash. Restrict all
    # canonical forms to this configured origin/path, then require the call token.
    signature = websocket.headers.get("x-twilio-signature", "")
    if websocket.url.query or not any(
        validator.validate(candidate + suffix, {}, signature)
        for candidate in (url, url.replace("https://", "wss://", 1))
        for suffix in ("", "/")
    ):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    bound = False
    stream_sid: str | None = None
    try:
        async with asyncio.timeout(5):
            while not bound:
                message = await websocket.receive_text()
                if len(message) > 32_768:
                    raise ValueError("oversized carrier event")
                event = json.loads(message)
                if event.get("event") == "connected":
                    continue
                if event.get("event") != "start":
                    raise ValueError("carrier stream must start before media")
                start = event.get("start") or {}
                if not await service.accept_media_monitor(call_id, plan_id, start):
                    raise ValueError("carrier stream is not authorized for this call")
                stream_sid = start["streamSid"]
                bound = True
        while True:
            message = await websocket.receive_text()
            if len(message) > 32_768:
                raise ValueError("oversized carrier event")
            event = json.loads(message)
            if event.get("streamSid") != stream_sid:
                raise ValueError("carrier stream identity changed")
            if event.get("event") == "media":
                media = event.get("media") or {}
                service.observe_media_audio(
                    call_id, media["track"], int(media["timestamp"]), media["payload"]
                )
            elif event.get("event") == "stop":
                break
    except (WebSocketDisconnect, TimeoutError, ValueError, KeyError, TypeError):
        pass
    finally:
        if bound:
            await service.media_monitor_closed(call_id)
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass


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


@router.post("/announce-dtmf")
async def announce_dtmf(
    request: Request, form: FormData = Depends(verify_twilio_request)
) -> Response:
    await _validated_call_id(request)
    digits = request.query_params.get("digits")
    if not digits or not DTMF_DIGITS_PATTERN.match(digits):
        raise HTTPException(status_code=400, detail="invalid dtmf digits")
    return Response(
        content=(
            f'<?xml version="1.0" encoding="UTF-8"?><Response><Play digits="{digits}"/></Response>'
        ),
        media_type="text/xml",
    )
