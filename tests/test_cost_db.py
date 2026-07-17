from __future__ import annotations

import pytest

from tests.conftest import seed_call


@pytest.mark.asyncio
async def test_add_realtime_usage_accumulates_across_calls(database, packet):
    call_id = await seed_call(database, packet)

    await database.add_realtime_usage(
        call_id,
        input_text_tokens=10,
        input_audio_tokens=20,
        input_cached_text_tokens=1,
        input_cached_audio_tokens=2,
        output_text_tokens=5,
        output_audio_tokens=6,
    )
    await database.add_realtime_usage(
        call_id,
        input_text_tokens=100,
        input_audio_tokens=200,
        input_cached_text_tokens=10,
        input_cached_audio_tokens=20,
        output_text_tokens=50,
        output_audio_tokens=60,
    )

    call = await database.get_call(call_id)
    assert call["realtime_input_text_tokens"] == 110
    assert call["realtime_input_audio_tokens"] == 220
    assert call["realtime_input_cached_text_tokens"] == 11
    assert call["realtime_input_cached_audio_tokens"] == 22
    assert call["realtime_output_text_tokens"] == 55
    assert call["realtime_output_audio_tokens"] == 66


@pytest.mark.asyncio
async def test_add_extractor_usage_accumulates(database, packet):
    call_id = await seed_call(database, packet)

    await database.add_extractor_usage(call_id, input_tokens=100, output_tokens=20)
    await database.add_extractor_usage(call_id, input_tokens=50, output_tokens=10)

    call = await database.get_call(call_id)
    assert call["extractor_input_tokens"] == 150
    assert call["extractor_output_tokens"] == 30


@pytest.mark.asyncio
async def test_record_exa_search_increments_count_and_sums_dollars(database, packet):
    call_id = await seed_call(database, packet)

    await database.record_exa_search(call_id, cost_dollars=0.005)
    await database.record_exa_search(call_id, cost_dollars=0.007)

    call = await database.get_call(call_id)
    assert call["exa_search_count"] == 2
    assert call["exa_cost_dollars"] == pytest.approx(0.012)


@pytest.mark.asyncio
async def test_update_call_accepts_twilio_reported_duration_seconds(database, packet):
    call_id = await seed_call(database, packet)

    assert await database.update_call(call_id, twilio_reported_duration_seconds=61)

    call = await database.get_call(call_id)
    assert call["twilio_reported_duration_seconds"] == 61
