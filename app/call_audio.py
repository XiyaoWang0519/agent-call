"""Observe carrier audio without putting another relay in the speech path.

Twilio forks the callee's inbound and outbound tracks. Outbound samples establish
carrier playback progress, unlike Live transcripts or backend completion. This is
not a physical-handset receipt. The live-phone harness measures received audio too.
"""

from __future__ import annotations

import base64
import binascii
import math
import time
from dataclasses import dataclass, field


def _decode_mulaw(value: int) -> int:
    value = ~value & 255
    magnitude = (((value & 15) << 3) + 132) << ((value & 112) >> 4)
    return 132 - magnitude if value & 128 else magnitude - 132


_MULAW_SQUARES = tuple(_decode_mulaw(value) ** 2 for value in range(256))


@dataclass(slots=True)
class AudioTrack:
    end_ms: float = -1
    received_at: float = 0
    last_voice_ms: float | None = None
    voice_samples: int = 0
    speaking: bool = False
    discontinuity: bool = False

    def observe(self, timestamp_ms: int, payload: str, *, now: float) -> tuple[bool, bool]:
        if timestamp_ms < 0 or len(payload) > 16_384:
            raise ValueError("invalid carrier audio frame")
        try:
            audio = base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid carrier audio encoding") from exc
        if not audio:
            return False, False
        if timestamp_ms < self.end_ms - 1:
            # Duplicate or reordered media cannot advance playback or a closing clock.
            return False, False
        gap = self.end_ms >= 0 and timestamp_ms > self.end_ms + 40
        if gap:
            self.last_voice_ms = None
            self.voice_samples = 0
            self.discontinuity = True
        self.end_ms = timestamp_ms + len(audio) / 8
        self.received_at = now
        rms = math.sqrt(sum(_MULAW_SQUARES[sample] for sample in audio) / len(audio))
        started = stopped = False
        if rms >= 180:
            self.last_voice_ms = self.end_ms
            self.voice_samples += len(audio)
            if not self.speaking and self.voice_samples >= 320:
                self.speaking = True
                started = True
            self.discontinuity = False
        else:
            self.voice_samples = 0
            if (
                self.speaking
                and self.last_voice_ms is not None
                and self.end_ms - self.last_voice_ms >= 300
            ):
                self.speaking = False
                stopped = True
        return started, stopped

    def silence_seconds(self, *, now: float) -> float | None:
        if self.last_voice_ms is None or self.discontinuity or now - self.received_at > 0.5:
            return None
        return max(0.0, (self.end_ms - self.last_voice_ms) / 1000)


@dataclass(slots=True)
class CallAudio:
    inbound: AudioTrack = field(default_factory=AudioTrack)
    outbound: AudioTrack = field(default_factory=AudioTrack)
    stream_sid: str | None = None
    connected: bool = False

    def observe(self, track: str, timestamp_ms: int, payload: str) -> tuple[bool, bool]:
        if track not in {"inbound", "outbound"}:
            raise ValueError("invalid carrier track")
        return getattr(self, track).observe(timestamp_ms, payload, now=time.monotonic())  # type: ignore[no-any-return]
