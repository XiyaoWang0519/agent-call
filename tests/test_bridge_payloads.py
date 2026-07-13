from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.openai_realtime import RealtimeBridge, RealtimeRuntime
from app.twilio_bridge import TwilioBridge


async def _noop(*args, **kwargs) -> None:
    return None


class FakeWebSocket:
    def __init__(self):
        self.messages: list[dict] = []
        self.sent = asyncio.Event()
        self.closed = False

    async def send(self, message: str) -> None:
        self.messages.append(json.loads(message))
        self.sent.set()

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_activation_update_is_serialized_and_waits_for_echo(settings):
    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=_noop,
        on_fatal=_noop,
    )
    websocket = FakeWebSocket()
    runtime = RealtimeRuntime(call_id="call_1", openai_call_id="rtc_1", websocket=websocket)
    bridge._runtime["call_1"] = runtime

    pending = asyncio.create_task(bridge.enable_automatic_responses("call_1"))
    await asyncio.wait_for(websocket.sent.wait(), timeout=1)
    echoed = {
        "type": "session.updated",
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
        },
    }
    runtime.update_waiter.set_result(echoed)
    assert await pending == echoed
    assert websocket.messages == [
        {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "audio": {
                    "input": {
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": "auto",
                            "create_response": True,
                            "interrupt_response": True,
                        }
                    }
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_function_output_precedes_manual_continuation(settings):
    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=_noop,
        on_fatal=_noop,
    )
    websocket = FakeWebSocket()
    bridge._runtime["call_1"] = RealtimeRuntime(
        call_id="call_1", openai_call_id="rtc_1", websocket=websocket
    )
    await bridge.send_tool_result("call_1", "tool_1", {"accepted": True})
    assert websocket.messages == [
        {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": "tool_1",
                "output": '{"accepted": true}',
            },
        },
        {"type": "response.create"},
    ]


class FakeParticipants:
    def __init__(self):
        self.creates: list[dict] = []

    def create(self, **kwargs):
        self.creates.append(kwargs)
        return SimpleNamespace(call_sid=f"CA{len(self.creates):032d}", conference_sid="CF1")


class FakeConference:
    def __init__(self):
        self.participants = FakeParticipants()


class FakeConferences:
    def __init__(self):
        self.by_name: dict[str, FakeConference] = {}

    def __call__(self, name: str) -> FakeConference:
        return self.by_name.setdefault(name, FakeConference())


@pytest.mark.asyncio
async def test_twilio_participant_options_match_bridge_contract(settings, packet):
    client = SimpleNamespace(conferences=FakeConferences())
    bridge = TwilioBridge(settings, client=client)
    common = {"call_id": "call_1", "plan_id": "plan_1"}

    await bridge.create_agent_participant(**common, conference_name="conference_1")
    agent = client.conferences("conference_1").participants.creates[0]
    assert agent["label"] == "agent"
    assert agent["start_conference_on_enter"] is False
    assert agent["end_conference_on_exit"] is False
    assert agent["time_limit"] == 720
    assert agent["wait_url"] == ""
    assert agent["to"].startswith("sip:proj_test@sip.api.openai.com;transport=tls?")
    assert "X-Plan-Id=plan_1" in agent["to"]
    assert "X-Bridge-Call-Id=call_1" in agent["to"]

    await bridge.create_callee_participant(**common, conference_sid_or_name="CF1", packet=packet)
    callee = client.conferences("CF1").participants.creates[0]
    assert callee["label"] == "callee"
    assert callee["start_conference_on_enter"] is True
    assert callee["end_conference_on_exit"] is True
    assert callee["time_limit"] == settings.max_call_seconds
    assert callee["machine_detection"] == "DetectMessageEnd"
    assert callee["amd_status_callback_method"] == "POST"
    assert "plan_id=plan_1" in callee["amd_status_callback"]

    await bridge.create_owner_participant(
        **common,
        conference_sid_or_name="CF1",
        owner_phone=settings.owner_phone_e164,
    )
    owner = client.conferences("CF1").participants.creates[1]
    assert owner["label"] == "owner"
    assert owner["end_conference_on_exit"] is False
    assert owner["timeout"] == 30
