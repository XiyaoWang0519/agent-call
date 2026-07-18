from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.db.protocols import DatabaseAccess

import json
from datetime import datetime

import aiosqlite

from app.db.engine import _iso_now
from app.models import StoredCallResult, TranscriptTurn


class TranscriptsMixin:
    async def add_transcript_turn(
        self: DatabaseAccess,
        *,
        call_id: str,
        turn_id: str,
        speaker: Literal["assistant", "callee", "owner", "system"],
        text: str,
        source_event_type: str,
        source_event_id: str,
    ) -> TranscriptTurn | None:
        async with self._write_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM transcripts WHERE call_id=?",
                (call_id,),
            )
            sequence_row = await cursor.fetchone()
            if sequence_row is None:
                await conn.rollback()
                raise RuntimeError("failed to allocate transcript sequence")
            sequence = sequence_row[0]
            created_at = _iso_now()
            try:
                await conn.execute(
                    """INSERT INTO transcripts
                       (call_id, turn_id, speaker, text, source_event_type, source_event_id,
                        sequence_number, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        call_id,
                        turn_id,
                        speaker,
                        text,
                        source_event_type,
                        source_event_id,
                        sequence,
                        created_at,
                    ),
                )
            except aiosqlite.IntegrityError:
                await conn.rollback()
                return None
            await conn.commit()
        return TranscriptTurn(
            call_id=call_id,
            turn_id=turn_id,
            speaker=speaker,
            text=text,
            source_event_type=source_event_type,
            source_event_id=source_event_id,
            sequence_number=sequence,
            created_at=datetime.fromisoformat(created_at),
        )

    async def get_transcript(self: DatabaseAccess, call_id: str) -> list[TranscriptTurn]:
        rows = await self.fetch_all(
            "SELECT * FROM transcripts WHERE call_id=? ORDER BY sequence_number", (call_id,)
        )
        return [TranscriptTurn.model_validate(row) for row in rows]

    async def save_result_with_transcript(
        self: DatabaseAccess,
        call_id: str,
        result: StoredCallResult,
        transcript: list[TranscriptTurn],
    ) -> None:
        now = _iso_now()
        async with self._immediate_transaction() as conn:
            await conn.execute(
                """INSERT INTO call_results(call_id, result_json, transcript_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(call_id) DO UPDATE SET
                     result_json=excluded.result_json,
                     transcript_json=excluded.transcript_json,
                     updated_at=excluded.updated_at""",
                (
                    call_id,
                    result.model_dump_json(),
                    json.dumps([turn.model_dump(mode="json") for turn in transcript]),
                    now,
                ),
            )

    async def get_result(self: DatabaseAccess, call_id: str) -> StoredCallResult | None:
        row = await self.fetch_one(
            "SELECT result_json FROM call_results WHERE call_id=?", (call_id,)
        )
        return StoredCallResult.model_validate(row["result"]) if row else None
