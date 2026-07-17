"""Pure cost-estimation math for a single call's token/search/telephony usage.

No I/O: callers pass the `calls` row dict and the resolved `Settings` pricing fields.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from app.models import CallCost, CallUsage
from app.settings import Settings


def compute_call_cost(call: Mapping[str, Any], settings: Settings) -> CallCost:
    input_text = int(call.get("realtime_input_text_tokens") or 0)
    input_audio = int(call.get("realtime_input_audio_tokens") or 0)
    cached_text = int(call.get("realtime_input_cached_text_tokens") or 0)
    cached_audio = int(call.get("realtime_input_cached_audio_tokens") or 0)
    output_text = int(call.get("realtime_output_text_tokens") or 0)
    output_audio = int(call.get("realtime_output_audio_tokens") or 0)
    uncached_text = max(0, input_text - cached_text)
    uncached_audio = max(0, input_audio - cached_audio)

    realtime_cost_usd = (
        uncached_text * settings.realtime_text_input_price_per_1m / 1e6
        + uncached_audio * settings.realtime_audio_input_price_per_1m / 1e6
        + cached_text * settings.realtime_cached_text_input_price_per_1m / 1e6
        + cached_audio * settings.realtime_cached_audio_input_price_per_1m / 1e6
        + output_text * settings.realtime_text_output_price_per_1m / 1e6
        + output_audio * settings.realtime_audio_output_price_per_1m / 1e6
    )

    extractor_input = int(call.get("extractor_input_tokens") or 0)
    extractor_output = int(call.get("extractor_output_tokens") or 0)
    extractor_cost_usd = (
        extractor_input * settings.extractor_input_price_per_1m / 1e6
        + extractor_output * settings.extractor_output_price_per_1m / 1e6
    )

    twilio_reported_duration_seconds = call.get("twilio_reported_duration_seconds")
    billable_duration_seconds = (
        twilio_reported_duration_seconds
        if twilio_reported_duration_seconds is not None
        else call.get("duration_seconds")
    )
    twilio_cost_usd = (
        math.ceil(billable_duration_seconds / 60) * settings.twilio_voice_price_per_minute
        if billable_duration_seconds and billable_duration_seconds > 0
        else 0.0
    )

    exa_search_count = int(call.get("exa_search_count") or 0)
    exa_cost_usd = float(call.get("exa_cost_dollars") or 0)

    total_cost_usd = realtime_cost_usd + extractor_cost_usd + twilio_cost_usd + exa_cost_usd

    usage = CallUsage(
        realtime_input_text_tokens=input_text,
        realtime_input_audio_tokens=input_audio,
        realtime_input_cached_text_tokens=cached_text,
        realtime_input_cached_audio_tokens=cached_audio,
        realtime_output_text_tokens=output_text,
        realtime_output_audio_tokens=output_audio,
        extractor_input_tokens=extractor_input,
        extractor_output_tokens=extractor_output,
        exa_search_count=exa_search_count,
        twilio_reported_duration_seconds=twilio_reported_duration_seconds,
        billable_duration_seconds=billable_duration_seconds,
    )
    return CallCost(
        usage=usage,
        realtime_cost_usd=round(realtime_cost_usd, 6),
        extractor_cost_usd=round(extractor_cost_usd, 6),
        twilio_cost_usd=round(twilio_cost_usd, 6),
        exa_cost_usd=round(exa_cost_usd, 6),
        total_cost_usd=round(total_cost_usd, 6),
    )
