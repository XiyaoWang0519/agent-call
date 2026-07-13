from __future__ import annotations

from types import SimpleNamespace

from app.openai_realtime import RealtimeBridge


async def _noop(*args, **kwargs) -> None:
    return None


def test_initial_accept_payload_is_typed_and_matches_release_contract(settings, packet):
    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=_noop,
        on_fatal=_noop,
    )
    payload = bridge.build_accept_payload(packet).model_dump(exclude_none=True)

    assert payload["type"] == "realtime"
    assert payload["model"] == "gpt-realtime-2.1"
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["output_modalities"] == ["audio"]
    assert payload["max_output_tokens"] == 300
    assert payload["parallel_tool_calls"] is False
    assert payload["tool_choice"] == "auto"
    assert payload["audio"]["input"] == {
        "format": {"type": "audio/pcmu"},
        "transcription": {"model": "gpt-4o-mini-transcribe"},
        "turn_detection": {
            "type": "semantic_vad",
            "eagerness": "auto",
            "create_response": False,
            "interrupt_response": False,
        },
    }
    assert payload["audio"]["output"] == {
        "format": {"type": "audio/pcmu"},
        "voice": "marin",
        "speed": 1.0,
    }
    assert [tool["name"] for tool in payload["tools"]] == [
        "transfer_to_owner",
        "record_call_outcome",
    ]


def test_session_created_echo_requires_transcription_and_full_initial_vad(settings, packet):
    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=_noop,
        on_fatal=_noop,
    )
    event = {
        "session": {
            "audio": {
                "input": {
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "auto",
                        "create_response": False,
                        "interrupt_response": False,
                    },
                }
            }
        }
    }
    assert bridge.expected_transcription_echoed(event)
    assert bridge.expected_initial_vad_echoed(event)
    event["session"]["audio"]["input"]["turn_detection"]["eagerness"] = "high"
    assert not bridge.expected_initial_vad_echoed(event)
