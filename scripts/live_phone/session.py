from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import time
from pathlib import Path
from typing import Any

from fastapi import WebSocket
from openai import AsyncOpenAI

from scripts.live_phone.audio import (
    RATE,
    DigitDetector,
    decode_mulaw,
    encode_mulaw,
    rms,
    tone,
    wav_bytes,
)
from scripts.live_phone.scenarios import Step


class Session:
    """Destination-side audio observer and deterministic conversational counterpart."""

    def __init__(
        self,
        role: str,
        root: Path,
        speech: dict[str, bytes],
        replacements: dict[str, str],
        client: AsyncOpenAI,
        asr_model: str,
        hangup: Any,
        signals: dict[str, asyncio.Event],
    ):
        self.role = role
        self.root = root
        self.speech = speech
        self.replacements = replacements
        self.client = client
        self.asr_model = asr_model
        self.hangup = hangup
        self.signals = signals
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.drained = asyncio.Event()
        self.finished = asyncio.Event()
        self.changed = asyncio.Event()
        self.started = time.monotonic()
        self.websocket: WebSocket | None = None
        self.stream_sid = ""
        self.call_sid = ""
        self.events: list[dict[str, Any]] = []
        self.transcripts: list[dict[str, Any]] = []
        self.voiced: list[tuple[float, float]] = []
        self.rx = bytearray()
        self.tx = bytearray()
        self.segment = bytearray()
        self.segment_start = 0.0
        self.silence_bytes = 0
        self.detector = DigitDetector()
        self.digits = ""
        self.expected_digit_cursor = 0
        self.marks: dict[str, asyncio.Event] = {}
        self.queue: asyncio.Queue[tuple[float, float, bytes] | None] = asyncio.Queue(maxsize=64)
        self.error: str | None = None
        self.gaps = 0
        self.last_text_boundary = 0.0

    def now(self) -> float:
        return time.monotonic() - self.started

    def event(self, kind: str, **values: Any) -> None:
        self.events.append({"type": kind, "at": self.now(), **values})
        self.changed.set()

    def render(self, text: str) -> str:
        for key, value in self.replacements.items():
            text = text.replace("{" + key + "}", value)
        return text

    def flush(self) -> None:
        if len(self.segment) >= 1600:
            self.queue.put_nowait((self.segment_start, self.now(), bytes(self.segment)))
        self.segment.clear()
        self.silence_bytes = 0

    def ingest(self, encoded: str, timestamp: int) -> None:
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > RATE * 2 or timestamp < 0 or timestamp > 720_000:
            raise ValueError("invalid media frame")
        pcm = decode_mulaw(raw)
        offset = timestamp * 16
        if offset < len(self.rx):
            raise ValueError("overlapping media timestamps")
        if offset > len(self.rx):
            if offset - len(self.rx) > 3200:
                self.gaps += 1
            self.rx.extend(bytes(offset - len(self.rx)))
        self.rx.extend(pcm)
        now = self.now()
        duration = len(pcm) / (RATE * 2)
        voiced = rms(pcm) > 350
        if voiced:
            self.voiced.append((now, duration))
            if not self.segment:
                self.segment_start = now
            self.silence_bytes = 0
        elif self.segment:
            self.silence_bytes += len(pcm)
        if self.segment or voiced:
            self.segment.extend(pcm)
        if self.silence_bytes >= 8000 or len(self.segment) >= RATE * 2 * 10:
            self.flush()
        for digit in self.detector.feed(pcm):
            self.digits += digit
            self.event("digit_audio", digit=digit)
        self.changed.set()

    async def transcribe(self) -> None:
        while (item := await self.queue.get()) is not None:
            start, end, pcm = item
            try:
                result = await self.client.audio.transcriptions.create(
                    model=self.asr_model,
                    file=("received.wav", wav_bytes(pcm), "audio/wav"),
                    response_format="json",
                    language="en",
                )
                # No prompt/expected text: the observer must hear the answer independently.
                self.transcripts.append({"start": start, "end": end, "text": result.text})
                self.event("transcript", start=start, end=end, text=result.text)
            except Exception as exc:
                self.error = f"asr:{type(exc).__name__}"
                self.changed.set()

    async def receive(self, websocket: WebSocket, stream_sid: str, call_sid: str) -> None:
        self.websocket = websocket
        self.stream_sid = stream_sid
        self.call_sid = call_sid
        self.started = time.monotonic()
        self.ready.set()
        worker = asyncio.create_task(self.transcribe())
        try:
            while True:
                message = await websocket.receive_json()
                if message.get("streamSid") != stream_sid:
                    raise ValueError("stream SID mismatch")
                kind = message.get("event")
                if kind == "media":
                    media = message["media"]
                    if media.get("track") != "inbound":
                        raise ValueError("wrong audio track")
                    self.ingest(media["payload"], int(media["timestamp"]))
                elif kind == "mark":
                    name = message["mark"]["name"]
                    if name in self.marks:
                        self.marks[name].set()
                        self.event("playback_finished", name=name)
                elif kind == "dtmf":
                    # Keep provider events separate; audio decoding is the IVR assertion.
                    self.event("digit_provider", digit=message["dtmf"]["digit"])
                elif kind == "stop":
                    break
        except Exception as exc:
            if type(exc).__name__ != "WebSocketDisconnect":
                self.error = f"media:{type(exc).__name__}"
        finally:
            self.closed.set()
            self.changed.set()
            try:
                self.flush()
                await self.queue.put(None)
                await asyncio.wait_for(worker, timeout=30)
            except (Exception, asyncio.CancelledError):
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
                self.error = self.error or "asr_drain_failed"
            self.save()
            self.drained.set()

    async def play(self, pcm: bytes, label: str) -> tuple[float, float]:
        if not self.websocket or self.closed.is_set():
            raise RuntimeError("phone disconnected before playback")
        if len(pcm) > RATE * 2 * 60:
            raise ValueError("fixture exceeds 60 seconds")
        name = f"play-{len(self.marks)}"
        self.marks[name] = asyncio.Event()
        start = self.now()
        self.last_text_boundary = start
        offset = int(start * RATE) * 2
        if offset > len(self.tx):
            self.tx.extend(bytes(offset - len(self.tx)))
        self.tx.extend(pcm)
        self.event("playback_started", name=name, label=label, duration=len(pcm) / (RATE * 2))
        mulaw = encode_mulaw(pcm)
        # Pace small packets to bound Twilio's queue and make interruption timing meaningful.
        for offset in range(0, len(mulaw), 160):
            await self.websocket.send_json(
                {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(mulaw[offset : offset + 160]).decode()},
                }
            )
            await asyncio.sleep(max(0, start + (offset + 160) / RATE - self.now()))
        await self.websocket.send_json(
            {"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}}
        )
        await asyncio.wait_for(self.marks[name].wait(), timeout=10)
        return start, self.now()

    async def until(self, predicate: Any, seconds: float) -> None:
        async with asyncio.timeout(seconds):
            while True:
                self.changed.clear()
                if self.error:
                    raise RuntimeError(self.error)
                if predicate():
                    return
                if self.closed.is_set():
                    # Receive may still be draining ASR; wait for final transcript events.
                    await asyncio.sleep(0.1)
                else:
                    await self.changed.wait()

    def heard(self, pattern: str) -> bool:
        text = " ".join(t["text"] for t in self.transcripts if t["end"] >= self.last_text_boundary)
        return re.search(pattern, text, flags=re.I) is not None

    async def execute(self, steps: tuple[Step, ...]) -> None:
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=70)
            for index, step in enumerate(steps):
                text = self.render(step.text)
                if step.action == "say":
                    # A partial ASR result is not the end of the agent's turn. Wait for
                    # acoustic silence before ordinary replies; interrupt deliberately bypasses it.
                    if self.voiced:
                        await self.until(lambda: self.now() - self.voiced[-1][0] >= 1.0, 30)
                    await self.play(self.speech[text], text)
                elif step.action == "expect":
                    await self.until(lambda text=text: self.heard(text), step.seconds)
                elif step.action == "digits":
                    await self.until(
                        lambda text=text: (
                            len(self.digits) - self.expected_digit_cursor >= len(text)
                        ),
                        step.seconds,
                    )
                    actual = self.digits[self.expected_digit_cursor :]
                    if actual != text:
                        raise AssertionError(f"wrong received DTMF: {actual}")
                    self.expected_digit_cursor = len(self.digits)
                elif step.action == "pause":
                    await asyncio.sleep(step.seconds)
                elif step.action == "tone":
                    await self.play(tone(step.seconds, (1000,)), "beep")
                elif step.action == "quiet":
                    start, end = await self.play(tone(step.seconds, (220, 330)), "hold tone")
                    speech = sum(d for t, d in self.voiced if start <= t <= end)
                    if speech > 0.3:
                        raise AssertionError("agent spoke during hold")
                    self.event("hold_quiet", seconds=end - start, speech_seconds=speech)
                elif step.action == "interrupt":
                    await self.until(
                        lambda: sum(d for t, d in self.voiced if self.now() - 0.9 <= t) >= 0.7,
                        30,
                    )
                    start, end = await self.play(self.speech[text], text)
                    overlap = sum(d for t, d in self.voiced if start <= t <= start + step.seconds)
                    tail = sum(d for t, d in self.voiced if start + step.seconds < t < end - 0.2)
                    if overlap < 0.06 or tail > 0.2 or end - start < step.seconds + 1:
                        raise AssertionError("interruption lacks overlap or speech did not stop")
                    self.event(
                        "interruption_verified", overlap=overlap, tail=tail, budget=step.seconds
                    )
                elif step.action == "signal":
                    await asyncio.wait_for(self.signals[text].wait(), timeout=step.seconds)
                elif step.action == "hangup":
                    await self.hangup(self.call_sid)
                self.event("step_passed", step=index, action=step.action)
        except Exception as exc:
            self.error = (
                self.error
                or f"scenario:{type(exc).__name__}:step-{index if 'index' in locals() else 'answer'}"
            )
        finally:
            self.finished.set()
            self.save()

    def evidence(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "connected": self.ready.is_set(),
            "closed": self.closed.is_set(),
            "steps_finished": self.finished.is_set(),
            "error": self.error,
            "media_gaps": self.gaps,
            "received_seconds": len(self.rx) / (RATE * 2),
            "voiced_seconds": sum(d for _, d in self.voiced),
            "digits": self.digits,
            "transcripts": self.transcripts,
            "events": self.events,
        }

    def save(self) -> None:
        for label, pcm in (("received", self.rx), ("sent", self.tx)):
            path = self.root / f"{self.role}-{label}.wav"
            path.write_bytes(wav_bytes(bytes(pcm)))
            path.chmod(0o600)
