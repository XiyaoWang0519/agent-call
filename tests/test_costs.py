from __future__ import annotations

from app.costs import compute_call_cost


def test_compute_call_cost_matches_hand_computed_components(settings):
    call = {
        "realtime_input_text_tokens": 1000,
        "realtime_input_audio_tokens": 2000,
        "realtime_input_cached_text_tokens": 200,
        "realtime_input_cached_audio_tokens": 500,
        "realtime_output_text_tokens": 300,
        "realtime_output_audio_tokens": 400,
        "extractor_input_tokens": 5000,
        "extractor_output_tokens": 1000,
        "exa_search_count": 3,
        "exa_cost_dollars": 0.021,
        "twilio_reported_duration_seconds": None,
        "duration_seconds": 90,
    }

    cost = compute_call_cost(call, settings)

    uncached_text = 1000 - 200
    uncached_audio = 2000 - 500
    expected_realtime = (
        uncached_text * settings.realtime_text_input_price_per_1m / 1e6
        + uncached_audio * settings.realtime_audio_input_price_per_1m / 1e6
        + 200 * settings.realtime_cached_text_input_price_per_1m / 1e6
        + 500 * settings.realtime_cached_audio_input_price_per_1m / 1e6
        + 300 * settings.realtime_text_output_price_per_1m / 1e6
        + 400 * settings.realtime_audio_output_price_per_1m / 1e6
    )
    expected_extractor = (
        5000 * settings.extractor_input_price_per_1m / 1e6
        + 1000 * settings.extractor_output_price_per_1m / 1e6
    )
    # 90 seconds falls back to duration_seconds (twilio_reported is None) and rounds up to
    # 2 billable minutes.
    expected_twilio = 2 * settings.twilio_voice_price_per_minute
    expected_exa = 0.021
    expected_total = expected_realtime + expected_extractor + expected_twilio + expected_exa

    assert cost.realtime_cost_usd == round(expected_realtime, 6)
    assert cost.extractor_cost_usd == round(expected_extractor, 6)
    assert cost.twilio_cost_usd == round(expected_twilio, 6)
    assert cost.exa_cost_usd == round(expected_exa, 6)
    assert cost.total_cost_usd == round(expected_total, 6)
    assert cost.usage.billable_duration_seconds == 90
    assert cost.usage.exa_search_count == 3
    assert cost.currency == "USD"
    assert cost.estimated is True


def test_compute_call_cost_treats_none_columns_as_zero(settings):
    call = {
        "realtime_input_text_tokens": None,
        "realtime_input_audio_tokens": None,
        "realtime_input_cached_text_tokens": None,
        "realtime_input_cached_audio_tokens": None,
        "realtime_output_text_tokens": None,
        "realtime_output_audio_tokens": None,
        "extractor_input_tokens": None,
        "extractor_output_tokens": None,
        "exa_search_count": None,
        "exa_cost_dollars": None,
        "twilio_reported_duration_seconds": None,
        "duration_seconds": None,
    }

    cost = compute_call_cost(call, settings)

    assert cost.realtime_cost_usd == 0.0
    assert cost.extractor_cost_usd == 0.0
    assert cost.twilio_cost_usd == 0.0
    assert cost.exa_cost_usd == 0.0
    assert cost.total_cost_usd == 0.0
    assert cost.usage.billable_duration_seconds is None


def test_compute_call_cost_prefers_twilio_reported_duration_over_app_duration(settings):
    call = {
        "twilio_reported_duration_seconds": 61,
        "duration_seconds": 500,
    }

    cost = compute_call_cost(call, settings)

    assert cost.usage.billable_duration_seconds == 61
    # 61 seconds rounds up to 2 billable minutes.
    assert cost.twilio_cost_usd == round(2 * settings.twilio_voice_price_per_minute, 6)


def test_compute_call_cost_zero_duration_yields_zero_twilio_cost(settings):
    call = {"twilio_reported_duration_seconds": 0, "duration_seconds": None}

    cost = compute_call_cost(call, settings)

    assert cost.usage.billable_duration_seconds == 0
    assert cost.twilio_cost_usd == 0.0
