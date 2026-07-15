from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from twilio.base.exceptions import TwilioRestException

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


class StreamingFakeWebSocket(FakeWebSocket):
    def __init__(self, echo: dict):
        super().__init__()
        self.echo = echo
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send(self, message: str) -> None:
        await super().send(message)
        await self.incoming.put(json.dumps(self.echo))

    async def close(self) -> None:
        if not self.closed:
            await self.incoming.put(None)
        await super().close()


class FakeConnection:
    def __init__(self, websocket: StreamingFakeWebSocket):
        self.websocket = websocket

    async def __aenter__(self) -> StreamingFakeWebSocket:
        return self.websocket

    async def __aexit__(self, *args) -> None:
        await self.websocket.close()


@pytest.mark.asyncio
async def test_opening_response_uses_session_context_without_a_script(settings):
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

    await bridge.create_opening("call_1")

    assert websocket.messages == [
        {
            "type": "response.create",
            "response": {"output_modalities": ["audio"]},
        }
    ]


@pytest.mark.asyncio
async def test_send_hook_runs_after_success_and_cannot_change_send_outcome(settings):
    observed: list[tuple[str, str, int]] = []
    websocket = FakeWebSocket()

    async def hook(call_id: str, event: dict) -> None:
        # The underlying socket write must already have completed before this callback.
        observed.append((call_id, event["type"], len(websocket.messages)))
        if event["type"] == "response.cancel":
            raise RuntimeError("telemetry failed")

    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=_noop,
        on_fatal=_noop,
        on_send=hook,
    )
    bridge._runtime["call_1"] = RealtimeRuntime(
        call_id="call_1", openai_call_id="rtc_1", websocket=websocket
    )

    await bridge.send("call_1", {"type": "response.create"})
    await bridge.send("call_1", {"type": "response.cancel"})

    assert observed == [
        ("call_1", "response.create", 1),
        ("call_1", "response.cancel", 2),
    ]
    assert len(websocket.messages) == 2

    bridge._runtime["call_2"] = RealtimeRuntime(
        call_id="call_2", openai_call_id="rtc_2", websocket=FailingSendWebSocket()
    )
    with pytest.raises(RuntimeError, match="socket send failed"):
        await bridge.send("call_2", {"type": "response.create"})
    assert all(call_id == "call_1" for call_id, _, _ in observed)


@pytest.mark.asyncio
async def test_voicemail_prompt_does_not_script_identity_or_callback_wording(settings):
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

    await bridge.create_voicemail("call_1")

    instructions = websocket.messages[0]["response"]["instructions"]
    assert "approved context" in instructions
    assert "Poke" not in instructions
    assert "AI assistant" not in instructions
    assert "callback" not in instructions


def test_session_prompt_leaves_opening_identity_and_wording_to_poke(settings, packet):
    instructions = (
        RealtimeBridge(
            settings,
            SimpleNamespace(),
            on_event=_noop,
            on_open=_noop,
            on_fatal=_noop,
        )
        .build_accept_payload(packet)
        .instructions
    )

    assert "Choose how to open the call from the approved context" in instructions
    assert "# Identity and disclosure" not in instructions
    assert "Always identify yourself" not in instructions
    assert "You are Poke" not in instructions
    assert "Am I speaking with" not in instructions


@pytest.mark.asyncio
async def test_sideband_receives_initial_update_echo_while_open_handler_waits(
    settings, monkeypatch
):
    echoed = {
        "type": "session.updated",
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
        },
    }
    websocket = StreamingFakeWebSocket(echoed)
    opened = asyncio.Event()
    fatals: list[tuple[str, str]] = []
    bridge: RealtimeBridge

    async def on_open(call_id: str) -> None:
        updated = await bridge.verify_initial_session(call_id)
        assert updated == echoed
        opened.set()
        await websocket.close()

    async def on_fatal(call_id: str, reason: str) -> None:
        fatals.append((call_id, reason))

    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=on_open,
        on_fatal=on_fatal,
    )
    runtime = RealtimeRuntime(call_id="call_1", openai_call_id="rtc_1")
    bridge._runtime["call_1"] = runtime
    monkeypatch.setattr(
        "app.openai_realtime.websockets.connect", lambda *args, **kwargs: FakeConnection(websocket)
    )

    await asyncio.wait_for(bridge._run(runtime), timeout=1)

    assert opened.is_set()
    assert fatals == []


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
                        # Transcription must be re-asserted: session.update replaces the
                        # nested audio.input object, so omitting it would drop callee
                        # transcription for the rest of the call.
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": "auto",
                            "create_response": True,
                            "interrupt_response": True,
                        },
                    }
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_initial_session_update_reasserts_safe_audio_gate(settings):
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

    pending = asyncio.create_task(bridge.verify_initial_session("call_1"))
    await asyncio.wait_for(websocket.sent.wait(), timeout=1)
    echoed = {
        "type": "session.updated",
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
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": "auto",
                            "create_response": False,
                            "interrupt_response": False,
                        },
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


@pytest.mark.asyncio
async def test_tool_output_and_continuation_cannot_be_interleaved(settings):
    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=_noop,
        on_fatal=_noop,
    )
    websocket = BlockingFirstSendWebSocket()
    bridge._runtime["call_1"] = RealtimeRuntime(
        call_id="call_1", openai_call_id="rtc_1", websocket=websocket
    )

    tool_result = asyncio.create_task(
        bridge.send_tool_result("call_1", "tool_1", {"accepted": True})
    )
    await asyncio.wait_for(websocket.first_sent.wait(), timeout=1)
    competing_send = asyncio.create_task(bridge.send("call_1", {"type": "response.cancel"}))
    await asyncio.sleep(0)

    assert [message["type"] for message in websocket.messages] == ["conversation.item.create"]

    websocket.release_first.set()
    await asyncio.gather(tool_result, competing_send)
    assert [message["type"] for message in websocket.messages] == [
        "conversation.item.create",
        "response.create",
        "response.cancel",
    ]


@pytest.mark.asyncio
async def test_cancellation_after_first_frame_finishes_atomic_tool_pair(settings):
    observed: list[str] = []

    async def on_send(call_id: str, event: dict) -> None:
        observed.append(event["type"])

    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=_noop,
        on_fatal=_noop,
        on_send=on_send,
    )
    websocket = BlockingFirstSendWebSocket()
    bridge._runtime["call_1"] = RealtimeRuntime(
        call_id="call_1", openai_call_id="rtc_1", websocket=websocket
    )

    tool_result = asyncio.create_task(
        bridge.send_tool_result("call_1", "tool_1", {"accepted": True})
    )
    await asyncio.wait_for(websocket.first_sent.wait(), timeout=1)
    competing_send = asyncio.create_task(bridge.send("call_1", {"type": "response.cancel"}))
    tool_result.cancel()
    await asyncio.sleep(0)

    assert tool_result.done() is False
    assert competing_send.done() is False
    assert [message["type"] for message in websocket.messages] == ["conversation.item.create"]

    websocket.release_first.set()
    with pytest.raises(asyncio.CancelledError):
        await tool_result
    assert observed[:2] == ["conversation.item.create", "response.create"]

    await competing_send
    assert [message["type"] for message in websocket.messages] == [
        "conversation.item.create",
        "response.create",
        "response.cancel",
    ]


@pytest.mark.asyncio
async def test_cancelled_single_frame_waiting_for_lock_is_never_sent(settings):
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
    await runtime.send_lock.acquire()

    pending = asyncio.create_task(bridge.send("call_1", {"type": "response.cancel"}))
    await asyncio.sleep(0)
    assert pending.done() is False
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    runtime.send_lock.release()
    await asyncio.sleep(0)
    assert websocket.messages == []


@pytest.mark.asyncio
async def test_cancelled_tool_pair_waiting_for_lock_is_never_sent(settings):
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
    await runtime.send_lock.acquire()

    pending = asyncio.create_task(bridge.send_tool_result("call_1", "tool_1", {"accepted": True}))
    await asyncio.sleep(0)
    assert pending.done() is False
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, timeout=0.1)

    runtime.send_lock.release()
    await asyncio.sleep(0)
    assert websocket.messages == []


@pytest.mark.asyncio
async def test_partial_tool_result_send_notifies_only_successful_frames(settings):
    observed: list[str] = []

    async def on_send(call_id: str, event: dict) -> None:
        observed.append(event["type"])

    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=_noop,
        on_fatal=_noop,
        on_send=on_send,
    )
    websocket = SecondSendFailsWebSocket()
    bridge._runtime["call_1"] = RealtimeRuntime(
        call_id="call_1", openai_call_id="rtc_1", websocket=websocket
    )

    with pytest.raises(RuntimeError, match="second socket send failed"):
        await bridge.send_tool_result("call_1", "tool_1", {"accepted": True})

    assert [message["type"] for message in websocket.messages] == ["conversation.item.create"]
    assert observed == ["conversation.item.create"]


@pytest.mark.asyncio
async def test_terminal_function_output_creates_a_dedicated_closing_response(settings):
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
    await bridge.send_tool_result(
        "call_1",
        "tool_1",
        {"accepted": True},
        continuation_instructions="Say one concise goodbye. Do not call any function.",
    )
    assert websocket.messages == [
        {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": "tool_1",
                "output": '{"accepted": true}',
            },
        },
        {
            "type": "response.create",
            "response": {
                "output_modalities": ["audio"],
                "instructions": "Say one concise goodbye. Do not call any function.",
            },
        },
    ]


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
    assert agent["to"].startswith("sip:proj_test@sip.api.openai.com;transport=tls?")
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
