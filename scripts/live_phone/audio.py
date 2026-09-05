from __future__ import annotations

import io
import math
import struct
import wave
from collections import deque

RATE = 8000


def decode_mulaw(data: bytes) -> bytes:
    samples = []
    for byte in data:
        value = (~byte) & 255
        sample = (((value & 15) << 3) + 132) << ((value >> 4) & 7)
        samples.append(132 - sample if value & 128 else sample - 132)
    return struct.pack(f"<{len(samples)}h", *samples)


def encode_mulaw(pcm: bytes) -> bytes:
    result = bytearray()
    for (value,) in struct.iter_unpack("<h", pcm):
        sign = 128 if value < 0 else 0
        value = min(abs(value), 32635) + 132
        exponent = max(0, min(7, value.bit_length() - 8))
        result.append((~(sign | exponent << 4 | (value >> (exponent + 3) & 15))) & 255)
    return bytes(result)


def pcm24_to_8(pcm: bytes) -> bytes:
    """Average three 24 kHz samples per telephone sample; no removed audioop dependency."""
    samples = [v[0] for v in struct.iter_unpack("<h", pcm)]
    averaged = [round(sum(samples[i : i + 3]) / 3) for i in range(0, len(samples) - 2, 3)]
    return struct.pack(f"<{len(averaged)}h", *averaged)


def wav_bytes(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(RATE)
        stream.writeframes(pcm)
    return out.getvalue()


def rms(pcm: bytes) -> float:
    samples = [v[0] for v in struct.iter_unpack("<h", pcm)]
    return math.sqrt(sum(v * v for v in samples) / len(samples)) if samples else 0.0


def tone(seconds: float, frequencies: tuple[int, ...] = (440,)) -> bytes:
    samples = [
        int(
            5000 * sum(math.sin(2 * math.pi * f * i / RATE) for f in frequencies) / len(frequencies)
        )
        for i in range(int(seconds * RATE))
    ]
    return struct.pack(f"<{len(samples)}h", *samples)


class DigitDetector:
    """In-band DTMF, 40 ms windows with minimum duration and release debounce."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.recent: deque[str | None] = deque(maxlen=2)
        self.latched: str | None = None

    @staticmethod
    def detect(pcm: bytes) -> str | None:
        samples = [v[0] for v in struct.iter_unpack("<h", pcm)]
        energy = sum(v * v for v in samples)
        if energy < len(samples) * 250**2:
            return None
        powers = []
        for freq in (697, 770, 852, 941, 1209, 1336, 1477, 1633):
            coeff = 2 * math.cos(2 * math.pi * freq / RATE)
            a = b = 0.0
            for sample in samples:
                a, b = sample + coeff * a - b, a
            powers.append(a * a + b * b - coeff * a * b)
        lo = max(range(4), key=lambda i: powers[i])
        hi = max(range(4, 8), key=lambda i: powers[i])
        if not 0.2 <= powers[lo] / max(powers[hi], 1) <= 5:
            return None
        if (powers[lo] + powers[hi]) / (len(samples) * energy) < 0.35:
            return None
        if any(powers[lo] < 4 * powers[i] for i in range(4) if i != lo):
            return None
        if any(powers[hi] < 4 * powers[i] for i in range(4, 8) if i != hi):
            return None
        return ("123A", "456B", "789C", "*0#D")[lo][hi - 4]

    def feed(self, pcm: bytes) -> list[str]:
        self.buffer.extend(pcm)
        found = []
        while len(self.buffer) >= 640:
            digit = self.detect(bytes(self.buffer[:640]))
            del self.buffer[:640]
            self.recent.append(digit)
            if len(self.recent) == 2 and all(v == digit for v in self.recent):
                if digit is not None and digit != self.latched:
                    found.append(digit)
                self.latched = digit
        return found
