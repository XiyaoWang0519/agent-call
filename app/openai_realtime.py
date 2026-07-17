from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import websockets
from openai import APIStatusError, AsyncOpenAI

from app.models import AcceptPayload, ContextPacket
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


class RealtimeEventQueueOverflow(RuntimeError):
    """Raised when application event handling cannot keep up with the sideband stream."""


@dataclass(slots=True)
class RealtimeRuntime:
    call_id: str
    openai_call_id: str
    websocket: Any | None = None
    task: asyncio.Task[None] | None = None
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
        client: AsyncOpenAI,
        *,
        on_event: EventHandler,
        on_open: OpenHandler,
        on_fatal: FatalHandler,
        on_send: SendHandler | None = None,
        on_activity: ActivityHandler | None = None,
    ):
        self.settings = settings
        self.client = client
        self.on_event = on_event
        self.on_open = on_open
        self.on_fatal = on_fatal
        self.on_send = on_send
        self.on_activity = on_activity
        self._runtime: dict[str, RealtimeRuntime] = {}

    def build_accept_payload(self, packet: ContextPacket) -> AcceptPayload:
        tools: list[dict[str, Any]] = [
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
                "name": "search_web",
                "description": (
                    "Search the public web for current, recent, location-specific, or "
                    "uncertain factual information. Use a standalone query with the exact "
                    "entity, location, and date context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 2,
                            "maxLength": 500,
                            "description": (
                                "A standalone natural-language web search query with all "
                                "context needed to understand it."
                            ),
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "send_dtmf",
                "description": (
                    "Send keypad tones to navigate an automated phone menu (IVR), such as "
                    "'press 2 for reservations'. The tones reach only the other party (the "
                    "callee leg), not the human user on this call."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "digits": {
                            "type": "string",
                            "pattern": "^[0-9*#w]{1,32}$",
                            "minLength": 1,
                            "maxLength": 32,
                            "description": (
                                "Keypad digits to send (0-9, *, #). Use 'w' for a half-second "
                                "pause. Send one short sequence at a time."
                            ),
                        }
                    },
                    "required": ["digits"],
                    "additionalProperties": False,
                },
            },
        ]
        if self.settings.ask_poke_enabled:
            tools.append(
                {
                    "type": "function",
                    "name": "ask_poke",
                    "description": (
                        "Ask the owner's assistant (Poke) one question it can answer from the "
                        "owner's information — account details, preferences, confirmations not "
                        "in your approved context. Tell the callee you are checking BEFORE "
                        "calling this. You will receive the answer or a timeout as the function "
                        "result. Never guess while waiting."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "minLength": 5,
                                "maxLength": 500,
                            },
                            "reason": {"type": "string", "maxLength": 200},
                        },
                        "required": ["question"],
                        "additionalProperties": False,
                    },
                }
            )
        if self.settings.hold_detection_enabled:
            tools.append(
                {
                    "type": "function",
                    "name": "report_hold",
                    "description": (
                        "Call this when you have been placed on hold, hear hold music, or an "
                        "automated message asks you to wait on the line. After calling it, stay "
                        "silent until a human returns."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {"type": "string", "maxLength": 200},
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                }
            )
        tools.append(
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
            }
        )
        return AcceptPayload(
            instructions=realtime_instructions(
                packet,
                ask_poke_enabled=self.settings.ask_poke_enabled,
                hold_detection_enabled=self.settings.hold_detection_enabled,
            ),
            audio={
                "input": self._input_audio_config(create_response=False, interrupt_response=False),
                "output": {
                    "voice": "cedar",
                    "speed": 1.0,
                },
            },
            tools=tools,
        )

    async def accept_and_connect(
        self,
        *,
        call_id: str,
        openai_call_id: str,
        packet: ContextPacket,
    ) -> int:
        payload = self.build_accept_payload(packet)
        try:
            async with asyncio.timeout(self.settings.openai_http_timeout_seconds):
                raw = await self.client.realtime.calls.with_raw_response.accept(
                    openai_call_id, **payload.model_dump(exclude_none=True)
                )
        except APIStatusError as exc:
            logger.error(
                "OpenAI call accept response call_id=%s status=%s headers=%s body=%r",
                call_id,
                exc.status_code,
                dict(exc.response.headers),
                exc.response.text,
            )
            raise
        except TimeoutError:
            logger.error(
                "OpenAI call accept timed out call_id=%s timeout=%ss",
                call_id,
                self.settings.openai_http_timeout_seconds,
            )
            raise
        # The accept response is bodyless today; log every non-secret response field.
        logger.info(
            "OpenAI call accept response call_id=%s status=%s headers=%s body=%r",
            call_id,
            raw.status_code,
            dict(raw.headers),
            raw.text,
        )
        if raw.status_code < 200 or raw.status_code >= 300:
            raise RuntimeError(f"OpenAI accept failed with HTTP {raw.status_code}")
        runtime = RealtimeRuntime(call_id=call_id, openai_call_id=openai_call_id)
        self._runtime[call_id] = runtime
        runtime.task = asyncio.create_task(self._run(runtime), name=f"sideband:{call_id}")
        return raw.status_code

    async def _run(self, runtime: RealtimeRuntime) -> None:
        url = f"wss://api.openai.com/v1/realtime?call_id={runtime.openai_call_id}"
        receiver: asyncio.Task[None] | None = None
        dispatcher: asyncio.Task[None] | None = None
        open_task: asyncio.Task[None] | None = None
        try:
            async with websockets.connect(
                url,
                additional_headers={
                    "Authorization": f"Bearer {Settings.reveal(self.settings.openai_api_key)}"
                },
                open_timeout=10,
                close_timeout=2,
            ) as websocket:
                runtime.websocket = websocket
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
        if self.on_send is None:
            return
        for event in events:
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

    async def _update_session(self, call_id: str, audio_input: dict[str, Any]) -> dict[str, Any]:
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
                    "session": {
                        "type": "realtime",
                        "audio": {"input": audio_input},
                    },
                },
            )
            try:
                return await asyncio.wait_for(runtime.update_waiter, timeout=3)
            finally:
                runtime.update_waiter = None

    def _transcription_config(self) -> dict[str, Any]:
        transcription: dict[str, Any] = {"model": self.settings.input_transcription_model}
        if self.settings.input_transcription_model == "gpt-realtime-whisper":
            if self.settings.input_transcription_delay:
                transcription["delay"] = self.settings.input_transcription_delay
        return transcription

    def _input_audio_config(
        self, *, create_response: bool, interrupt_response: bool
    ) -> dict[str, Any]:
        # Do not specify audio.format: SIP media negotiates G.711 with Twilio, and
        # explicitly overriding it can silence RTP. Always re-assert transcription
        # alongside turn_detection: session.update replaces the nested audio.input
        # object, so a turn_detection-only update would drop input transcription.
        return {
            "transcription": self._transcription_config(),
            "turn_detection": {
                "type": "semantic_vad",
                "eagerness": self.settings.semantic_vad_eagerness,
                "create_response": create_response,
                "interrupt_response": interrupt_response,
            },
        }

    async def verify_initial_session(self, call_id: str) -> dict[str, Any]:
        return await self._update_session(
            call_id,
            self._input_audio_config(create_response=False, interrupt_response=False),
        )

    async def enable_automatic_responses(self, call_id: str) -> dict[str, Any]:
        return await self._update_session(
            call_id,
            self._input_audio_config(create_response=True, interrupt_response=True),
        )

    async def suspend_automatic_responses(self, call_id: str) -> dict[str, Any]:
        # Stop the model from generating responses on its own (that is where hold-time
        # cost comes from) while leaving semantic VAD and input transcription active, so
        # a human returning from hold is still transcribed and can be detected.
        return await self._update_session(
            call_id,
            self._input_audio_config(create_response=False, interrupt_response=False),
        )

    async def create_opening(self, call_id: str) -> None:
        await self.request_response(call_id)

    async def cancel_response(self, call_id: str, response_id: str | None = None) -> None:
        event: dict[str, Any] = {"type": "response.cancel"}
        if response_id:
            event["response_id"] = response_id
        await self.send(call_id, event)

    async def create_voicemail(self, call_id: str) -> None:
        await self.request_response(
            call_id,
            instructions=(
                "Leave one concise voicemail that advances the approved objective using only "
                "the approved context. Do not ask questions."
            ),
        )

    async def request_response(self, call_id: str, *, instructions: str | None = None) -> None:
        response: dict[str, Any] = {"output_modalities": ["audio"]}
        if instructions:
            response["instructions"] = instructions
        await self.send(call_id, {"type": "response.create", "response": response})

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
                    "output_modalities": ["audio"],
                    "instructions": continuation_instructions,
                }
            events.append(continuation)
        await self._send_batch(call_id, events)

    async def hangup(self, openai_call_id: str | None) -> None:
        if not openai_call_id:
            return
        try:
            await self.client.realtime.calls.hangup(openai_call_id)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status in {404, 409, 410, 422}:
                return
            raise

    async def reject(self, openai_call_id: str) -> None:
        try:
            # The live API defaults to SIP 603 (Decline) when status_code is omitted.
            await self.client.realtime.calls.reject(openai_call_id)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status in {404, 409, 410, 422}:
                return
            logger.warning("failed to reject unmapped SIP call", exc_info=True)

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
        if transcription.get("model") != self.settings.input_transcription_model:
            return False
        expected_delay = self.settings.input_transcription_delay
        return expected_delay is None or transcription.get("delay") == expected_delay

    def expected_initial_vad_echoed(self, event: dict[str, Any]) -> bool:
        turn = event.get("session", {}).get("audio", {}).get("input", {}).get("turn_detection", {})
        return (
            turn.get("type") == "semantic_vad"
            and turn.get("eagerness") == self.settings.semantic_vad_eagerness
            and turn.get("create_response") is False
            and turn.get("interrupt_response") is False
        )

    def activation_update_confirmed(self, event: dict[str, Any]) -> bool:
        turn = event.get("session", {}).get("audio", {}).get("input", {}).get("turn_detection", {})
        return (
            turn.get("type") == "semantic_vad"
            and turn.get("eagerness") == self.settings.semantic_vad_eagerness
            and turn.get("create_response") is True
            and turn.get("interrupt_response") is True
        )
