from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.protocols import DatabaseAccess

from collections.abc import Iterable
from typing import Any

from app.db.engine import _iso_now
from app.models import TERMINAL_STATES, CallState

# Schema columns of `calls` that current callers actually pass to update_call(). Anything
# outside this set is rejected up front instead of failing later as a SQL error.
_UPDATE_CALL_ALLOWED_COLUMNS = frozenset(
    {
        "state",
        "conference_sid",
        "twilio_ai_call_sid",
        "twilio_callee_call_sid",
        "twilio_owner_call_sid",
        "openai_call_id",
        "openai_accept_status",
        "transcription_verified",
        "semantic_vad_verified",
        "callee_dialed",
        "sideband_open",
        "callee_joined",
        "opening_sent",
        "voicemail_sent",
        "termination_claimed",
        "termination_reason",
        "transfer_outcome",
        "advisory_outcome_json",
        "interruption_observed",
        "answered_at",
        "last_event_at",
        "twilio_reported_duration_seconds",
    }
)


class CallsMixin:
    async def get_call(self: DatabaseAccess, call_id: str) -> dict[str, Any] | None:
        return await self.fetch_one("SELECT * FROM calls WHERE call_id=?", (call_id,))

    async def get_call_by_openai_id(
        self: DatabaseAccess, openai_call_id: str
    ) -> dict[str, Any] | None:
        return await self.fetch_one("SELECT * FROM calls WHERE openai_call_id=?", (openai_call_id,))

    async def get_call_by_twilio_sid(self: DatabaseAccess, sid: str) -> dict[str, Any] | None:
        return await self.fetch_one(
            """SELECT * FROM calls
               WHERE twilio_ai_call_sid=? OR twilio_callee_call_sid=?""",
            (sid, sid),
        )

    async def list_calls(self: DatabaseAccess, limit: int = 100) -> list[dict[str, Any]]:
        return await self.fetch_all(
            "SELECT * FROM calls ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    async def list_nonterminal_calls(self: DatabaseAccess) -> list[dict[str, Any]]:
        placeholders, params = self._in_clause(state.value for state in TERMINAL_STATES)
        return await self.fetch_all(
            f"SELECT * FROM calls WHERE state NOT IN ({placeholders})",  # noqa: S608
            params,
        )

    async def list_terminal_calls_needing_finalization(
        self: DatabaseAccess,
    ) -> list[dict[str, Any]]:
        placeholders, params = self._in_clause(state.value for state in TERMINAL_STATES)
        return await self.fetch_all(
            f"""SELECT calls.* FROM calls
                LEFT JOIN call_results ON call_results.call_id = calls.call_id
                WHERE calls.state IN ({placeholders})
                  AND (
                    call_results.call_id IS NULL
                    OR json_extract(call_results.result_json, '$.finalization_status') =
                       'telephony_only'
                  )""",  # noqa: S608
            params,
        )

    async def set_conference_cleanup_pending(
        self: DatabaseAccess, call_id: str, pending: bool
    ) -> None:
        await self.execute(
            "UPDATE calls SET conference_cleanup_pending=? WHERE call_id=?",
            (1 if pending else 0, call_id),
        )

    async def list_conference_cleanup_pending(self: DatabaseAccess) -> list[dict[str, Any]]:
        return await self.fetch_all("SELECT * FROM calls WHERE conference_cleanup_pending=1")

    async def touch_call(self: DatabaseAccess, call_id: str) -> None:
        await self.execute(
            "UPDATE calls SET last_event_at=? WHERE call_id=?", (_iso_now(), call_id)
        )

    async def touch_calls(self: DatabaseAccess, activity: Iterable[tuple[str, str]]) -> None:
        """Persist latest observed activity for several calls in one durable transaction."""
        placeholders, terminal_states = self._in_clause(
            sorted(state.value for state in TERMINAL_STATES)
        )
        rows = [
            (occurred_at, call_id, occurred_at, *terminal_states)
            for call_id, occurred_at in activity
        ]
        if not rows:
            return
        async with self._immediate_transaction() as conn:
            await conn.executemany(
                f"""UPDATE calls SET last_event_at=?
                    WHERE call_id=? AND last_event_at<?
                      AND state NOT IN ({placeholders})""",  # noqa: S608
                rows,
            )

    async def update_call(self: DatabaseAccess, call_id: str, **values: Any) -> bool:
        if not values:
            return True
        unknown = values.keys() - _UPDATE_CALL_ALLOWED_COLUMNS
        if unknown:
            raise ValueError(f"invalid call column(s): {', '.join(sorted(unknown))}")
        values["last_event_at"] = _iso_now()
        columns = ", ".join(f"{key}=?" for key in values)
        params = [
            self._serialize_advisory_outcome(value) if key == "advisory_outcome_json" else value
            for key, value in values.items()
        ]
        params.append(call_id)
        return await self._execute_cas(f"UPDATE calls SET {columns} WHERE call_id=?", params)

    async def bind_openai_call(
        self: DatabaseAccess, *, call_id: str, plan_id: str, openai_call_id: str
    ) -> bool:
        """Atomically bind one expected prewarming call to one OpenAI SIP call."""
        return await self._execute_cas(
            """UPDATE calls SET openai_call_id=?, last_event_at=?
               WHERE call_id=? AND plan_id=? AND state=? AND openai_call_id IS NULL""",
            (openai_call_id, _iso_now(), call_id, plan_id, CallState.PREWARMING.value),
        )

    async def add_realtime_usage(
        self: DatabaseAccess,
        call_id: str,
        *,
        input_text_tokens: int,
        input_audio_tokens: int,
        input_cached_text_tokens: int,
        input_cached_audio_tokens: int,
        output_text_tokens: int,
        output_audio_tokens: int,
    ) -> None:
        await self.execute(
            """UPDATE calls
               SET realtime_input_text_tokens = realtime_input_text_tokens + ?,
                   realtime_input_audio_tokens = realtime_input_audio_tokens + ?,
                   realtime_input_cached_text_tokens = realtime_input_cached_text_tokens + ?,
                   realtime_input_cached_audio_tokens = realtime_input_cached_audio_tokens + ?,
                   realtime_output_text_tokens = realtime_output_text_tokens + ?,
                   realtime_output_audio_tokens = realtime_output_audio_tokens + ?
               WHERE call_id = ?""",
            (
                input_text_tokens,
                input_audio_tokens,
                input_cached_text_tokens,
                input_cached_audio_tokens,
                output_text_tokens,
                output_audio_tokens,
                call_id,
            ),
        )

    async def add_extractor_usage(
        self: DatabaseAccess, call_id: str, *, input_tokens: int, output_tokens: int
    ) -> None:
        await self.execute(
            """UPDATE calls
               SET extractor_input_tokens = extractor_input_tokens + ?,
                   extractor_output_tokens = extractor_output_tokens + ?
               WHERE call_id = ?""",
            (input_tokens, output_tokens, call_id),
        )

    async def record_exa_search(self: DatabaseAccess, call_id: str, *, cost_dollars: float) -> None:
        await self.execute(
            """UPDATE calls
               SET exa_search_count = exa_search_count + 1,
                   exa_cost_dollars = exa_cost_dollars + ?
               WHERE call_id = ?""",
            (cost_dollars, call_id),
        )

    async def cas_state(
        self: DatabaseAccess, call_id: str, expected: CallState, replacement: CallState
    ) -> bool:
        return await self._execute_cas(
            """UPDATE calls SET state=?, last_event_at=?
               WHERE call_id=? AND state=?""",
            (replacement.value, _iso_now(), call_id, expected.value),
        )

    async def set_flag_once(self: DatabaseAccess, call_id: str, flag: str) -> bool:
        allowed = {
            "sideband_open",
            "callee_joined",
            "callee_dialed",
            "opening_sent",
            "voicemail_sent",
            "termination_claimed",
        }
        if flag not in allowed:
            raise ValueError(f"invalid call flag: {flag}")
        return await self._execute_cas(
            f"UPDATE calls SET {flag}=1, last_event_at=? WHERE call_id=? AND {flag}=0",  # noqa: S608
            (_iso_now(), call_id),
        )

    async def set_amd_once(
        self: DatabaseAccess, call_id: str, answered_by: str, handling: str
    ) -> bool:
        return await self._execute_cas(
            """UPDATE calls
               SET amd_result=?, answered_by=?, answer_handling=?, last_event_at=?
               WHERE call_id=? AND amd_result IS NULL""",
            (answered_by, answered_by, handling, _iso_now(), call_id),
        )

    async def claim_opening_if_not_voicemail(self: DatabaseAccess, call_id: str) -> bool:
        """Atomically claim the opening unless AMD has already classified voicemail."""
        return await self._execute_cas(
            """UPDATE calls SET opening_sent=1, last_event_at=?
               WHERE call_id=? AND opening_sent=0 AND answer_handling IS NOT ?""",
            (_iso_now(), call_id, "voicemail"),
        )
