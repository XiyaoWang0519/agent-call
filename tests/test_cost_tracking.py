from __future__ import annotations

import pytest

from app.models import CallState
from tests.conftest import seed_call


def _response_done_event(*, response_id: str) -> dict:
    return {
        "type": "response.done",
        "response": {
            "id": response_id,
            "status": "completed",
            "usage": {
                "input_token_details": {
                    "text_tokens": 100,
                    "audio_tokens": 200,
                    "cached_tokens_details": {"text_tokens": 10, "audio_tokens": 20},
                },
                "output_token_details": {"text_tokens": 30, "audio_tokens": 40},
            },
        },
    }


@pytest.mark.asyncio
async def test_response_done_accumulates_realtime_usage_and_flows_into_snapshot_cost(
    service, packet
):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)

    await service.handle_realtime_event(call_id, _response_done_event(response_id="resp_1"))

    call = await service.db.get_call(call_id)
    assert call["realtime_input_text_tokens"] == 100
    assert call["realtime_input_audio_tokens"] == 200
    assert call["realtime_input_cached_text_tokens"] == 10
    assert call["realtime_input_cached_audio_tokens"] == 20
    assert call["realtime_output_text_tokens"] == 30
    assert call["realtime_output_audio_tokens"] == 40

    await service.handle_realtime_event(call_id, _response_done_event(response_id="resp_2"))

    call = await service.db.get_call(call_id)
    assert call["realtime_input_text_tokens"] == 200
    assert call["realtime_input_audio_tokens"] == 400
    assert call["realtime_input_cached_text_tokens"] == 20
    assert call["realtime_input_cached_audio_tokens"] == 40
    assert call["realtime_output_text_tokens"] == 60
    assert call["realtime_output_audio_tokens"] == 80

    snapshot = await service.get_snapshot(call_id)
    assert snapshot.cost is not None
    assert snapshot.cost.total_cost_usd > 0
    assert snapshot.cost.usage.realtime_input_text_tokens == 200


@pytest.mark.asyncio
async def test_response_done_with_no_usage_data_does_not_write(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)

    await service.handle_realtime_event(
        call_id, {"type": "response.done", "response": {"id": "resp_1", "status": "completed"}}
    )

    call = await service.db.get_call(call_id)
    assert call["realtime_input_text_tokens"] == 0
    assert call["realtime_input_audio_tokens"] == 0


@pytest.mark.asyncio
async def test_response_done_records_usage_for_cancelled_responses(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    event = _response_done_event(response_id="resp_1")
    event["response"]["status"] = "cancelled"

    await service.handle_realtime_event(call_id, event)

    call = await service.db.get_call(call_id)
    assert call["realtime_input_text_tokens"] == 100
