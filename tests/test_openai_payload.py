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
    assert payload["max_output_tokens"] == "inf"
    assert payload["parallel_tool_calls"] is True
    assert payload["tool_choice"] == "auto"
    assert payload["tracing"] == "auto"
    assert payload["audio"]["input"] == {
        "transcription": {"model": "gpt-realtime-whisper"},
        "turn_detection": {
            "type": "semantic_vad",
            "eagerness": "auto",
            "create_response": False,
            "interrupt_response": False,
        },
    }
    assert payload["audio"]["output"] == {
        "voice": "cedar",
        "speed": 1.0,
    }
    assert "format" not in payload["audio"]["input"]
    assert "format" not in payload["audio"]["output"]
    assert [tool["name"] for tool in payload["tools"]] == [
        "transfer_to_owner",
        "record_call_outcome",
        "search_web",
        "send_dtmf",
        "end_call",
    ]
    assert "ask_agent" not in [tool["name"] for tool in payload["tools"]]
    search_web = payload["tools"][2]
    assert search_web["parameters"] == {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 2,
                "maxLength": 500,
                "description": (
                    "A standalone natural-language web search query with all context needed "
                    "to understand it."
                ),
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    send_dtmf = payload["tools"][3]
    assert send_dtmf["parameters"]["required"] == ["digits"]
    assert send_dtmf["parameters"]["properties"]["digits"]["pattern"] == "^[0-9*#w]{1,32}$"
    assert (
        "explicitly requested short test sequence together"
        in send_dtmf["parameters"]["properties"]["digits"]["description"]
    )
    assert send_dtmf["parameters"]["additionalProperties"] is False
    end_call = payload["tools"][4]
    assert end_call["parameters"]["required"] == ["reason"]
    assert "objective_completed" in end_call["parameters"]["properties"]["reason"]["enum"]
    assert "final goodbye" in end_call["description"].lower()
    # Ending must be gated on the callee having nothing further, not just objective status;
    # a pending callee request must be answered as a normal turn before end_call.
    description = end_call["description"].lower()
    assert "callee has nothing further" in description
    assert "answer it fully" in description


def test_accept_payload_includes_ask_agent_when_enabled(settings, packet):
    settings.ask_agent_enabled = True
    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=_noop,
        on_fatal=_noop,
    )
    payload = bridge.build_accept_payload(packet).model_dump(exclude_none=True)
    names = [tool["name"] for tool in payload["tools"]]
    assert names == [
        "transfer_to_owner",
        "record_call_outcome",
        "search_web",
        "send_dtmf",
        "ask_agent",
        "end_call",
    ]
    ask_agent = next(tool for tool in payload["tools"] if tool["name"] == "ask_agent")
    assert ask_agent["parameters"]["required"] == ["question"]
    assert "question" in ask_agent["parameters"]["properties"]
    assert "Never guess" in ask_agent["description"]
    assert "ask_agent" in payload["instructions"]


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
                    "transcription": {"model": "gpt-realtime-whisper"},
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


def test_semantic_vad_high_setting_is_available_for_tuning(settings, packet):
    from app.settings import Settings

    values = settings.model_dump()
    values["semantic_vad_eagerness"] = "high"
    tuned_settings = Settings(**values)
    bridge = RealtimeBridge(
        tuned_settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=_noop,
        on_fatal=_noop,
    )

    payload = bridge.build_accept_payload(packet).model_dump(exclude_none=True)
    turn = payload["audio"]["input"]["turn_detection"]
    assert turn["eagerness"] == "high"
    assert bridge.expected_initial_vad_echoed(
        {"session": {"audio": {"input": {"turn_detection": turn}}}}
    )


def test_activation_echo_must_preserve_configured_semantic_vad(settings):
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
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "auto",
                        "create_response": True,
                        "interrupt_response": True,
                    }
                }
            }
        }
    }

    assert bridge.activation_update_confirmed(event)
    event["session"]["audio"]["input"]["turn_detection"]["eagerness"] = "high"
    assert not bridge.activation_update_confirmed(event)
