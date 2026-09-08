from __future__ import annotations

import time
from collections import defaultdict


class FailedAttemptLimiter:
    """Process-local limiter for failed owner-secret attempts."""

    def __init__(self, *, max_failures: int, window_seconds: float) -> None:
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._failures: dict[str, list[float]] = defaultdict(list)

    def _prune(self, key: str, now: float) -> list[float]:
        cutoff = now - self._window_seconds
        kept = [stamp for stamp in self._failures[key] if stamp > cutoff]
        if kept:
            self._failures[key] = kept
        else:
            self._failures.pop(key, None)
        return kept

    def is_blocked(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self._max_failures

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        failures = self._prune(key, now)
        failures.append(now)
        self._failures[key] = failures

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)
