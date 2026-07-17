"""Low-level SQLite engine: schema, connection lifecycle, and shared write/read helpers.

`DatabaseEngine` owns everything about talking to sqlite (connections, migrations,
transaction/cancellation semantics). The concern-specific mixins in the sibling modules
(`plans`, `deployment`, `calls`, ...) are composed onto it by `app.db.Database`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.models import CallState

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    context_json TEXT NOT NULL,
    authority_basis TEXT,
    confirmation_text TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    call_id TEXT
);

CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL UNIQUE REFERENCES plans(plan_id),
    state TEXT NOT NULL,
    conference_name TEXT NOT NULL UNIQUE,
    conference_sid TEXT,
    twilio_ai_call_sid TEXT,
    twilio_callee_call_sid TEXT,
    twilio_owner_call_sid TEXT,
    openai_call_id TEXT UNIQUE,
    openai_accept_status INTEGER,
    transcription_verified INTEGER NOT NULL DEFAULT 0,
    semantic_vad_verified INTEGER NOT NULL DEFAULT 0,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    tool_continuation_observed INTEGER NOT NULL DEFAULT 0,
    interruption_observed INTEGER NOT NULL DEFAULT 0,
    sideband_open INTEGER NOT NULL DEFAULT 0,
    callee_joined INTEGER NOT NULL DEFAULT 0,
    callee_dialed INTEGER NOT NULL DEFAULT 0,
    amd_result TEXT,
    answered_by TEXT,
    answer_handling TEXT,
    opening_sent INTEGER NOT NULL DEFAULT 0,
    voicemail_sent INTEGER NOT NULL DEFAULT 0,
    termination_claimed INTEGER NOT NULL DEFAULT 0,
    termination_reason TEXT,
    conference_cleanup_pending INTEGER NOT NULL DEFAULT 0,
    advisory_outcome_json TEXT,
    transfer_outcome TEXT,
    last_event_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    answered_at TEXT,
    ended_at TEXT,
    duration_seconds INTEGER,
    realtime_input_text_tokens INTEGER NOT NULL DEFAULT 0,
    realtime_input_audio_tokens INTEGER NOT NULL DEFAULT 0,
    realtime_input_cached_text_tokens INTEGER NOT NULL DEFAULT 0,
    realtime_input_cached_audio_tokens INTEGER NOT NULL DEFAULT 0,
    realtime_output_text_tokens INTEGER NOT NULL DEFAULT 0,
    realtime_output_audio_tokens INTEGER NOT NULL DEFAULT 0,
    extractor_input_tokens INTEGER NOT NULL DEFAULT 0,
    extractor_output_tokens INTEGER NOT NULL DEFAULT 0,
    exa_search_count INTEGER NOT NULL DEFAULT 0,
    exa_cost_dollars REAL NOT NULL DEFAULT 0,
    twilio_reported_duration_seconds INTEGER
);

CREATE INDEX IF NOT EXISTS calls_state_idx ON calls(state);
CREATE INDEX IF NOT EXISTS calls_twilio_ai_idx ON calls(twilio_ai_call_sid);
CREATE INDEX IF NOT EXISTS calls_twilio_callee_idx ON calls(twilio_callee_call_sid);

CREATE TABLE IF NOT EXISTS call_latency_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES calls(call_id),
    stage TEXT NOT NULL,
    event_key TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    monotonic_ns INTEGER NOT NULL,
    clock_id TEXT NOT NULL,
    UNIQUE(call_id, stage, event_key)
);

CREATE INDEX IF NOT EXISTS call_latency_events_call_idx
    ON call_latency_events(call_id, id);

CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES calls(call_id),
    turn_id TEXT NOT NULL,
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    source_event_type TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    sequence_number INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(call_id, source_event_id),
    UNIQUE(call_id, sequence_number)
);

CREATE TABLE IF NOT EXISTS call_results (
    call_id TEXT PRIMARY KEY REFERENCES calls(call_id),
    result_json TEXT NOT NULL,
    transcript_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    webhook_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deployment_control (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    locked INTEGER NOT NULL DEFAULT 0,
    locked_at TEXT
);

INSERT OR IGNORE INTO deployment_control (singleton, locked) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS call_questions (
    question_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES calls(call_id),
    tool_call_id TEXT NOT NULL,
    sequence_number INTEGER NOT NULL,
    question TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    answer TEXT,
    asked_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE (call_id, sequence_number),
    UNIQUE (call_id, tool_call_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_call_questions_one_pending
    ON call_questions(call_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS call_questions_call_seq_idx
    ON call_questions(call_id, sequence_number);
"""


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _decode_json_columns(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("context_json", "advisory_outcome_json", "result_json", "transcript_json"):
        if row.get(key) is not None:
            row[key.removesuffix("_json")] = json.loads(row[key])
    return row


class DatabaseEngine:
    def __init__(self, path: Path):
        self.path = path
        self._lifecycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        self._writer: aiosqlite.Connection | None = None
        self._reader: aiosqlite.Connection | None = None
        # Monotonic values are comparable only when this process-local clock ID matches.
        self._latency_clock_id = uuid4().hex

    async def initialize(self) -> None:
        async with self._lifecycle_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if (self._writer is None) != (self._reader is None):
                raise RuntimeError("database connection state is inconsistent")
            created_connections = self._writer is None
            if created_connections:
                writer: aiosqlite.Connection | None = None
                reader: aiosqlite.Connection | None = None
                try:
                    writer = await self._connect(query_only=False)
                    reader = await self._connect(query_only=True)
                except BaseException:
                    if reader is not None:
                        await reader.close()
                    if writer is not None:
                        await writer.close()
                    raise
                self._writer = writer
                self._reader = reader

            try:
                async with self._write_connection() as conn:
                    await self._run_migrations(conn)
            except BaseException:
                if created_connections:
                    await self._close_connections()
                raise

    async def _connect(self, *, query_only: bool) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.path)
        try:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=FULL")
            if query_only:
                await conn.execute("PRAGMA query_only=ON")
            return conn
        except BaseException:
            await conn.close()
            raise

    async def _run_migrations(self, conn: aiosqlite.Connection) -> None:
        await conn.executescript(SCHEMA)
        async with conn.execute("PRAGMA table_info(calls)") as cursor:
            existing = {row[1] for row in await cursor.fetchall()}
        for legacy_name, current_name in (
            ("xai_call_id", "openai_call_id"),
            ("xai_connect_status", "openai_accept_status"),
            ("vad_verified", "semantic_vad_verified"),
        ):
            if legacy_name in existing and current_name not in existing:
                await conn.execute(
                    f"ALTER TABLE calls RENAME COLUMN {legacy_name} TO {current_name}"
                )
                existing.remove(legacy_name)
                existing.add(current_name)
        if "greeting_sent" in existing and "opening_sent" not in existing:
            await conn.execute("ALTER TABLE calls RENAME COLUMN greeting_sent TO opening_sent")
            existing.remove("greeting_sent")
            existing.add("opening_sent")
        await conn.execute(
            "UPDATE calls SET state=? WHERE state=?",
            (
                CallState.ACTIVE.value,
                "greeting_started",
            ),
        )
        migrations = {
            "openai_accept_status": "INTEGER",
            "transcription_verified": "INTEGER NOT NULL DEFAULT 0",
            "semantic_vad_verified": "INTEGER NOT NULL DEFAULT 0",
            "tool_call_count": "INTEGER NOT NULL DEFAULT 0",
            "tool_continuation_observed": "INTEGER NOT NULL DEFAULT 0",
            "interruption_observed": "INTEGER NOT NULL DEFAULT 0",
            "opening_sent": "INTEGER NOT NULL DEFAULT 0",
            "twilio_owner_call_sid": "TEXT",
            "conference_cleanup_pending": "INTEGER NOT NULL DEFAULT 0",
            "realtime_input_text_tokens": "INTEGER NOT NULL DEFAULT 0",
            "realtime_input_audio_tokens": "INTEGER NOT NULL DEFAULT 0",
            "realtime_input_cached_text_tokens": "INTEGER NOT NULL DEFAULT 0",
            "realtime_input_cached_audio_tokens": "INTEGER NOT NULL DEFAULT 0",
            "realtime_output_text_tokens": "INTEGER NOT NULL DEFAULT 0",
            "realtime_output_audio_tokens": "INTEGER NOT NULL DEFAULT 0",
            "extractor_input_tokens": "INTEGER NOT NULL DEFAULT 0",
            "extractor_output_tokens": "INTEGER NOT NULL DEFAULT 0",
            "exa_search_count": "INTEGER NOT NULL DEFAULT 0",
            "exa_cost_dollars": "REAL NOT NULL DEFAULT 0",
            "twilio_reported_duration_seconds": "INTEGER",
        }
        for name, definition in migrations.items():
            if name not in existing:
                await conn.execute(f"ALTER TABLE calls ADD COLUMN {name} {definition}")
                existing.add(name)
        if "xai_call_id" in existing:
            await conn.execute(
                "UPDATE calls SET openai_call_id=COALESCE(openai_call_id, xai_call_id)"
            )
        if "xai_connect_status" in existing:
            await conn.execute(
                """UPDATE calls
                   SET openai_accept_status=COALESCE(openai_accept_status, xai_connect_status)"""
            )
        if "vad_verified" in existing:
            await conn.execute(
                """UPDATE calls
                   SET semantic_vad_verified=MAX(semantic_vad_verified, vad_verified)"""
            )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS calls_twilio_owner_idx ON calls(twilio_owner_call_sid)"
        )
        await conn.commit()

    @asynccontextmanager
    async def _write_connection(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            conn = self._writer
            if conn is None:
                raise RuntimeError("database is not initialized")
            try:
                yield conn
            except BaseException:
                # aiosqlite queues work on a worker thread. Cancellation can arrive after an
                # operation is queued but before it starts, when in_transaction is still false.
                # Queue an unconditional rollback behind that work and keep the lock until the
                # rollback finishes so the next writer never inherits the abandoned transaction.
                await self._rollback_writer(conn)
                raise
            else:
                # Do not let a missed commit poison the persistent writer for the
                # next operation. Transactional methods commit explicitly.
                if conn.in_transaction:
                    await self._rollback_writer(conn)

    @staticmethod
    async def _rollback_writer(conn: aiosqlite.Connection) -> None:
        rollback = asyncio.create_task(conn.rollback(), name="sqlite-writer-rollback")
        interrupted = False
        while not rollback.done():
            try:
                await asyncio.shield(rollback)
            except asyncio.CancelledError:
                # A repeated cancellation must still not release the writer lock ahead of the
                # queued rollback. Re-raise it once the connection has been restored.
                interrupted = True
        rollback.result()
        if interrupted:
            raise asyncio.CancelledError

    @asynccontextmanager
    async def _read_connection(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._read_lock:
            conn = self._reader
            if conn is None:
                raise RuntimeError("database is not initialized")
            yield conn

    async def close(self) -> None:
        """Close both persistent connections; safe to call more than once."""
        async with self._lifecycle_lock:
            await self._close_connections()

    async def _close_connections(self) -> None:
        async with self._write_lock, self._read_lock:
            writer = self._writer
            reader = self._reader
            self._writer = None
            self._reader = None

            first_error: BaseException | None = None
            for conn in (reader, writer):
                if conn is None:
                    continue
                try:
                    await conn.close()
                except BaseException as exc:  # pragma: no cover - defensive cleanup
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self._write_connection() as conn:
            async with conn.execute(sql, tuple(params)) as cursor:
                rowcount = int(cursor.rowcount)
            await conn.commit()
            return rowcount

    async def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        async with self._read_connection() as conn:
            async with conn.execute(sql, tuple(params)) as cursor:
                row = await cursor.fetchone()
        return _decode_json_columns(dict(row)) if row else None

    async def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        async with self._read_connection() as conn:
            async with conn.execute(sql, tuple(params)) as cursor:
                rows = await cursor.fetchall()
        return [_decode_json_columns(dict(row)) for row in rows]

    # --- shared helpers used by the concern mixins -------------------------------------

    async def _execute_cas(self, sql: str, params: Iterable[Any] = ()) -> bool:
        """Run a single-row conditional UPDATE/INSERT and report whether it matched."""
        return await self.execute(sql, params) == 1

    @staticmethod
    def _in_clause(values: Iterable[Any]) -> tuple[str, tuple[Any, ...]]:
        """Build a `?,?,...` placeholder string alongside the matching params tuple."""
        values = tuple(values)
        return ",".join("?" for _ in values), values

    @asynccontextmanager
    async def _immediate_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """BEGIN IMMEDIATE, run the body, commit on normal exit.

        Only fits blocks that always commit once entered (no conditional early-return
        rollback with an alternate result). Rollback-on-exception is already handled by
        `_write_connection`'s cancellation-safe exception path, so this helper does not
        duplicate it.
        """
        async with self._write_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            yield conn
            await conn.commit()

    @staticmethod
    def _serialize_advisory_outcome(value: dict[str, Any] | None) -> Any:
        return json.dumps(value) if value is not None else value
