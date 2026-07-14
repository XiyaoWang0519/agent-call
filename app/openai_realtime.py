from __future__ import annotations

import asyncio
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


@dataclass(slots=True)
class RealtimeRuntime:
    call_id: str
    openai_call_id: str
    websocket: Any | None = None
    task: asyncio.Task[None] | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    update_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    update_waiter: asyncio.Future[dict[str, Any]] | None = None
    closing: bool = False


class RealtimeBridge:
    def __init__(
        self,
        settings: Settings,
        client: AsyncOpenAI,
        *,
        on_event: EventHandler,
        on_open: OpenHandler,
        on_fatal: FatalHandler,
    ):
        self.settings = settings
        self.client = client
        self.on_event = on_event
        self.on_open = on_open
        self.on_fatal = on_fatal
        self._runtime: dict[str, RealtimeRuntime] = {}

    def build_accept_payload(self, packet: ContextPacket) -> AcceptPayload:
        transcription: dict[str, Any] = {"model": self.settings.input_transcription_model}
        if self.settings.input_transcription_model == "gpt-realtime-whisper":
            if self.settings.input_transcription_delay:
                transcription["delay"] = self.settings.input_transcription_delay
        return AcceptPayload(
            instructions=realtime_instructions(packet),
            audio={
                "input": {
                    "transcription": transcription,
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "auto",
                        "create_response": False,
                        "interrupt_response": False,
                    },
                },
                "output": {
                    "voice": "marin",
                    "speed": 1.0,
                },
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
            ],
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
                await self.on_open(runtime.call_id)
                async for message in websocket:
                    event = json.loads(message)
                    if event.get("type") == "session.updated" and runtime.update_waiter:
                        if not runtime.update_waiter.done():
                            runtime.update_waiter.set_result(event)
                    await self.on_event(runtime.call_id, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not runtime.closing:
                logger.exception("Realtime sideband failed for %s", runtime.call_id)
                await self.on_fatal(runtime.call_id, f"sideband_error:{type(exc).__name__}")
        finally:
            runtime.websocket = None

    async def send(self, call_id: str, event: dict[str, Any]) -> None:
        runtime = self._runtime.get(call_id)
        if runtime is None or runtime.websocket is None:
            raise RuntimeError("sideband is not open")
        async with runtime.send_lock:
            await runtime.websocket.send(json.dumps(event))

    async def enable_automatic_responses(self, call_id: str) -> dict[str, Any]:
        runtime = self._runtime.get(call_id)
        if runtime is None:
            raise RuntimeError("sideband runtime missing")
        async with runtime.update_lock:
            loop = asyncio.get_running_loop()
            runtime.update_waiter = loop.create_future()
            # Patch only turn_detection. Do not re-specify audio.format: SIP media
            # negotiates G.711 with Twilio, and an explicit format can silence RTP.
            await self.send(
                call_id,
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
                },
            )
            try:
                return await asyncio.wait_for(runtime.update_waiter, timeout=3)
            finally:
                runtime.update_waiter = None

    async def create_greeting(self, call_id: str, target: str) -> None:
        greeting = (
            f"Hi, this is Poke, Irvin's AI assistant calling on his behalf. "
            f"Am I speaking with {target}?"
        )
        await self.send(
            call_id,
            {
                "type": "response.create",
                "response": {
                    "output_modalities": ["audio"],
                    "instructions": f'Say exactly: "{greeting}" Do not add any other words.',
                },
            },
        )

    async def create_voicemail(self, call_id: str, packet: ContextPacket) -> None:
        await self.send(
            call_id,
            {
                "type": "response.create",
                "response": {
                    "output_modalities": ["audio"],
                    "instructions": (
                        "Leave exactly one concise voicemail. Identify yourself as Poke, "
                        f"{packet.owner.display_name}'s AI assistant, state the approved purpose, "
                        f"and give callback number {packet.owner.callback_number}. Do not ask questions."
                    ),
                },
            },
        )

    async def send_tool_result(
        self, call_id: str, tool_call_id: str, output: dict[str, Any]
    ) -> None:
        await self.send(
            call_id,
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": json.dumps(output),
                },
            },
        )
        await self.send(call_id, {"type": "response.create"})

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
        await asyncio.sleep(1.5)
        if runtime.websocket is not None:
            await runtime.websocket.close()
        current = asyncio.current_task()
        if runtime.task and runtime.task is not current and not runtime.task.done():
            runtime.task.cancel()
            await asyncio.gather(runtime.task, return_exceptions=True)
        self._runtime.pop(call_id, None)

    def expected_transcription_echoed(self, event: dict[str, Any]) -> bool:
        session = event.get("session", {})
        transcription = session.get("audio", {}).get("input", {}).get("transcription") or {}
        if transcription.get("model") != self.settings.input_transcription_model:
            return False
        expected_delay = self.settings.input_transcription_delay
        return expected_delay is None or transcription.get("delay") == expected_delay

    @staticmethod
    def expected_initial_vad_echoed(event: dict[str, Any]) -> bool:
        turn = event.get("session", {}).get("audio", {}).get("input", {}).get("turn_detection", {})
        return (
            turn.get("type") == "semantic_vad"
            and turn.get("eagerness") == "auto"
            and turn.get("create_response") is False
            and turn.get("interrupt_response") is False
        )

    @staticmethod
    def activation_update_confirmed(event: dict[str, Any]) -> bool:
        turn = event.get("session", {}).get("audio", {}).get("input", {}).get("turn_detection", {})
        return turn.get("create_response") is True and turn.get("interrupt_response") is True
