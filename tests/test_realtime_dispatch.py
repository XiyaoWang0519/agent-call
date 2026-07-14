from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from app.openai_realtime import (
    REALTIME_EVENT_QUEUE_MAXSIZE,
    RealtimeBridge,
    RealtimeRuntime,
)


class QueueWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.messages: list[dict[str, Any]] = []
        self.connected = asyncio.Event()
        self.sent = asyncio.Event()
        self.received: list[dict[str, Any]] = []
        self.closed = False

    def __aiter__(self) -> QueueWebSocket:
        return self

    async def __anext__(self) -> str:
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        event = json.loads(message)
        self.received.append(event)
        return message

    async def feed(self, event: dict[str, Any]) -> None:
        await self.incoming.put(json.dumps(event))

    async def send(self, message: str) -> None:
        self.messages.append(json.loads(message))
        self.sent.set()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.incoming.put(None)


class FirstCloseFailsWebSocket(QueueWebSocket):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.close_attempts = 0

    async def close(self) -> None:
        self.close_attempts += 1
        if self.close_attempts == 1:
            if self.mode == "raises":
                raise RuntimeError("close failed")
            await asyncio.Event().wait()
        await super().close()


class FakeConnection:
    def __init__(self, websocket: QueueWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> QueueWebSocket:
        self.websocket.connected.set()
        return self.websocket

    async def __aexit__(self, *args: object) -> None:
        await self.websocket.close()


async def _noop(*args: object, **kwargs: object) -> None:
    return None


async def _start_bridge(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    call_id: str,
    websocket: QueueWebSocket,
    on_event: Callable[[str, dict[str, Any]], Awaitable[None]],
    on_open: Callable[[str], Awaitable[None]] = _noop,
) -> tuple[
    RealtimeBridge,
    RealtimeRuntime,
    asyncio.Task[None],
    list[tuple[str, str]],
    asyncio.Event,
]:
    fatals: list[tuple[str, str]] = []
    fatal_called = asyncio.Event()

    async def on_fatal(failed_call_id: str, reason: str) -> None:
        fatals.append((failed_call_id, reason))
        fatal_called.set()

    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=on_event,
        on_open=on_open,
        on_fatal=on_fatal,
    )
    runtime = RealtimeRuntime(call_id=call_id, openai_call_id=f"rtc_{call_id}")
    bridge._runtime[call_id] = runtime
    monkeypatch.setattr(
        "app.openai_realtime.websockets.connect",
        lambda *args, **kwargs: FakeConnection(websocket),
    )
    task = asyncio.create_task(bridge._run(runtime), name=f"test-sideband:{call_id}")
    runtime.task = task
    await asyncio.wait_for(websocket.connected.wait(), timeout=1)
    await asyncio.sleep(0)
    return bridge, runtime, task, fatals, fatal_called


@pytest.mark.asyncio
async def test_reader_continues_while_first_application_event_is_blocked(settings, monkeypatch):
    websocket = QueueWebSocket()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    handled: list[int] = []

    async def on_event(call_id: str, event: dict[str, Any]) -> None:
        handled.append(event["sequence"])
        if event["sequence"] == 1:
            first_started.set()
            await release_first.wait()

    _, runtime, task, fatals, _ = await _start_bridge(
        settings,
        monkeypatch,
        call_id="call_read_ahead",
        websocket=websocket,
        on_event=on_event,
    )

    await websocket.feed({"type": "test.event", "sequence": 1})
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await websocket.feed({"type": "test.event", "sequence": 2})
    for _ in range(10):
        if runtime.event_queue.qsize() == 1:
            break
        await asyncio.sleep(0)

    assert [event["sequence"] for event in websocket.received] == [1, 2]
    assert runtime.event_queue.qsize() == 1

    release_first.set()
    await websocket.close()
    await asyncio.wait_for(task, timeout=1)
    assert handled == [1, 2]
    assert fatals == []


@pytest.mark.asyncio
async def test_session_updated_readiness_bypasses_blocked_dispatcher(settings, monkeypatch):
    websocket = QueueWebSocket()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    handled_types: list[str] = []

    async def on_event(call_id: str, event: dict[str, Any]) -> None:
        handled_types.append(event["type"])
        if event["type"] == "test.block":
            first_started.set()
            await release_first.wait()

    bridge, _, task, fatals, _ = await _start_bridge(
        settings,
        monkeypatch,
        call_id="call_readiness",
        websocket=websocket,
        on_event=on_event,
    )
    await websocket.feed({"type": "test.block"})
    await asyncio.wait_for(first_started.wait(), timeout=1)

    update = asyncio.create_task(bridge.verify_initial_session("call_readiness"))
    await asyncio.wait_for(websocket.sent.wait(), timeout=1)
    echoed = {"type": "session.updated", "session": {"audio": {"input": {}}}}
    await websocket.feed(echoed)

    assert await asyncio.wait_for(update, timeout=1) == echoed
    assert handled_types == ["test.block"]

    release_first.set()
    await websocket.close()
    await asyncio.wait_for(task, timeout=1)
    assert handled_types == ["test.block", "session.updated"]
    assert fatals == []


@pytest.mark.asyncio
async def test_on_open_can_drain_production_runtime_without_parent_child_cycle(
    settings, monkeypatch
):
    monkeypatch.setattr("app.openai_realtime.REALTIME_MEDIA_DRAIN_SECONDS", 0)
    monkeypatch.setattr("app.openai_realtime.REALTIME_CLOSE_TIMEOUT_SECONDS", 0.1)
    websocket = QueueWebSocket()
    fatals: list[tuple[str, str]] = []
    drain_returned = asyncio.Event()
    bridge_holder: dict[str, RealtimeBridge] = {}

    async def on_open(call_id: str) -> None:
        await bridge_holder["bridge"].drain_and_close(call_id)
        drain_returned.set()

    async def on_fatal(call_id: str, reason: str) -> None:
        fatals.append((call_id, reason))

    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=on_open,
        on_fatal=on_fatal,
    )
    bridge_holder["bridge"] = bridge
    runtime = RealtimeRuntime(call_id="call_open_drain", openai_call_id="rtc_open_drain")
    bridge._runtime[runtime.call_id] = runtime
    monkeypatch.setattr(
        "app.openai_realtime.websockets.connect",
        lambda *args, **kwargs: FakeConnection(websocket),
    )
    runtime.task = asyncio.create_task(bridge._run(runtime), name="sideband:call_open_drain")

    await asyncio.wait_for(runtime.task, timeout=1)

    assert drain_returned.is_set()
    assert runtime.open_task is not None and runtime.open_task.done()
    assert runtime.receiver_task is not None and runtime.receiver_task.done()
    assert runtime.dispatcher_task is not None and runtime.dispatcher_task.done()
    assert runtime.call_id not in bridge._runtime
    assert fatals == []


@pytest.mark.asyncio
@pytest.mark.parametrize("close_mode", ["raises", "times_out"])
async def test_child_close_failure_wakes_supervisor_and_clears_runtime(
    settings, monkeypatch, close_mode
):
    monkeypatch.setattr("app.openai_realtime.REALTIME_MEDIA_DRAIN_SECONDS", 0)
    monkeypatch.setattr("app.openai_realtime.REALTIME_CLOSE_TIMEOUT_SECONDS", 0.01)
    websocket = FirstCloseFailsWebSocket(close_mode)
    fatals: list[tuple[str, str]] = []
    bridge_holder: dict[str, RealtimeBridge] = {}
    after_drain = asyncio.Event()

    async def on_open(call_id: str) -> None:
        await bridge_holder["bridge"].drain_and_close(call_id)
        # Production termination persists terminal state and schedules finalization here.
        after_drain.set()

    async def on_fatal(call_id: str, reason: str) -> None:
        fatals.append((call_id, reason))

    bridge = RealtimeBridge(
        settings,
        SimpleNamespace(),
        on_event=_noop,
        on_open=on_open,
        on_fatal=on_fatal,
    )
    bridge_holder["bridge"] = bridge
    runtime = RealtimeRuntime(call_id=f"call_close_{close_mode}", openai_call_id="rtc_close")
    bridge._runtime[runtime.call_id] = runtime
    monkeypatch.setattr(
        "app.openai_realtime.websockets.connect",
        lambda *args, **kwargs: FakeConnection(websocket),
    )
    runtime.task = asyncio.create_task(bridge._run(runtime), name=f"sideband:{runtime.call_id}")

    await asyncio.wait_for(runtime.task, timeout=0.5)

    assert websocket.close_attempts == 2
    assert after_drain.is_set()
    assert runtime.open_task is not None and runtime.open_task.done()
    assert runtime.open_task.exception() is None
    assert runtime.receiver_task is not None and runtime.receiver_task.done()
    assert runtime.dispatcher_task is not None and runtime.dispatcher_task.done()
    assert runtime.call_id not in bridge._runtime
    assert fatals == []


@pytest.mark.asyncio
async def test_dispatcher_close_failure_finishes_current_handler_before_cleanup(
    settings, monkeypatch
):
    monkeypatch.setattr("app.openai_realtime.REALTIME_MEDIA_DRAIN_SECONDS", 0)
    monkeypatch.setattr("app.openai_realtime.REALTIME_CLOSE_TIMEOUT_SECONDS", 0.01)
    websocket = FirstCloseFailsWebSocket("raises")
    handler_steps: list[str] = []
    bridge: RealtimeBridge

    async def on_event(call_id: str, event: dict[str, Any]) -> None:
        handler_steps.append("before_drain")
        await bridge.drain_and_close(call_id)
        # This models terminal state persistence/finalizer scheduling in terminate_call.
        handler_steps.append("after_drain")

    bridge, runtime, task, fatals, _ = await _start_bridge(
        settings,
        monkeypatch,
        call_id="call_dispatcher_close_failure",
        websocket=websocket,
        on_event=on_event,
    )
    await websocket.feed({"type": "test.terminate"})

    await asyncio.wait_for(task, timeout=0.5)

    assert handler_steps == ["before_drain", "after_drain"]
    assert websocket.close_attempts == 2
    assert runtime.dispatcher_task is not None and runtime.dispatcher_task.done()
    assert runtime.receiver_task is not None and runtime.receiver_task.done()
    assert runtime.call_id not in bridge._runtime
    assert fatals == []


@pytest.mark.asyncio
async def test_external_drain_waits_for_queued_events_before_cancel_fallback(settings, monkeypatch):
    monkeypatch.setattr("app.openai_realtime.REALTIME_MEDIA_DRAIN_SECONDS", 0.01)
    monkeypatch.setattr("app.openai_realtime.REALTIME_CLOSE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr("app.openai_realtime.REALTIME_TASK_DRAIN_TIMEOUT_SECONDS", 0.25)
    monkeypatch.setattr("app.openai_realtime.REALTIME_TASK_CANCEL_TIMEOUT_SECONDS", 0.1)
    websocket = QueueWebSocket()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    handled: list[int] = []

    async def on_event(call_id: str, event: dict[str, Any]) -> None:
        handled.append(event["sequence"])
        if event["sequence"] == 1:
            first_started.set()
            await release_first.wait()

    bridge, runtime, _, fatals, _ = await _start_bridge(
        settings,
        monkeypatch,
        call_id="call_external_drain",
        websocket=websocket,
        on_event=on_event,
    )
    await websocket.feed({"type": "test.event", "sequence": 1})
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await websocket.feed({"type": "test.event", "sequence": 2})
    for _ in range(10):
        if runtime.event_queue.qsize() == 1:
            break
        await asyncio.sleep(0)
    assert runtime.event_queue.qsize() == 1

    async def release_during_supervised_drain() -> None:
        # Release after the websocket has closed. The old immediate-cancel behavior dropped the
        # second event here; the supervised grace window lets both FIFO entries complete.
        await asyncio.sleep(0.03)
        release_first.set()

    release_task = asyncio.create_task(release_during_supervised_drain())
    await asyncio.wait_for(bridge.drain_and_close(runtime.call_id), timeout=1)
    await release_task

    assert handled == [1, 2]
    assert runtime.task is not None and runtime.task.done()
    assert runtime.call_id not in bridge._runtime
    assert fatals == []


@pytest.mark.asyncio
async def test_dispatcher_preserves_fifo_order(settings, monkeypatch):
    websocket = QueueWebSocket()
    handled: list[int] = []

    async def on_event(call_id: str, event: dict[str, Any]) -> None:
        handled.append(event["sequence"])
        await asyncio.sleep(0)

    _, runtime, task, fatals, _ = await _start_bridge(
        settings,
        monkeypatch,
        call_id="call_fifo",
        websocket=websocket,
        on_event=on_event,
    )
    for sequence in range(25):
        await websocket.feed({"type": "test.event", "sequence": sequence})
    await websocket.close()

    await asyncio.wait_for(task, timeout=1)
    assert handled == list(range(25))
    assert runtime.receiver_task is not None and runtime.receiver_task.done()
    assert runtime.dispatcher_task is not None and runtime.dispatcher_task.done()
    assert fatals == []


@pytest.mark.asyncio
async def test_queue_overflow_is_fatal_instead_of_dropping_events(settings, monkeypatch):
    websocket = QueueWebSocket()
    first_started = asyncio.Event()
    never_release = asyncio.Event()

    async def on_event(call_id: str, event: dict[str, Any]) -> None:
        first_started.set()
        await never_release.wait()

    _, runtime, task, fatals, fatal_called = await _start_bridge(
        settings,
        monkeypatch,
        call_id="call_overflow",
        websocket=websocket,
        on_event=on_event,
    )
    await websocket.feed({"type": "test.block", "sequence": 0})
    await asyncio.wait_for(first_started.wait(), timeout=1)
    for sequence in range(1, REALTIME_EVENT_QUEUE_MAXSIZE + 2):
        await websocket.feed({"type": "test.event", "sequence": sequence})

    await asyncio.wait_for(fatal_called.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=1)
    assert fatals == [("call_overflow", "sideband_error:RealtimeEventQueueOverflow")]
    assert runtime.dispatcher_task is not None and runtime.dispatcher_task.cancelled()


@pytest.mark.asyncio
async def test_handler_failure_is_fatal_and_cancels_reader(settings, monkeypatch):
    class HandlerFailure(RuntimeError):
        pass

    websocket = QueueWebSocket()

    async def on_event(call_id: str, event: dict[str, Any]) -> None:
        raise HandlerFailure("handler failed")

    _, runtime, task, fatals, fatal_called = await _start_bridge(
        settings,
        monkeypatch,
        call_id="call_handler_failure",
        websocket=websocket,
        on_event=on_event,
    )
    await websocket.feed({"type": "test.event"})

    await asyncio.wait_for(fatal_called.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=1)
    assert fatals == [("call_handler_failure", "sideband_error:HandlerFailure")]
    assert runtime.receiver_task is not None and runtime.receiver_task.cancelled()
    assert runtime.dispatcher_task is not None and runtime.dispatcher_task.done()
