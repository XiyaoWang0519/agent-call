from __future__ import annotations

from types import SimpleNamespace

from app.xai_realtime import RealtimeBridge


async def _noop(*args, **kwargs) -> None:
    return None


def _bridge(settings):
    return RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=_noop,
        on_fatal=_noop,
    )


def test_initial_session_config_matches_xai_voice_contract(settings, packet):
    config = _bridge(settings).build_session_config(packet).model_dump(exclude_none=False)

    assert settings.realtime_model == "grok-voice-think-fast-1.0"
    assert config["voice"] == "eve"
    assert config["reasoning"] == {"effort": "high"}
    assert config["turn_detection"] is None
    assert config["audio"] == {
        "input": {"transcription": {"model": "grok-transcribe"}},
        "output": {"speed": 1.0},
    }
    assert [tool["name"] for tool in config["tools"]] == [
        "transfer_to_owner",
        "record_call_outcome",
        "end_call",
    ]
    end_call = config["tools"][2]
    assert end_call["parameters"]["required"] == ["reason"]
    assert "objective_completed" in end_call["parameters"]["properties"]["reason"]["enum"]
    description = end_call["description"].lower()
    assert "final goodbye" in description
    assert "callee has nothing further" in description
    assert "answer it fully" in description


def test_initial_echo_requires_grok_transcription_and_manual_turn_gate(settings):
    bridge = _bridge(settings)
    event = {
        "session": {
            "audio": {"input": {"transcription": {"model": "grok-transcribe"}}},
            "turn_detection": None,
        }
    }
    assert bridge.expected_transcription_echoed(event)
    assert bridge.expected_initial_vad_echoed(event)
    event["session"]["turn_detection"] = {"type": "server_vad"}
    assert not bridge.expected_initial_vad_echoed(event)


def test_activation_echo_must_preserve_configured_server_vad(settings):
    bridge = _bridge(settings)
    event = {
        "session": {
            "turn_detection": {
                "type": "server_vad",
                "silence_duration_ms": 700,
                "prefix_padding_ms": 333,
            }
        }
    }
    assert bridge.activation_update_confirmed(event)
    event["session"]["turn_detection"]["silence_duration_ms"] = 701
    assert not bridge.activation_update_confirmed(event)
