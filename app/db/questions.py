from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.protocols import DatabaseAccess

from typing import Any
from uuid import uuid4

import aiosqlite

from app.db.engine import _iso_now
from app.models import CallState

# call_questions has no JSON columns, so rows below are returned as plain dict(row) without
# running them through _decode_json_columns (unlike calls-table RETURNING * rows elsewhere).


class QuestionsMixin:
    async def create_question(
        self: DatabaseAccess,
        call_id: str,
        *,
        tool_call_id: str,
        question: str,
        reason: str | None,
        deadline_at: str,
        max_questions: int,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Insert a pending question. Returns (row, None) or (None, error_code)."""

        question_id = str(uuid4())
        asked_at = _iso_now()
        async with self._write_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT state FROM calls WHERE call_id=?",
                (call_id,),
            )
            call_row = await cursor.fetchone()
            if call_row is None or call_row[0] != CallState.ACTIVE.value:
                await conn.rollback()
                return None, "call_not_active"
            # Same tool_call_id first: a redelivered ask_poke must reuse the pending row
            # instead of returning question_pending and closing the open function call.
            cursor = await conn.execute(
                """SELECT * FROM call_questions
                   WHERE call_id=? AND tool_call_id=?""",
                (call_id, tool_call_id),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                await conn.rollback()
                existing_row = dict(existing)
                if existing_row["status"] == "pending":
                    return existing_row, None
                return None, "duplicate_tool_call"
            cursor = await conn.execute(
                """SELECT 1 FROM call_questions
                   WHERE call_id=? AND status='pending' LIMIT 1""",
                (call_id,),
            )
            if await cursor.fetchone() is not None:
                await conn.rollback()
                return None, "question_pending"
            # Quota is enforced after the same-tool_call_id reuse check so a redelivered
            # ask_poke at the limit is treated as the original ask, not a new rejection.
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM call_questions WHERE call_id=?",
                (call_id,),
            )
            count_row = await cursor.fetchone()
            if count_row is not None and count_row[0] >= max_questions:
                await conn.rollback()
                return None, "question_limit_reached"
            cursor = await conn.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM call_questions WHERE call_id=?",
                (call_id,),
            )
            sequence_row = await cursor.fetchone()
            if sequence_row is None:
                await conn.rollback()
                return None, "sequence_unavailable"
            sequence = sequence_row[0]
            try:
                cursor = await conn.execute(
                    """INSERT INTO call_questions
                       (question_id, call_id, tool_call_id, sequence_number, question, reason,
                        status, answer, asked_at, deadline_at, resolved_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, NULL)
                       RETURNING *""",
                    (
                        question_id,
                        call_id,
                        tool_call_id,
                        sequence,
                        question,
                        reason,
                        asked_at,
                        deadline_at,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                await conn.rollback()
                if "tool_call_id" in str(exc):
                    # UNIQUE(call_id, tool_call_id): a redelivered tool event whose
                    # question already resolved, not a pending-question conflict.
                    return None, "duplicate_tool_call"
                return None, "question_pending"
            row = await cursor.fetchone()
            await conn.commit()
        return dict(row) if row else None, None

    async def claim_question_answer(
        self: DatabaseAccess,
        call_id: str,
        question_id: str,
        answer: str,
    ) -> dict[str, Any] | None:
        resolved_at = _iso_now()
        async with self._write_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT state FROM calls WHERE call_id=?",
                (call_id,),
            )
            call_row = await cursor.fetchone()
            if call_row is None or call_row[0] != CallState.ACTIVE.value:
                await conn.rollback()
                return None
            cursor = await conn.execute(
                """UPDATE call_questions
                   SET status='answered', answer=?, resolved_at=?
                   WHERE question_id=? AND call_id=? AND status='pending'
                   RETURNING *""",
                (answer, resolved_at, question_id, call_id),
            )
            row = await cursor.fetchone()
            if row is None:
                await conn.rollback()
                return None
            await conn.commit()
        return dict(row)

    async def claim_question_expiry(
        self: DatabaseAccess, question_id: str
    ) -> dict[str, Any] | None:
        resolved_at = _iso_now()
        async with self._write_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            # Only active calls may expire questions: once termination claims the call,
            # cancel_pending_questions must win so late answers report call_ended and no
            # timeout tool result is injected into a sideband being torn down.
            cursor = await conn.execute(
                """SELECT c.state FROM call_questions q
                   JOIN calls c ON c.call_id = q.call_id
                   WHERE q.question_id=?""",
                (question_id,),
            )
            call_row = await cursor.fetchone()
            if call_row is None or call_row[0] != CallState.ACTIVE.value:
                await conn.rollback()
                return None
            cursor = await conn.execute(
                """UPDATE call_questions
                   SET status='expired', resolved_at=?
                   WHERE question_id=? AND status='pending'
                   RETURNING *""",
                (resolved_at, question_id),
            )
            row = await cursor.fetchone()
            if row is None:
                await conn.rollback()
                return None
            await conn.commit()
        return dict(row)

    async def cancel_pending_questions(self: DatabaseAccess, call_id: str) -> list[dict[str, Any]]:
        async with self._immediate_transaction() as conn:
            resolved_at = _iso_now()
            cursor = await conn.execute(
                """UPDATE call_questions
                   SET status='cancelled', resolved_at=?
                   WHERE call_id=? AND status='pending'
                   RETURNING *""",
                (resolved_at, call_id),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def cancel_all_pending_questions(self: DatabaseAccess) -> list[dict[str, Any]]:
        async with self._immediate_transaction() as conn:
            resolved_at = _iso_now()
            cursor = await conn.execute(
                """UPDATE call_questions
                   SET status='cancelled', resolved_at=?
                   WHERE status='pending'
                   RETURNING *""",
                (resolved_at,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_question(self: DatabaseAccess, question_id: str) -> dict[str, Any] | None:
        return await self.fetch_one(
            "SELECT * FROM call_questions WHERE question_id=?",
            (question_id,),
        )

    async def get_questions_after(
        self: DatabaseAccess, call_id: str, after_sequence: int
    ) -> list[dict[str, Any]]:
        return await self.fetch_all(
            """SELECT * FROM call_questions
               WHERE call_id=? AND sequence_number > ?
               ORDER BY sequence_number ASC""",
            (call_id, after_sequence),
        )

    async def count_call_questions(self: DatabaseAccess, call_id: str) -> int:
        row = await self.fetch_one(
            "SELECT COUNT(*) AS count FROM call_questions WHERE call_id=?",
            (call_id,),
        )
        return int(row["count"]) if row else 0
