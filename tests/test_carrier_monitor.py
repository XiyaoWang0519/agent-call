from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator

from app.call_audio import CallAudio
from app.main import create_app
from app.models import CallState
from app.settings import Settings
from tests.conftest import seed_call
from tests.test_call_audio import SILENCE, VOICE


async def binding(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._call_audio[call_id] = CallAudio()
    service._media_tokens[call_id] = "one-use-random-token"
    service._media_ready[call_id] = asyncio.Event()
    return call_id, {
        "accountSid": service.settings.twilio_account_sid,
        "callSid": "CA" + "b" * 32,
        "streamSid": "MZ" + "d" * 32,
        "tracks": ["inbound", "outbound"],
        "customParameters": {"token": "one-use-random-token"},
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
    }


async def test_carrier_token_can_bind_only_once(service, packet):
    call_id, start = await binding(service, packet)
    assert await service.accept_media_monitor(call_id, f"plan_{call_id}", start)
    assert service._media_ready[call_id].is_set()
    assert call_id not in service._media_tokens
    assert not await service.accept_media_monitor(call_id, f"plan_{call_id}", start)


async def test_overlapping_carrier_speech_persists_interruption(service, packet):
    call_id, start = await binding(service, packet)
    assert await service.accept_media_monitor(call_id, f"plan_{call_id}", start)
    for track in ("outbound", "inbound"):
        service.observe_media_audio(call_id, track, 0, VOICE)
        service.observe_media_audio(call_id, track, 20, VOICE)
    for _ in range(50):
        if (await service.db.get_call(call_id))["interruption_observed"]:
            break
        await asyncio.sleep(0.01)
    assert (await service.db.get_call(call_id))["interruption_observed"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("accountSid", "ACwrong"),
        ("callSid", "CAwrong"),
        ("customParameters", {"token": "wrong"}),
        ("tracks", ["inbound"]),
        ("mediaFormat", {"encoding": "audio/pcm", "sampleRate": 24000, "channels": 1}),
        ("streamSid", None),
    ],
)
async def test_carrier_binding_rejects_wrong_identity_or_codec(service, packet, field, value):
    call_id, start = await binding(service, packet)
    start[field] = value
    assert not await service.accept_media_monitor(call_id, f"plan_{call_id}", start)
    assert not service._media_ready[call_id].is_set()
    assert call_id in service._media_tokens


async def test_carrier_binding_rejects_wrong_plan(service, packet):
    call_id, start = await binding(service, packet)
    assert not await service.accept_media_monitor(call_id, "plan_other", start)


@pytest.mark.parametrize(
    "status,reason",
    [("in-progress", "carrier_monitor_lost"), ("completed", "callee_call_completed")],
)
async def test_stream_close_reconciles_callee_hangup(service, packet, monkeypatch, status, reason):
    call_id, start = await binding(service, packet)
    assert await service.accept_media_monitor(call_id, f"plan_{call_id}", start)
    monkeypatch.setattr(
        service.twilio,
        "callee_status",
        AsyncMock(return_value={"CallStatus": status, "CallDuration": "12"}),
    )
    await service.media_monitor_closed(call_id)
    call = await service.db.get_call(call_id)
    assert call["termination_reason"] == reason
    if status == "completed":
        assert call["twilio_reported_duration_seconds"] == 12


def test_carrier_websocket_rejects_unsigned_connection(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/webhooks/twilio/media/call_1/plan_1"):
                pytest.fail("unsigned socket accepted")


@pytest.mark.parametrize("wrong_stream", [False, True])
@pytest.mark.parametrize(
    "scheme,suffix", [("https", ""), ("wss", ""), ("wss", "/"), ("https", "/")]
)
def test_signed_carrier_socket_forwards_only_bound_stream(settings, wrong_stream, scheme, suffix):
    app = create_app(settings)
    path = "/webhooks/twilio/media/call_1/plan_1"
    signature = RequestValidator(Settings.reveal(settings.twilio_auth_token)).compute_signature(
        settings.public_base_url.replace("https://", scheme + "://") + path + suffix, {}
    )
    with TestClient(app) as client:
        svc = app.state.call_service
        svc.accept_media_monitor = AsyncMock(return_value=True)
        svc.observe_media_audio = Mock()
        svc.media_monitor_closed = AsyncMock()
        with client.websocket_connect(path, headers={"X-Twilio-Signature": signature}) as ws:
            ws.send_json({"event": "connected"})
            ws.send_json({"event": "start", "start": {"streamSid": "MZexpected"}})
            ws.send_json(
                {
                    "event": "media",
                    "streamSid": "MZother" if wrong_stream else "MZexpected",
                    "media": {"track": "outbound", "timestamp": "20", "payload": SILENCE},
                }
            )
            if not wrong_stream:
                ws.send_json({"event": "stop", "streamSid": "MZexpected"})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
        svc.accept_media_monitor.assert_awaited_once_with(
            "call_1", "plan_1", {"streamSid": "MZexpected"}
        )
        assert svc.observe_media_audio.call_count == (0 if wrong_stream else 1)
        svc.media_monitor_closed.assert_awaited_once_with("call_1")
