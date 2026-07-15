from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

from app.models import ContextPacket, RealtimeSessionConfig
from app.prompts import realtime_instructions
from app.settings import Settings

logger = logging.getLogger(__name__)

EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
OpenHandler = Callable[[str], Awaitable[None]]
FatalHandler = Callable[[str, str], Awaitable[None]]
SendHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
ActivityHandler = Callable[[str], None]

# Realtime events are normally consumed as quickly as they arrive. A bounded queue protects a
# call from unbounded memory growth if application handling stalls while still leaving ample room
# for short bursts of audio/transcript events.
REALTIME_EVENT_QUEUE_MAXSIZE = 512
REALTIME_MEDIA_DRAIN_SECONDS = 1.5
REALTIME_CLOSE_TIMEOUT_SECONDS = 2.5
REALTIME_TASK_DRAIN_TIMEOUT_SECONDS = 2.0
REALTIME_TASK_CANCEL_TIMEOUT_SECONDS = 1.0
REALTIME_SEND_TIMEOUT_SECONDS = 10.0
REALTIME_SHUTDOWN_FINAL_TIMEOUT_SECONDS = 10.0
_EVENT_QUEUE_CLOSED = object()
_LOGGED_REALTIME_EVENT_TYPES = {
    "session.updated",
    "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_stopped",
    "input_audio_buffer.committed",
    "conversation.item.input_audio_transcription.completed",
    "response.created",
    "response.output_audio.done",
    "response.output_audio_transcript.done",
    "response.done",
    "error",
}


class RealtimeEventQueueOverflow(RuntimeError):
    """Raised when application event handling cannot keep up with the sideband stream."""


@dataclass(slots=True)
class RealtimeRuntime:
    call_id: str
    xai_call_id: str
    packet: ContextPacket | None = None
    websocket: Any | None = None
    task: asyncio.Task[None] | None = None
    connected_waiter: asyncio.Future[int] | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    update_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    update_waiter: asyncio.Future[dict[str, Any]] | None = None
    event_queue: asyncio.Queue[Any] = field(
        default_factory=lambda: asyncio.Queue(maxsize=REALTIME_EVENT_QUEUE_MAXSIZE)
    )
    open_task: asyncio.Task[None] | None = None
    receiver_task: asyncio.Task[None] | None = None
    dispatcher_task: asyncio.Task[None] | None = None
    closing: bool = False
    stop_after_current: bool = False


class RealtimeBridge:
    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        *,
        on_event: EventHandler,
        on_open: OpenHandler,
        on_fatal: FatalHandler,
        on_send: SendHandler | None = None,
        on_activity: ActivityHandler | None = None,
    ):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            base_url="https://api.x.ai/v1",
            headers={"Authorization": f"Bearer {Settings.reveal(settings.xai_api_key)}"},
            timeout=httpx.Timeout(
                settings.xai_http_timeout_seconds,
                connect=settings.xai_connect_timeout_seconds,
            ),
        )
        self.on_event = on_event
        self.on_open = on_open
        self.on_fatal = on_fatal
        self.on_send = on_send
        self.on_activity = on_activity
        self._runtime: dict[str, RealtimeRuntime] = {}

    def build_session_config(self, packet: ContextPacket) -> RealtimeSessionConfig:
        return RealtimeSessionConfig(
            instructions=realtime_instructions(packet),
            audio={
                "input": {"transcription": self._transcription_config()},
                "output": {"speed": 1.0},
            },
            tools=[
                {
                    "type": "function",
                    "name": "transfer_to_owner",
                    "description": "Transfer only when the owner must personally take over.",
                    "parameters": {
                        "type": "object",
                        "properties": {"reason": {"type": "string"}},
                        "required": ["reason"],
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "record_call_outcome",
                    "description": "Advisory summary of explicit outcomes near the end of the call.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string"},
                            "summary": {"type": "string"},
                            "commitments": {"type": "array", "items": {"type": "string"}},
                            "followUps": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["status", "summary", "commitments", "followUps"],
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "end_call",
                    "description": (
                        "Request the end of the phone call once the conversation is truly "
                        "finished: the objective is resolved and the callee has nothing further. "
                        "If the callee just asked a question or made a request, answer it fully "
                        "as a normal turn before calling this. Once finished, call this promptly "
                        "instead of waiting for the callee or outer client to hang up. After it "
                        "succeeds, you will be prompted to say the final goodbye."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {
                                "type": "string",
                                "enum": [
                                    "objective_completed",
                                    "callee_declined",
                                    "wrong_number",
                                    "unable_to_complete",
                                    "out_of_scope",
                                ],
                            }
                        },
                        "required": ["reason"],
                        "additionalProperties": False,
                    },
                },
            ],
        )

    async def connect(
        self,
        *,
        call_id: str,
        xai_call_id: str,
        packet: ContextPacket,
    ) -> int:
        runtime = RealtimeRuntime(call_id=call_id, xai_call_id=xai_call_id, packet=packet)
        runtime.connected_waiter = asyncio.get_running_loop().create_future()
        self._runtime[call_id] = runtime
        runtime.task = asyncio.create_task(self._run(runtime), name=f"sideband:{call_id}")
        try:
            return await asyncio.wait_for(
                asyncio.shield(runtime.connected_waiter),
                timeout=self.settings.xai_connect_timeout_seconds,
            )
        except BaseException:
            if runtime.task and not runtime.task.done():
                runtime.task.cancel()
                await asyncio.gather(runtime.task, return_exceptions=True)
            self._runtime.pop(call_id, None)
            raise

    async def _run(self, runtime: RealtimeRuntime) -> None:
        url = (
            "wss://api.x.ai/v1/realtime"
            f"?call_id={runtime.xai_call_id}&model={self.settings.realtime_model}"
        )
        receiver: asyncio.Task[None] | None = None
        dispatcher: asyncio.Task[None] | None = None
        open_task: asyncio.Task[None] | None = None
        try:
            async with websockets.connect(
                url,
                additional_headers={
                    "Authorization": f"Bearer {Settings.reveal(self.settings.xai_api_key)}"
                },
                open_timeout=self.settings.xai_connect_timeout_seconds,
                close_timeout=2,
            ) as websocket:
                runtime.websocket = websocket
                if runtime.connected_waiter and not runtime.connected_waiter.done():
                    runtime.connected_waiter.set_result(101)
                # A runtime is single-use today, but resetting here makes the lifecycle explicit
                # and prevents stale queued events if that ever changes.
                runtime.event_queue = asyncio.Queue(maxsize=REALTIME_EVENT_QUEUE_MAXSIZE)
                # The SIP session already exists by the time this sideband attaches, so its
                # session.created event may not be replayed. Start receiving before on_open so
                # that the readiness handshake can send session.update and await session.updated.
                receiver = asyncio.create_task(
                    self._receive_events(runtime, websocket),
                    name=f"sideband-receiver:{runtime.call_id}",
                )
                dispatcher = asyncio.create_task(
                    self._dispatch_events(runtime),
                    name=f"sideband-dispatcher:{runtime.call_id}",
                )
                runtime.receiver_task = receiver
                runtime.dispatcher_task = dispatcher

                # Supervise on_open as well: it may be awaiting the session.updated readiness
                # echo, while either event task may fail independently.
                open_task = asyncio.create_task(
                    self.on_open(runtime.call_id),
                    name=f"sideband-open:{runtime.call_id}",
                )
                runtime.open_task = open_task
                done, _ = await asyncio.wait(
                    {open_task, receiver, dispatcher},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if open_task not in done:
                    # on_open may intentionally terminate the call. Its teardown closes the
                    # websocket from this child task; let that child finish instead of treating
                    # the resulting receiver close as a readiness failure and canceling it.
                    if runtime.closing:
                        await open_task
                        if runtime.stop_after_current:
                            return
                        await self._supervise_event_tasks(receiver, dispatcher)
                        return
                    failed = receiver if receiver in done else dispatcher
                    await failed
                    raise RuntimeError("Realtime sideband closed before readiness completed")
                await open_task
                if runtime.stop_after_current:
                    return
                await self._supervise_event_tasks(receiver, dispatcher)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if runtime.connected_waiter and not runtime.connected_waiter.done():
                runtime.connected_waiter.set_exception(exc)
            if not runtime.closing:
                logger.exception("Realtime sideband failed for %s", runtime.call_id)
                await self.on_fatal(runtime.call_id, f"sideband_error:{type(exc).__name__}")
        finally:
            tasks = [task for task in (open_task, receiver, dispatcher) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            runtime.websocket = None
            if runtime.closing and self._runtime.get(runtime.call_id) is runtime:
                self._runtime.pop(runtime.call_id, None)

    async def _receive_events(self, runtime: RealtimeRuntime, websocket: Any) -> None:
        async for message in websocket:
            event = json.loads(message)
            event_type = event.get("type")
            if event_type in _LOGGED_REALTIME_EVENT_TYPES:
                response = event.get("response") or {}
                logger.info(
                    "Realtime event received call_id=%s type=%s event_id=%s response_id=%s",
                    runtime.call_id,
                    event_type,
                    event.get("event_id"),
                    response.get("id") or event.get("response_id"),
                )
            # Record liveness on the reader fast path. Application dispatch may be
            # intentionally backlogged, but a frame already read from the socket is
            # authoritative evidence that the call is still alive.
            if self.on_activity is not None:
                self.on_activity(runtime.call_id)
            if event.get("type") == "session.updated" and runtime.update_waiter:
                if not runtime.update_waiter.done():
                    runtime.update_waiter.set_result(event)
            try:
                runtime.event_queue.put_nowait(event)
            except asyncio.QueueFull as exc:
                raise RealtimeEventQueueOverflow(
                    f"Realtime event queue exceeded {REALTIME_EVENT_QUEUE_MAXSIZE} events"
                ) from exc

        # A normal websocket close drains events already accepted by the reader before stopping
        # the dispatcher. This control marker is not an incoming event, so waiting for capacity is
        # safe and must not be treated as an overflow.
        await runtime.event_queue.put(_EVENT_QUEUE_CLOSED)

    async def _dispatch_events(self, runtime: RealtimeRuntime) -> None:
        while True:
            event = await runtime.event_queue.get()
            try:
                if event is _EVENT_QUEUE_CLOSED:
                    return
                await self.on_event(runtime.call_id, event)
                if runtime.stop_after_current:
                    return
            finally:
                runtime.event_queue.task_done()

    @staticmethod
    async def _supervise_event_tasks(
        receiver: asyncio.Task[None], dispatcher: asyncio.Task[None]
    ) -> None:
        done, _ = await asyncio.wait(
            {receiver, dispatcher},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if dispatcher in done:
            # A dispatcher failure must immediately stop the reader. A successful dispatcher can
            # only consume its close marker after the reader has reached a clean websocket close.
            await dispatcher
            if not receiver.done():
                raise RuntimeError("Realtime dispatcher stopped before the receiver")

        await receiver
        await dispatcher

    async def send(self, call_id: str, event: dict[str, Any]) -> None:
        await self._send_batch(call_id, [event])

    async def _send_batch(self, call_id: str, events: list[dict[str, Any]]) -> None:
        serialized = [(event, json.dumps(event)) for event in events]
        if not serialized:
            return
        runtime = self._runtime.get(call_id)
        if runtime is None:
            raise RuntimeError("sideband is not open")

        # Single control frames retain normal asyncio cancellation semantics. Only
        # a multi-frame protocol pair needs cancellation shielding after it starts.
        if len(serialized) == 1:
            await self._send_locked_batch(call_id, runtime, serialized)
            return

        # Waiting for ownership is still normally cancellable. Once acquired,
        # however, the protocol pair must either finish or hit a wire failure.
        await runtime.send_lock.acquire()
        try:
            send_task = asyncio.create_task(
                self._send_owned_batch(call_id, runtime, serialized),
                name=f"sideband-send:{call_id}",
            )
        except BaseException:
            runtime.send_lock.release()
            raise
        interrupted = False
        while not send_task.done():
            try:
                await asyncio.shield(send_task)
            except asyncio.CancelledError:
                # Once a batch is eligible to write, cancellation cannot split its
                # function output from its continuation. Re-raise only after the
                # shielded sender has released the per-call lock and recorded every
                # frame that actually reached the websocket.
                interrupted = True
            except BaseException:
                break

        failure: BaseException | None = None
        try:
            send_task.result()
        except BaseException as exc:
            failure = exc
        if interrupted:
            raise asyncio.CancelledError
        if failure is not None:
            raise failure

    async def _send_locked_batch(
        self,
        call_id: str,
        runtime: RealtimeRuntime,
        serialized: list[tuple[dict[str, Any], str]],
    ) -> None:
        sent: list[dict[str, Any]] = []
        try:
            async with runtime.send_lock:
                websocket = runtime.websocket
                if websocket is None:
                    raise RuntimeError("sideband is not open")
                for event, payload in serialized:
                    # An unbounded stalled send makes the sender cancellation-proof via
                    # _send_batch's shield loop, so bound every write on the wire.
                    async with asyncio.timeout(REALTIME_SEND_TIMEOUT_SECONDS):
                        await websocket.send(payload)
                    sent.append(event)
        finally:
            await self._notify_sent(call_id, sent)

    async def _send_owned_batch(
        self,
        call_id: str,
        runtime: RealtimeRuntime,
        serialized: list[tuple[dict[str, Any], str]],
    ) -> None:
        sent: list[dict[str, Any]] = []
        failure: BaseException | None = None
        try:
            websocket = runtime.websocket
            if websocket is None:
                raise RuntimeError("sideband is not open")
            for event, payload in serialized:
                # An unbounded stalled send makes the sender cancellation-proof via
                # _send_batch's shield loop, so bound every write on the wire.
                async with asyncio.timeout(REALTIME_SEND_TIMEOUT_SECONDS):
                    await websocket.send(payload)
                sent.append(event)
        except BaseException as exc:
            failure = exc
        finally:
            runtime.send_lock.release()

        await self._notify_sent(call_id, sent)
        if failure is not None:
            raise failure

    async def _notify_sent(self, call_id: str, events: list[dict[str, Any]]) -> None:
        for event in events:
            logger.info(
                "Realtime control sent call_id=%s type=%s",
                call_id,
                event.get("type"),
            )
            if self.on_send is None:
                continue
            try:
                await self.on_send(call_id, event)
            except Exception:
                # Telemetry must never turn a successfully sent Realtime event into a call failure.
                logger.warning(
                    "failed to record outbound Realtime event call_id=%s type=%s",
                    call_id,
                    event.get("type"),
                    exc_info=True,
                )

    async def _update_session(self, call_id: str, session: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime.get(call_id)
        if runtime is None:
            raise RuntimeError("sideband runtime missing")
        async with runtime.update_lock:
            loop = asyncio.get_running_loop()
            runtime.update_waiter = loop.create_future()
            await self.send(
                call_id,
                {
                    "type": "session.update",
                    "session": session,
                },
            )
            try:
                return await asyncio.wait_for(runtime.update_waiter, timeout=3)
            finally:
                runtime.update_waiter = None

    def _transcription_config(self) -> dict[str, Any]:
        return {"model": self.settings.input_transcription_model}

    async def verify_initial_session(self, call_id: str) -> dict[str, Any]:
        runtime = self._runtime.get(call_id)
        if runtime is None:
            raise RuntimeError("sideband runtime missing")
        if runtime.packet is None:
            raise RuntimeError("sideband runtime is missing approved call context")
        config = self.build_session_config(runtime.packet)
        return await self._update_session(call_id, config.model_dump(exclude_none=False))

    async def enable_automatic_responses(self, call_id: str) -> dict[str, Any]:
        return await self._update_session(
            call_id,
            {
                "turn_detection": {
                    "type": "server_vad",
                    "silence_duration_ms": self.settings.server_vad_silence_duration_ms,
                    "prefix_padding_ms": self.settings.server_vad_prefix_padding_ms,
                }
            },
        )

    async def create_opening(self, call_id: str) -> None:
        await self.send(call_id, {"type": "response.create"})

    async def cancel_response(self, call_id: str, response_id: str | None = None) -> None:
        event: dict[str, Any] = {"type": "response.cancel"}
        if response_id:
            event["response_id"] = response_id
        await self.send(call_id, event)

    async def create_voicemail(self, call_id: str) -> None:
        await self.send(
            call_id,
            {
                "type": "response.create",
                "response": {
                    "instructions": (
                        "Leave one concise voicemail that advances the approved objective using only "
                        "the approved context. Do not ask questions."
                    ),
                },
            },
        )

    async def send_tool_result(
        self,
        call_id: str,
        tool_call_id: str,
        output: dict[str, Any],
        *,
        continue_response: bool = True,
        continuation_instructions: str | None = None,
    ) -> None:
        events: list[dict[str, Any]] = [
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": json.dumps(output),
                },
            }
        ]
        if continue_response:
            continuation: dict[str, Any] = {"type": "response.create"}
            if continuation_instructions:
                continuation["response"] = {
                    "instructions": continuation_instructions,
                }
            events.append(continuation)
        await self._send_batch(call_id, events)

    async def hangup(self, xai_call_id: str | None) -> None:
        if not xai_call_id:
            return
        try:
            response = await self.client.post(f"/realtime/calls/{xai_call_id}/hangup")
            if response.status_code in {404, 409, 410, 422}:
                return
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {404, 409, 410, 422}:
                return
            raise

    async def reject(self, xai_call_id: str) -> None:
        # xAI currently exposes hangup rather than a separate SIP reject endpoint.
        try:
            await self.hangup(xai_call_id)
        except Exception:
            logger.warning("failed to hang up unmapped xAI SIP call", exc_info=True)

    async def drain_and_close(self, call_id: str) -> None:
        runtime = self._runtime.get(call_id)
        if runtime is None:
            return
        runtime.closing = True
        await asyncio.sleep(REALTIME_MEDIA_DRAIN_SECONDS)
        current = asyncio.current_task()
        runtime_tasks = {
            task
            for task in (
                runtime.task,
                runtime.open_task,
                runtime.receiver_task,
                runtime.dispatcher_task,
            )
            if task is not None
        }
        close_error: BaseException | None = None
        if runtime.websocket is not None:
            try:
                await asyncio.wait_for(
                    runtime.websocket.close(), timeout=REALTIME_CLOSE_TIMEOUT_SECONDS
                )
            except TimeoutError as exc:
                close_error = exc
                logger.warning("timed out closing Realtime websocket call_id=%s", call_id)
            except Exception as exc:
                close_error = exc
                logger.warning(
                    "failed to close Realtime websocket call_id=%s", call_id, exc_info=True
                )

        if current in runtime_tasks:
            # The supervisor owns child cancellation. Awaiting or canceling it from one of its
            # own children creates a parent/child cancellation cycle; normal websocket close will
            # instead let the reader and FIFO dispatcher finish under _run's supervision.
            if close_error is not None:
                # The current callback may still need to persist terminal state and schedule its
                # finalizer after drain_and_close returns. Let it finish, then have on_open/_run or
                # the dispatcher stop at the callback boundary so the supervisor can clean up.
                runtime.stop_after_current = True
            return

        task = runtime.task
        if task is not None and not task.done():
            done, _ = await asyncio.wait({task}, timeout=REALTIME_TASK_DRAIN_TIMEOUT_SECONDS)
            if not done:
                task.cancel()
                done, _ = await asyncio.wait({task}, timeout=REALTIME_TASK_CANCEL_TIMEOUT_SECONDS)
                if not done:
                    # asyncio cannot forcibly destroy a cancellation-resistant task. Keep the
                    # runtime registered so a later teardown can retry rather than hiding a leak.
                    logger.error(
                        "Realtime sideband did not stop after cancellation call_id=%s", call_id
                    )
                    return
            await asyncio.gather(task, return_exceptions=True)

        if self._runtime.get(call_id) is runtime:
            self._runtime.pop(call_id, None)

    async def close_all(self) -> None:
        """Stop every sideband runtime before its callback dependencies are closed."""

        if not self._runtime:
            return
        call_ids = tuple(self._runtime)
        results = await asyncio.gather(
            *(self.drain_and_close(call_id) for call_id in call_ids),
            return_exceptions=True,
        )
        for call_id, result in zip(call_ids, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "failed to close Realtime runtime call_id=%s",
                    call_id,
                    exc_info=(type(result), result, result.__traceback__),
                )

        # drain_and_close deliberately retains a cancellation-resistant runtime so
        # a later teardown can see it. Application shutdown has no later owner, so
        # make one final bounded cancellation pass. A stalled send (see
        # REALTIME_SEND_TIMEOUT_SECONDS) can still outlive cancellation briefly, so this
        # wait itself is bounded rather than left to hang process shutdown; any task still
        # outstanding after the timeout is logged and abandoned rather than awaited further.
        remaining = tuple(self._runtime.values())
        tasks = {
            task
            for runtime in remaining
            for task in (
                runtime.task,
                runtime.open_task,
                runtime.receiver_task,
                runtime.dispatcher_task,
            )
            if task is not None and not task.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(
                tasks, timeout=REALTIME_SHUTDOWN_FINAL_TIMEOUT_SECONDS
            )
            for task in done:
                with contextlib.suppress(asyncio.CancelledError):
                    task.exception()
            if pending:
                logger.error(
                    "Realtime shutdown gave up waiting for %d task(s) to stop: %s",
                    len(pending),
                    ", ".join(sorted(task.get_name() for task in pending)),
                )
        for runtime in remaining:
            if runtime.task is None or runtime.task.done():
                self._runtime.pop(runtime.call_id, None)

    def expected_transcription_echoed(self, event: dict[str, Any]) -> bool:
        session = event.get("session", {})
        transcription = session.get("audio", {}).get("input", {}).get("transcription") or {}
        return transcription.get("model") == self.settings.input_transcription_model

    def expected_initial_vad_echoed(self, event: dict[str, Any]) -> bool:
        return event.get("session", {}).get("turn_detection") is None

    def activation_update_confirmed(self, event: dict[str, Any]) -> bool:
        turn = event.get("session", {}).get("turn_detection") or {}
        return (
            turn.get("type") == "server_vad"
            and turn.get("silence_duration_ms") == self.settings.server_vad_silence_duration_ms
            and turn.get("prefix_padding_ms") == self.settings.server_vad_prefix_padding_ms
        )

    async def close(self) -> None:
        await self.close_all()
        await self.client.aclose()
