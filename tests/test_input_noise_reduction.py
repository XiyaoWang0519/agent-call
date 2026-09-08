from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models import InputNoiseReduction
from app.openai_realtime import RealtimeBridge, RealtimeRuntime
from app.settings import Settings


async def _noop(*args, **kwargs) -> None:
    return None


@pytest.mark.parametrize("mode", ["near_field", "far_field"])
def test_noise_reduction_accepts_environment_configuration(mode):
    settings = Settings.from_environ({"INPUT_NOISE_REDUCTION": mode})
    assert settings.input_noise_reduction == mode


@pytest.mark.parametrize("mode", ["off", "auto", "near-field", ""])
def test_noise_reduction_rejects_unsupported_configuration(mode):
    with pytest.raises(ValidationError, match="input_noise_reduction"):
        Settings.from_environ({"INPUT_NOISE_REDUCTION": mode})
    with pytest.raises(ValidationError):
        InputNoiseReduction(type=mode)


def test_noise_reduction_defaults_to_far_field():
    assert Settings.from_environ({}).input_noise_reduction == "far_field"


@pytest.mark.parametrize("mode", [None, "near_field", "far_field"])
@pytest.mark.parametrize("turn_mode", ["semantic_vad", "server_vad"])
async def test_noise_filter_survives_accept_activation_hold_and_resume(
    settings, packet, mode, turn_mode
):
    settings.turn_detection_mode = turn_mode
    settings.input_noise_reduction = mode
    settings.input_transcription_delay = "low"
    settings.semantic_vad_eagerness = "medium"
    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=_noop,
        on_fatal=_noop,
    )
    expected_input = {
        "transcription": {"model": "gpt-realtime-whisper", "delay": "low"},
        "turn_detection": {
            "type": "semantic_vad",
            "eagerness": "medium",
            "create_response": False,
            "interrupt_response": False,
        },
    }
    if turn_mode == "server_vad":
        expected_input["turn_detection"] = {
            "type": "server_vad",
            "threshold": 0.5,
            "silence_duration_ms": 300,
            "prefix_padding_ms": 300,
            "create_response": False,
            "interrupt_response": False,
        }
    if mode is not None:
        expected_input["noise_reduction"] = {"type": mode}

    accepted = bridge.build_accept_payload(packet).model_dump(mode="json", exclude_none=True)
    assert accepted["audio"]["input"] == expected_input
    assert "format" not in accepted["audio"]["output"]

    class EchoWebSocket:
        async def send(self, message: str) -> None:
            event = json.loads(message)
            assert event["type"] == "session.update"
            assert event["session"]["audio"] == {"input": expected_input}
            # Echo only after inspecting the actual serialized socket payload.
            runtime.update_waiter.set_result(
                {"type": "session.updated", "session": event["session"]}
            )

    runtime = RealtimeRuntime(
        call_id="call_noise", openai_call_id="rtc_noise", websocket=EchoWebSocket()
    )
    bridge._runtime["call_noise"] = runtime
    for update, enabled in [
        (bridge.verify_initial_session, False),
        (bridge.enable_automatic_responses, True),
        (bridge.suspend_automatic_responses, False),
        (bridge.enable_automatic_responses, True),
    ]:
        expected_input["turn_detection"]["create_response"] = enabled
        expected_input["turn_detection"]["interrupt_response"] = enabled
        echoed = await asyncio.wait_for(update("call_noise"), timeout=1)
        assert echoed["session"]["audio"]["input"] == expected_input
        assert bridge.expected_transcription_echoed(echoed)
        if enabled:
            assert bridge.activation_update_confirmed(echoed)
        else:
            assert bridge.expected_initial_vad_echoed(echoed)
