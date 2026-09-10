from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError

from app.openai_live import LiveBridge
from app.settings import Settings


async def _noop(*args, **kwargs) -> None:
    return None


def bridge_for(settings):
    return LiveBridge(settings, SimpleNamespace(), on_event=_noop, on_open=_noop, on_fatal=_noop)


def test_accept_contract_separates_live_voice_and_responses_tools(settings, packet):
    payload = bridge_for(settings).build_accept_payload(packet).model_dump(exclude_none=True)
    assert set(payload) == {"session"}
    session = payload["session"]
    assert session["type"] == "live"
    assert session["model"] == "gpt-live-1"
    assert session["store"] is False
    assert session["audio"] == {"output": {"voice": "marin"}}
    assert session["delegation"]["type"] == "responses"
    backend = session["delegation"]["responses"]
    assert backend["model"] == "gpt-5.6-terra"
    assert backend["reasoning"] == {"effort": "low"}
    assert backend["parallel_tool_calls"] is False
    assert backend["tool_choice"] == "auto"
    assert packet.approved_context_json() in backend["instructions"]
    assert packet.approved_context_json() not in session["instructions"]
    assert {t["name"] for t in backend["tools"]} == {
        "transfer_to_owner",
        "record_call_outcome",
        "search_web",
        "send_dtmf",
        "finish_call_after_goodbye",
    }
    assert all(t["type"] == "function" and "function" not in t for t in backend["tools"])
    closing = next(t for t in backend["tools"] if t["name"] == "finish_call_after_goodbye")
    assert closing["parameters"]["required"] == ["reason", "farewell"]
    assert "turn_detection" not in str(payload)
    assert "transcription" not in session["audio"]


@pytest.mark.parametrize("field", ["model", "parallel_tool_calls"])
def test_session_echo_requires_expected_backend(settings, packet, field):
    bridge = bridge_for(settings)
    session = bridge.build_accept_payload(packet).session.model_dump()
    assert bridge.session_configuration_confirmed({"session": session})
    session["delegation"]["responses"][field] = "unexpected"
    assert not bridge.session_configuration_confirmed({"session": session})


def test_session_echo_rejects_wrong_voice_model(settings, packet):
    bridge = bridge_for(settings)
    session = bridge.build_accept_payload(packet).session.model_dump()
    session["model"] = "gpt-realtime-2.1"
    assert not bridge.session_configuration_confirmed({"session": session})


@pytest.mark.parametrize("key", [None, SecretStr(""), SecretStr("  ")])
def test_no_exa_omits_search_tool_and_search_instructions(settings, packet, key):
    settings.exa_api_key = key
    settings.ask_agent_enabled = True
    backend = bridge_for(settings).build_accept_payload(packet).session.delegation.responses
    assert "search_web" not in {tool.name for tool in backend.tools}
    assert "search_web" not in backend.instructions
    assert "Web search is unavailable" in backend.instructions
    assert "ask_agent" in {tool.name for tool in backend.tools}


def test_optional_owner_and_hold_tools(settings, packet):
    settings.ask_agent_enabled = settings.hold_detection_enabled = True
    backend = bridge_for(settings).build_accept_payload(packet).session.delegation.responses
    assert {"ask_agent", "report_hold"} <= {t.name for t in backend.tools}
    assert "never guess or invent the pending answer" in backend.instructions


@pytest.mark.parametrize(
    "env",
    [
        {"LIVE_MODEL": "gpt-realtime-2.1"},
        {"LIVE_BACKEND_MODEL": "gpt-realtime-mini"},
        {"LIVE_BACKEND_REASONING_EFFORT": "invalid"},
    ],
)
def test_configuration_cannot_switch_back_to_realtime(env):
    with pytest.raises(ValidationError):
        Settings.from_environ(env)
