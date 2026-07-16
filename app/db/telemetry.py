from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic_ns
from typing import Any

from app.db.engine import _iso_now

UPSERT_LATENCY_EVENT = """
INSERT INTO call_latency_events
    (call_id, stage, event_key, occurred_at, monotonic_ns, clock_id)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(call_id, stage, event_key) DO UPDATE SET
    occurred_at=excluded.occurred_at,
    monotonic_ns=excluded.monotonic_ns,
    clock_id=excluded.clock_id
WHERE
    (
        call_latency_events.clock_id=excluded.clock_id
        AND excluded.monotonic_ns < call_latency_events.monotonic_ns
    )
    OR
    (
        call_latency_events.clock_id<>excluded.clock_id
        AND excluded.occurred_at < call_latency_events.occurred_at
    )
"""


class LatencyStage(StrEnum):
    TWILIO_AGENT_REQUEST = "twilio_agent_request"
    TWILIO_AGENT_CREATED = "twilio_agent_created"
    OPENAI_ACCEPT_REQUEST = "openai_accept_request"
    OPENAI_ACCEPT_COMPLETED = "openai_accept_completed"
    SIDEBAND_OPEN = "sideband_open"
    INITIAL_SESSION_ACK = "initial_session_ack"
    TWILIO_CALLEE_REQUEST = "twilio_callee_request"
    TWILIO_CALLEE_CREATED = "twilio_callee_created"
    CALLEE_ANSWERED = "callee_answered"
    FIRST_RESPONSE_CREATE = "first_response_create"
    FIRST_ASSISTANT_TRANSCRIPT = "first_assistant_transcript"
    FIRST_OPENAI_AUDIO_DELTA = "first_openai_audio_delta"
    TOOL_CALL_RECEIVED = "tool_call_received"
    EXA_SEARCH_STARTED = "exa_search_started"
    EXA_SEARCH_COMPLETED = "exa_search_completed"
    ASK_POKE_ASKED = "ask_poke_asked"
    ASK_POKE_RESOLVED = "ask_poke_resolved"
    TOOL_RESULT_SENT = "tool_result_sent"


@dataclass(frozen=True, slots=True)
class LatencyMark:
    occurred_at: str
    monotonic_ns: int

    @classmethod
    def now(cls) -> LatencyMark:
        return cls(datetime.now(UTC).isoformat(), monotonic_ns())


class TelemetryMixin:
    async def record_latency_events(
        self,
        call_id: str,
        events: Iterable[tuple[LatencyStage, LatencyMark, str]],
    ) -> None:
        rows = [
            (
                call_id,
                stage.value,
                event_key,
                mark.occurred_at,
                mark.monotonic_ns,
                self._latency_clock_id,
            )
            for stage, mark, event_key in events
        ]
        if not rows:
            return
        async with self._immediate_transaction() as conn:
            await conn.executemany(UPSERT_LATENCY_EVENT, rows)

    async def record_latency_event(
        self,
        call_id: str,
        stage: LatencyStage,
        mark: LatencyMark | None = None,
        *,
        event_key: str = "",
    ) -> None:
        await self.record_latency_events(
            call_id,
            [(stage, mark or LatencyMark.now(), event_key)],
        )

    async def get_latency_events(self, call_id: str) -> list[dict[str, Any]]:
        return await self.fetch_all(
            """SELECT stage, event_key, occurred_at, monotonic_ns, clock_id
               FROM call_latency_events WHERE call_id=? ORDER BY occurred_at, id""",
            (call_id,),
        )

    async def record_tool_call(
        self,
        call_id: str,
        *,
        latency_mark: LatencyMark,
        event_key: str = "",
        advisory_outcome: dict[str, Any] | None = None,
    ) -> None:
        """Persist one tool receipt and its optional validated advisory in one commit."""

        async with self._immediate_transaction() as conn:
            now = _iso_now()
            if advisory_outcome is None:
                await conn.execute(
                    """UPDATE calls SET tool_call_count=tool_call_count+1, last_event_at=?
                       WHERE call_id=?""",
                    (now, call_id),
                )
            else:
                await conn.execute(
                    """UPDATE calls
                       SET tool_call_count=tool_call_count+1,
                           advisory_outcome_json=?,
                           last_event_at=?
                       WHERE call_id=?""",
                    (self._serialize_advisory_outcome(advisory_outcome), now, call_id),
                )
            await conn.execute(
                UPSERT_LATENCY_EVENT,
                (
                    call_id,
                    LatencyStage.TOOL_CALL_RECEIVED.value,
                    event_key,
                    latency_mark.occurred_at,
                    latency_mark.monotonic_ns,
                    self._latency_clock_id,
                ),
            )

    async def mark_tool_continuation_observed(self, call_id: str) -> bool:
        """Record a continuation only when at least one tool call was durably received."""

        return await self._execute_cas(
            """UPDATE calls
               SET tool_continuation_observed=1, last_event_at=?
               WHERE call_id=?
                 AND tool_call_count>0
                 AND tool_continuation_observed=0""",
            (_iso_now(), call_id),
        )
