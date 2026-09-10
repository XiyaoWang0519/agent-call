from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from twilio.base.exceptions import TwilioRestException

from app.openai_live import LiveBridge, LiveRuntime
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


class FailingSendWebSocket(FakeWebSocket):
    async def send(self, message: str) -> None:
        raise RuntimeError("socket send failed")


class BlockingFirstSendWebSocket(FakeWebSocket):
    def __init__(self):
        super().__init__()
        self.first_sent = asyncio.Event()
        self.release_first = asyncio.Event()

    async def send(self, message: str) -> None:
        await super().send(message)
        if len(self.messages) == 1:
            self.first_sent.set()
            await self.release_first.wait()


class SecondSendFailsWebSocket(FakeWebSocket):
    async def send(self, message: str) -> None:
        if self.messages:
            raise RuntimeError("second socket send failed")
        await super().send(message)


@pytest.mark.asyncio
async def test_send_hook_runs_after_success_and_cannot_change_send_outcome(settings):
    observed: list[tuple[str, str, int]] = []
    websocket = FakeWebSocket()

    async def hook(call_id: str, event: dict) -> None:
        # The underlying socket write must already have completed before this callback.
        observed.append((call_id, event["type"], len(websocket.messages)))
        if event["type"] == "response.cancel":
            raise RuntimeError("telemetry failed")

    bridge = LiveBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=_noop,
        on_fatal=_noop,
        on_send=hook,
    )
    bridge._runtime["call_1"] = LiveRuntime(
        call_id="call_1", openai_call_id="rtc_1", websocket=websocket
    )

    await bridge.send("call_1", {"type": "response.create"})
    await bridge.send("call_1", {"type": "response.cancel"})

    assert observed == [
        ("call_1", "response.create", 1),
        ("call_1", "response.cancel", 2),
    ]
    assert len(websocket.messages) == 2

    bridge._runtime["call_2"] = LiveRuntime(
        call_id="call_2", openai_call_id="rtc_2", websocket=FailingSendWebSocket()
    )
    with pytest.raises(RuntimeError, match="socket send failed"):
        await bridge.send("call_2", {"type": "response.create"})
    assert all(call_id == "call_1" for call_id, _, _ in observed)


class FakeParticipants:
    def __init__(self):
        self.creates: list[dict] = []
        self.updates: list[dict] = []

    def create(self, **kwargs):
        self.creates.append(kwargs)
        return SimpleNamespace(call_sid=f"CA{len(self.creates):032d}", conference_sid="CF1")

    def __call__(self, call_sid: str):
        parent = self

        class _Participant:
            def update(self, **kwargs):
                parent.updates.append({"call_sid": call_sid, **kwargs})
                return SimpleNamespace(call_sid=call_sid, muted=kwargs.get("muted", False))

            def delete(self):
                return None

        return _Participant()


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
    assert agent["early_media"] is False
    assert agent["muted"] is False
    assert agent["jitter_buffer_size"] == "small"
    assert agent["to"].startswith("sip:proj_test@sip.api.openai.com;transport=tls;secure=true?")
    assert "X-Plan-Id=plan_1" in agent["to"]
    assert "X-Bridge-Call-Id=call_1" in agent["to"]
    assert agent["conference_status_callback_event"] == [
        "start",
        "end",
        "join",
        "leave",
        "mute",
    ]

    await bridge.create_callee_participant(**common, conference_sid_or_name="CF1", packet=packet)
    callee = client.conferences("CF1").participants.creates[0]
    assert callee["label"] == "callee"
    assert callee["start_conference_on_enter"] is True
    assert callee["end_conference_on_exit"] is True
    assert callee["time_limit"] == settings.max_call_seconds
    assert callee["machine_detection"] == "DetectMessageEnd"
    assert callee["jitter_buffer_size"] == "small"
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
    assert owner["jitter_buffer_size"] == "small"

    await bridge.unmute_participant("CF1", "CA" + "a" * 32)
    await bridge.enable_end_conference_on_exit("CF1", "CA" + "c" * 32)
    assert client.conferences("CF1").participants.updates == [
        {"call_sid": "CA" + "a" * 32, "muted": False},
        {"call_sid": "CA" + "c" * 32, "end_conference_on_exit": True},
    ]


@pytest.mark.asyncio
async def test_complete_conference_treats_missing_resource_as_already_closed(settings):
    class MissingConference:
        participants = FakeParticipants()

        def update(self, **kwargs):
            del kwargs
            raise TwilioRestException(404, "/Conferences/CF-missing", "not found")

    client = SimpleNamespace(conferences=lambda _name: MissingConference())
    bridge = TwilioBridge(settings, client=client)

    await bridge.complete_conference("CF-missing")


def connected_bridge(settings, websocket=None):
    websocket = websocket or FakeWebSocket()
    bridge = LiveBridge(settings, SimpleNamespace(), on_event=_noop, on_open=_noop, on_fatal=_noop)
    runtime = LiveRuntime(call_id="call_1", openai_call_id="session_1", websocket=websocket)
    bridge._runtime["call_1"] = runtime
    return bridge, runtime, websocket


def collect(bridge, runtime, event, delegation="delegation_1"):
    bridge._observe_tool_batch(
        runtime, {"type": "response.event", "delegation_id": delegation, "event": event}
    )


def collect_tool(bridge, runtime, tool_id="tool_1"):
    collect(bridge, runtime, {"type": "response.created", "response": {"id": "resp_1"}})
    collect(
        bridge,
        runtime,
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": tool_id,
                "name": "search_web",
                "arguments": "{}",
            },
        },
    )


@pytest.mark.parametrize(
    "method",
    [
        "create_voicemail",
        "enable_conversation",
        "suspend_conversation",
        "notify_call_resumed",
    ],
)
async def test_voice_control_uses_native_context_append(settings, method):
    bridge, _, websocket = connected_bridge(settings)
    await getattr(bridge, method)("call_1")
    assert len(websocket.messages) == 1
    event = websocket.messages[0]
    assert event["type"] == "session.instructions.append"
    assert event["delegation_id"] is None
    assert 0 < len(event["content"].encode()) <= 500
    assert event["event_id"]
    assert "response" not in event


async def test_instruction_updates_reject_oversize_before_send(settings):
    bridge, _, websocket = connected_bridge(settings)
    with pytest.raises(ValueError):
        await bridge.append_instructions("call_1", "界" * 200)
    assert websocket.messages == []


async def test_tool_batch_waits_for_terminal_event_and_all_outputs(settings):
    bridge, runtime, websocket = connected_bridge(settings)
    collect_tool(bridge, runtime)
    collect_tool(bridge, runtime, "tool_2")
    await bridge.send_tool_result("call_1", "tool_1", {"ok": True})
    assert [m["type"] for m in websocket.messages] == ["response.item.create"]
    # Terminal responses have empty output; the earlier output items remain authoritative.
    collect(
        bridge, runtime, {"type": "response.completed", "response": {"id": "resp_1", "output": []}}
    )
    await bridge._continue_ready_batches(runtime)
    assert len(websocket.messages) == 1
    await bridge.send_tool_result("call_1", "tool_2", {"ok": True}, continue_response=False)
    assert [m["type"] for m in websocket.messages] == [
        "response.item.create",
        "response.item.create",
        "response.create",
    ]
    assert websocket.messages[-1] == {"type": "response.create"}
    assert "delivery_hint" in json.loads(websocket.messages[1]["item"]["output"])
    await bridge.send_tool_result("call_1", "tool_2", {"ok": True})
    await bridge._continue_ready_batches(runtime)
    assert len(websocket.messages) == 3


async def test_all_results_before_completion_continue_only_after_completion(settings):
    bridge, runtime, websocket = connected_bridge(settings)
    collect_tool(bridge, runtime)
    await bridge.send_tool_result("call_1", "tool_1", {"ok": True})
    assert len(websocket.messages) == 1
    collect(
        bridge, runtime, {"type": "response.completed", "response": {"id": "resp_1", "output": []}}
    )
    await bridge._continue_ready_batches(runtime)
    assert websocket.messages[-1] == {"type": "response.create"}


async def test_arguments_done_does_not_authorize_a_tool_result(settings):
    bridge, runtime, websocket = connected_bridge(settings)
    collect(
        bridge,
        runtime,
        {"type": "response.function_call_arguments.done", "call_id": "tool_1", "arguments": "{}"},
    )
    with pytest.raises(RuntimeError, match="does not belong"):
        await bridge.send_tool_result("call_1", "tool_1", {"ok": True})
    assert not websocket.messages


async def test_result_send_failure_remains_retryable(settings):
    bridge, runtime, _ = connected_bridge(settings, FailingSendWebSocket())
    collect_tool(bridge, runtime)
    with pytest.raises(RuntimeError):
        await bridge.send_tool_result("call_1", "tool_1", {"ok": True})
    assert not runtime.emitted_results
    runtime.websocket = FakeWebSocket()
    await bridge.send_tool_result("call_1", "tool_1", {"ok": True})
    assert runtime.emitted_results == {"tool_1"}


async def test_continuation_failure_does_not_resend_accepted_result(settings):
    bridge, runtime, websocket = connected_bridge(settings, SecondSendFailsWebSocket())
    collect_tool(bridge, runtime)
    collect(
        bridge, runtime, {"type": "response.completed", "response": {"id": "resp_1", "output": []}}
    )
    with pytest.raises(RuntimeError):
        await bridge.send_tool_result("call_1", "tool_1", {"ok": True})
    assert runtime.emitted_results == {"tool_1"}
    assert not runtime.continued_responses
    runtime.websocket = FakeWebSocket()
    await bridge._continue_ready_batches(runtime)
    assert runtime.websocket.messages == [{"type": "response.create"}]
    assert len(websocket.messages) == 1


async def test_cancelled_send_waiting_for_lock_is_never_written(settings):
    bridge, runtime, websocket = connected_bridge(settings)
    await runtime.send_lock.acquire()
    task = asyncio.create_task(bridge.send("call_1", {"type": "session.close"}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runtime.send_lock.release()
    assert not websocket.messages


async def test_readiness_only_updates_supported_responses_fields(settings):
    bridge, runtime, websocket = connected_bridge(settings)
    task = asyncio.create_task(bridge.verify_initial_session("call_1"))
    await websocket.sent.wait()
    event = websocket.messages[0]
    assert event["type"] == "session.update"
    assert event["session"] == {
        "delegation": {"type": "responses", "responses": {"tool_choice": "auto"}}
    }
    assert runtime.update_event_id == event["event_id"]
    expected = {
        "type": "session.updated",
        "client_event_id": event["event_id"],
        "session": {"model": "gpt-live-1"},
    }
    runtime.update_waiter.set_result(expected)
    assert await task == expected
    assert runtime.update_waiter is None


async def test_cancellation_after_write_finishes_bookkeeping_and_continuation(settings):
    bridge, runtime, websocket = connected_bridge(settings, BlockingFirstSendWebSocket())
    collect_tool(bridge, runtime)
    collect(
        bridge, runtime, {"type": "response.completed", "response": {"id": "resp_1", "output": []}}
    )
    task = asyncio.create_task(bridge.send_tool_result("call_1", "tool_1", {"ok": True}))
    await websocket.first_sent.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    websocket.release_first.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.emitted_results == {"tool_1"}
    assert runtime.continued_responses == {"resp_1"}
    await bridge.send_tool_result("call_1", "tool_1", {"ok": True})
    assert [m["type"] for m in websocket.messages] == ["response.item.create", "response.create"]


async def test_cancelled_tool_result_waiting_for_ownership_is_not_sent(settings):
    bridge, runtime, websocket = connected_bridge(settings)
    collect_tool(bridge, runtime)
    await runtime.tool_lock.acquire()
    task = asyncio.create_task(bridge.send_tool_result("call_1", "tool_1", {"ok": True}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runtime.tool_lock.release()
    assert not websocket.messages
    assert not runtime.emitted_results
