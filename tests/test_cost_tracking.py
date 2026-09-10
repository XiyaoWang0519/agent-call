from __future__ import annotations

import pytest

from app.models import CallState
from tests.conftest import seed_call


def backend_event(response_id, kind="response.completed"):
    return {
        "type": "response.event",
        "delegation_id": "delegation_1",
        "event": {
            "type": kind,
            "response": {
                "id": response_id,
                "output": [],
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 10},
                    "output_tokens": 30,
                },
            },
        },
    }


async def test_live_snapshots_are_cumulative_and_final_usage_is_durable(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    for seconds in [12, 12, 10, 24]:
        await service.handle_live_event(
            call_id, {"type": "session.usage.updated", "usage": {"seconds": seconds}}
        )
    snapshot = await service.get_snapshot(call_id)
    assert snapshot.cost.usage.live_session_seconds == 24
    assert snapshot.cost.usage.live_usage_finalized is False
    assert snapshot.cost.live_cost_usd == pytest.approx(0.02)
    # A closed event finalizes accounting even if it reports a connection-loss reason.
    await service.handle_live_event(
        call_id, {"type": "session.closed", "reason": "connection_lost", "usage": {"seconds": 30}}
    )
    await service.handle_live_event(
        call_id, {"type": "session.usage.updated", "usage": {"seconds": 99}}
    )
    row = await service.db.get_call(call_id)
    assert row["live_session_seconds"] == 30
    assert row["live_usage_finalized"] == 1


@pytest.mark.parametrize(
    "kind", ["response.completed", "response.cancelled", "response.failed", "response.incomplete"]
)
async def test_backend_usage_is_separate_and_deduplicated(service, packet, kind):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    event = backend_event("resp_1", kind)
    await service.handle_live_event(call_id, event)
    await service.handle_live_event(call_id, event)
    await service.handle_live_event(call_id, backend_event("resp_2"))
    snapshot = await service.get_snapshot(call_id)
    assert snapshot.cost.usage.backend_input_tokens == 200
    assert snapshot.cost.usage.backend_cached_input_tokens == 20
    assert snapshot.cost.usage.backend_output_tokens == 60
    assert snapshot.cost.usage.realtime_input_text_tokens == 0
    assert snapshot.cost.usage.live_session_seconds == 0
    assert snapshot.cost.backend_cost_usd == pytest.approx(0.001084)


async def test_missing_usage_does_not_mark_finalization_or_consume_response_id(service, packet):
    call_id = await seed_call(service.db, packet, state=CallState.ACTIVE)
    await service.handle_live_event(
        call_id,
        {
            "type": "response.event",
            "event": {"type": "response.completed", "response": {"id": "resp_1"}},
        },
    )
    await service.handle_live_event(call_id, backend_event("resp_1"))
    snapshot = await service.get_snapshot(call_id)
    assert snapshot.cost.usage.backend_input_tokens == 100
    assert snapshot.cost.usage.live_usage_finalized is False


@pytest.mark.parametrize("seconds", [-1, float("inf"), float("nan")])
async def test_invalid_duration_is_rejected(service, packet, seconds):
    call_id = await seed_call(service.db, packet)
    with pytest.raises(ValueError):
        await service.db.record_live_usage(call_id, seconds=seconds, finalized=False)
