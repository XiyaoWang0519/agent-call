from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from openai import InvalidWebhookSignatureError
from pydantic import ValidationError

from app.models import RealtimeIncomingEvent
from app.settings import Settings

router = APIRouter(prefix="/webhooks/openai", tags=["openai-webhooks"])


@router.post("")
async def openai_webhook(request: Request) -> Response:
    body = await request.body()
    service = request.app.state.call_service
    settings: Settings = request.app.state.settings
    try:
        event = service.openai.webhooks.unwrap(
            body,
            request.headers,
            secret=Settings.reveal(settings.openai_webhook_secret),
        )
    except (InvalidWebhookSignatureError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid OpenAI signature") from exc
    webhook_id = request.headers.get("webhook-id")
    if not webhook_id or not await service.db.record_webhook_once(webhook_id):
        raise HTTPException(status_code=400, detail="replayed or missing webhook-id")
    if event.type != "realtime.call.incoming":
        return Response(status_code=204)
    try:
        typed = RealtimeIncomingEvent.model_validate(event.model_dump())
        await service.handle_openai_incoming(
            typed.data.call_id,
            typed.data.sip_headers,
        )
    except (ValidationError, LookupError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=200)
