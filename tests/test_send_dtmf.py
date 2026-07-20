from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from app.main import create_app
from app.models import CallState
from app.settings import Settings
from tests.conftest import seed_call, wait_background


def _tool_event(
    tool_call_id: str,
    name: str,
    arguments: str,
) -> dict[str, str]:
    return {
        "type": "response.function_call_arguments.done",
        "event_id": f"evt_{tool_call_id}",
        "call_id": tool_call_id,
        "name": name,
        "arguments": arguments,
    }


async def test_send_dtmf_happy_path_records_twilio_call_and_result(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)

    await service.handle_realtime_event(
        call_id,
        _tool_event("tool_dtmf", "send_dtmf", '{"digits":"1w2"}'),
    )
    await wait_background()

    assert service._test_twilio.dtmf == [("CF" + "a" * 32, "CA" + "b" * 32, "1w2")]
    assert service._test_realtime.tool_results[-1] == (
        call_id,
        "tool_dtmf",
        {"ok": True, "digits": "1w2"},
    )
    call = await service.db.get_call(call_id)
    assert call["tool_call_count"] == 1


async def test_send_dtmf_rejects_invalid_digits(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)

    for bad_digits in ('{"digits":"5551234;DROP"}', '{"digits":""}'):
        await service.handle_realtime_event(
            call_id,
            _tool_event("tool_dtmf_bad", "send_dtmf", bad_digits),
        )
        await wait_background()

        assert service._test_twilio.dtmf == []
        assert service._test_realtime.tool_results[-1][2] == {
            "ok": False,
            "error": "invalid_dtmf_request",
        }


async def test_send_dtmf_rejects_when_call_not_ready(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.PREWARMING)

    await service.handle_realtime_event(
        call_id,
        _tool_event("tool_dtmf_not_ready", "send_dtmf", '{"digits":"1"}'),
    )
    await wait_background()

    assert service._test_twilio.dtmf == []
    assert service._test_realtime.tool_results[-1][2] == {
        "ok": False,
        "error": "call_not_ready",
    }


async def test_send_dtmf_rejects_when_callee_sid_missing(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service.db.update_call(call_id, twilio_callee_call_sid=None)

    await service.handle_realtime_event(
        call_id,
        _tool_event("tool_dtmf_no_sid", "send_dtmf", '{"digits":"1"}'),
    )
    await wait_background()

    assert service._test_twilio.dtmf == []
    assert service._test_realtime.tool_results[-1][2] == {
        "ok": False,
        "error": "call_not_ready",
    }


async def test_send_dtmf_twilio_failure_reports_error_and_clears_inflight(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._test_twilio.dtmf_exc = TwilioRestException(500, "https://twilio.test", "boom")

    await service.handle_realtime_event(
        call_id,
        _tool_event("tool_dtmf_fail", "send_dtmf", '{"digits":"1"}'),
    )
    await wait_background()

    assert service._test_realtime.tool_results[-1][2] == {
        "ok": False,
        "error": "dtmf_failed",
    }
    assert call_id not in service._inflight_tools


async def test_send_dtmf_runs_in_background_and_does_not_block_dispatcher(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    dtmf_started = asyncio.Event()
    release_dtmf = asyncio.Event()

    async def blocked_send_dtmf(*args, **kwargs):
        del args, kwargs
        dtmf_started.set()
        await release_dtmf.wait()

    service._test_twilio.send_dtmf = blocked_send_dtmf

    # The per-call dispatcher must not stall on the slow Twilio call: this await
    # has to return promptly even though send_dtmf is still in flight.
    await asyncio.wait_for(
        service.handle_realtime_event(
            call_id,
            _tool_event("tool_dtmf_slow", "send_dtmf", '{"digits":"1"}'),
        ),
        timeout=1,
    )
    await asyncio.wait_for(dtmf_started.wait(), timeout=1)
    assert service._test_realtime.tool_results == []

    release_dtmf.set()
    await wait_background()
    assert service._test_realtime.tool_results[-1][1] == "tool_dtmf_slow"


async def test_send_dtmf_cancels_active_response_before_delivering_result(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    service._active_response_ids[call_id] = "resp_active"

    await service.handle_realtime_event(
        call_id,
        _tool_event("tool_dtmf_cancel", "send_dtmf", '{"digits":"1"}'),
    )
    await wait_background()

    assert ("cancel_response", call_id) in service._test_realtime.events
    assert call_id not in service._active_response_ids
    assert service._test_realtime.tool_results[-1][1] == "tool_dtmf_cancel"


async def test_send_dtmf_call_not_ready_does_not_cancel_active_response(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.PREWARMING)
    service._active_response_ids[call_id] = "resp_active"

    await service.handle_realtime_event(
        call_id,
        _tool_event("tool_dtmf_not_ready_cancel", "send_dtmf", '{"digits":"1"}'),
    )
    await wait_background()

    assert ("cancel_response", call_id) not in service._test_realtime.events
    assert service._active_response_ids[call_id] == "resp_active"


def test_announce_dtmf_webhook_returns_play_twiml(settings: Settings, packet):
    app = create_app(settings)
    params = {}
    validator = RequestValidator(Settings.reveal(settings.twilio_auth_token))
    with TestClient(app) as client:
        client.portal.call(seed_call, app.state.call_service.db, packet)

        valid_path = (
            "/webhooks/twilio/announce-dtmf?call_id=call_test&plan_id=plan_call_test&digits=12w%23"
        )
        valid_signature = validator.compute_signature(
            f"{settings.public_base_url}{valid_path}", params
        )
        valid = client.post(
            valid_path,
            headers={"X-Twilio-Signature": valid_signature},
        )
        assert valid.status_code == 200
        assert valid.headers["content-type"].startswith("text/xml")
        assert '<Play digits="12w#"/>' in valid.text

        bad_path = "/webhooks/twilio/announce-dtmf?call_id=call_test&plan_id=plan_call_test&digits=bad;digits"
        bad_signature = validator.compute_signature(f"{settings.public_base_url}{bad_path}", params)
        bad = client.post(
            bad_path,
            headers={"X-Twilio-Signature": bad_signature},
        )
        assert bad.status_code == 400

        missing_path = "/webhooks/twilio/announce-dtmf?call_id=call_none&plan_id=plan_none&digits=1"
        missing_signature = validator.compute_signature(
            f"{settings.public_base_url}{missing_path}", params
        )
        missing = client.post(
            missing_path,
            headers={"X-Twilio-Signature": missing_signature},
        )
        assert missing.status_code == 400
