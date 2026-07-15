from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from time import monotonic_ns
from typing import Any
from uuid import uuid4

import aiosqlite

from app.models import TERMINAL_STATES, CallState, StoredCallResult, TranscriptTurn

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
    xai_call_id TEXT UNIQUE,
    xai_connect_status INTEGER,
    transcription_verified INTEGER NOT NULL DEFAULT 0,
    vad_verified INTEGER NOT NULL DEFAULT 0,
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
    duration_seconds INTEGER
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
"""

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


DEPLOYMENT_LOCK_TTL = timedelta(minutes=15)

# Transfers are legal once the callee has joined, even while async AMD/activation is
# still converging toward 'active'. The promote CAS moves state to terminating
# regardless of which live state it started from.
TRANSFER_ELIGIBLE_STATES = ("prewarming", "ready_to_activate", "activating", "active")


class LatencyStage(StrEnum):
    TWILIO_AGENT_REQUEST = "twilio_agent_request"
    TWILIO_AGENT_CREATED = "twilio_agent_created"
    XAI_CONNECT_REQUEST = "xai_connect_request"
    XAI_CONNECT_COMPLETED = "xai_connect_completed"
    SIDEBAND_OPEN = "sideband_open"
    INITIAL_SESSION_ACK = "initial_session_ack"
    TWILIO_CALLEE_REQUEST = "twilio_callee_request"
    TWILIO_CALLEE_CREATED = "twilio_callee_created"
    CALLEE_ANSWERED = "callee_answered"
    FIRST_RESPONSE_CREATE = "first_response_create"
    FIRST_ASSISTANT_TRANSCRIPT = "first_assistant_transcript"
    FIRST_XAI_AUDIO_DELTA = "first_xai_audio_delta"
    TOOL_CALL_RECEIVED = "tool_call_received"
    TOOL_RESULT_SENT = "tool_result_sent"


@dataclass(frozen=True, slots=True)
class LatencyMark:
    occurred_at: str
    monotonic_ns: int

    @classmethod
    def now(cls) -> LatencyMark:
        return cls(datetime.now(UTC).isoformat(), monotonic_ns())


class DeploymentLockedError(RuntimeError):
    """Raised when a deployment lease temporarily blocks new phone calls."""


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _decode_json_columns(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("context_json", "advisory_outcome_json", "result_json", "transcript_json"):
        if row.get(key) is not None:
            row[key.removesuffix("_json")] = json.loads(row[key])
    return row


class Database:
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
        for old_name, new_name in (
            ("openai_call_id", "xai_call_id"),
            ("openai_accept_status", "xai_connect_status"),
            ("semantic_vad_verified", "vad_verified"),
        ):
            if old_name in existing and new_name not in existing:
                await conn.execute(f"ALTER TABLE calls RENAME COLUMN {old_name} TO {new_name}")
                existing.remove(old_name)
                existing.add(new_name)
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
            "xai_call_id": "TEXT",
            "xai_connect_status": "INTEGER",
            "transcription_verified": "INTEGER NOT NULL DEFAULT 0",
            "vad_verified": "INTEGER NOT NULL DEFAULT 0",
            "tool_call_count": "INTEGER NOT NULL DEFAULT 0",
            "tool_continuation_observed": "INTEGER NOT NULL DEFAULT 0",
            "interruption_observed": "INTEGER NOT NULL DEFAULT 0",
            "opening_sent": "INTEGER NOT NULL DEFAULT 0",
            "twilio_owner_call_sid": "TEXT",
            "conference_cleanup_pending": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in migrations.items():
            if name not in existing:
                await conn.execute(f"ALTER TABLE calls ADD COLUMN {name} {definition}")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS calls_twilio_owner_idx ON calls(twilio_owner_call_sid)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS calls_xai_call_idx ON calls(xai_call_id)"
        )
        await conn.commit()

    @asynccontextmanager
    async def _write_connection(self):
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
    async def _read_connection(self):
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
                rowcount = cursor.rowcount
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

    async def create_plan(
        self,
        plan_id: str,
        context: dict[str, Any],
        authority_basis: str | None,
        expires_at: datetime,
    ) -> None:
        now = _iso_now()
        await self.execute(
            """INSERT INTO plans
               (plan_id, state, context_json, authority_basis, created_at, expires_at)
               VALUES (?, 'prepared', ?, ?, ?, ?)""",
            (plan_id, json.dumps(context), authority_basis, now, expires_at.isoformat()),
        )

    async def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        return await self.fetch_one("SELECT * FROM plans WHERE plan_id = ?", (plan_id,))

    async def claim_plan_and_create_call(
        self,
        *,
        plan_id: str,
        call_id: str,
        conference_name: str,
        confirmation_text: str,
    ) -> bool:
        now = _iso_now()
        async with self._write_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT locked, locked_at FROM deployment_control WHERE singleton=1"
            )
            lock = await cursor.fetchone()
            if lock and lock["locked"]:
                locked_at = datetime.fromisoformat(lock["locked_at"])
                if datetime.now(UTC) - locked_at < DEPLOYMENT_LOCK_TTL:
                    await conn.rollback()
                    raise DeploymentLockedError("deployment is in progress")
                await conn.execute(
                    "UPDATE deployment_control SET locked=0, locked_at=NULL WHERE singleton=1"
                )
            cursor = await conn.execute(
                """UPDATE plans SET state='started', call_id=?, confirmation_text=?
                   WHERE plan_id=? AND state='prepared' AND expires_at>?""",
                (call_id, confirmation_text, plan_id, now),
            )
            if cursor.rowcount != 1:
                await conn.rollback()
                return False
            await conn.execute(
                """INSERT INTO calls
                   (call_id, plan_id, state, conference_name, last_event_at, created_at, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    call_id,
                    plan_id,
                    CallState.PREWARMING.value,
                    conference_name,
                    now,
                    now,
                    now,
                ),
            )
            await conn.commit()
            return True

    async def acquire_deployment_lock(self) -> int:
        """Atomically block new calls if no call is currently nonterminal.

        Returns the number of active calls that prevented acquisition. Zero means
        the deployment lock was acquired.
        """

        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        async with self._write_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                f"SELECT COUNT(*) FROM calls WHERE state NOT IN ({placeholders})",  # noqa: S608
                tuple(state.value for state in TERMINAL_STATES),
            )
            active_calls = int((await cursor.fetchone())[0])
            if active_calls:
                await conn.rollback()
                return active_calls
            await conn.execute(
                """UPDATE deployment_control
                   SET locked=1, locked_at=?
                   WHERE singleton=1""",
                (_iso_now(),),
            )
            await conn.commit()
            return 0

    async def release_deployment_lock(self) -> None:
        await self.execute(
            "UPDATE deployment_control SET locked=0, locked_at=NULL WHERE singleton=1"
        )

    async def deployment_lock_is_active(self) -> bool:
        row = await self.fetch_one(
            "SELECT locked, locked_at FROM deployment_control WHERE singleton=1"
        )
        if not row or not row["locked"] or not row["locked_at"]:
            return False
        return datetime.now(UTC) - datetime.fromisoformat(row["locked_at"]) < DEPLOYMENT_LOCK_TTL

    async def get_call(self, call_id: str) -> dict[str, Any] | None:
        return await self.fetch_one("SELECT * FROM calls WHERE call_id=?", (call_id,))

    async def get_call_by_xai_id(self, xai_call_id: str) -> dict[str, Any] | None:
        return await self.fetch_one("SELECT * FROM calls WHERE xai_call_id=?", (xai_call_id,))

    async def get_call_by_twilio_sid(self, sid: str) -> dict[str, Any] | None:
        return await self.fetch_one(
            """SELECT * FROM calls
               WHERE twilio_ai_call_sid=? OR twilio_callee_call_sid=?""",
            (sid, sid),
        )

    async def list_calls(self, limit: int = 100) -> list[dict[str, Any]]:
        return await self.fetch_all(
            "SELECT * FROM calls ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    async def list_nonterminal_calls(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        return await self.fetch_all(
            f"SELECT * FROM calls WHERE state NOT IN ({placeholders})",  # noqa: S608
            tuple(state.value for state in TERMINAL_STATES),
        )

    async def list_terminal_calls_needing_finalization(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        return await self.fetch_all(
            f"""SELECT calls.* FROM calls
                LEFT JOIN call_results ON call_results.call_id = calls.call_id
                WHERE calls.state IN ({placeholders})
                  AND (
                    call_results.call_id IS NULL
                    OR json_extract(call_results.result_json, '$.finalization_status') =
                       'telephony_only'
                  )""",  # noqa: S608
            tuple(state.value for state in TERMINAL_STATES),
        )

    async def set_conference_cleanup_pending(self, call_id: str, pending: bool) -> None:
        await self.execute(
            "UPDATE calls SET conference_cleanup_pending=? WHERE call_id=?",
            (1 if pending else 0, call_id),
        )

    async def list_conference_cleanup_pending(self) -> list[dict[str, Any]]:
        return await self.fetch_all("SELECT * FROM calls WHERE conference_cleanup_pending=1")

    async def touch_call(self, call_id: str) -> None:
        await self.execute(
            "UPDATE calls SET last_event_at=? WHERE call_id=?", (_iso_now(), call_id)
        )

    async def touch_calls(self, activity: Iterable[tuple[str, str]]) -> None:
        """Persist latest observed activity for several calls in one durable transaction."""
        terminal_states = tuple(sorted(state.value for state in TERMINAL_STATES))
        placeholders = ",".join("?" for _ in terminal_states)
        rows = [
            (occurred_at, call_id, occurred_at, *terminal_states)
            for call_id, occurred_at in activity
        ]
        if not rows:
            return
        async with self._write_connection() as conn:
            await conn.executemany(
                f"""UPDATE calls SET last_event_at=?
                    WHERE call_id=? AND last_event_at<?
                      AND state NOT IN ({placeholders})""",  # noqa: S608
                rows,
            )
            await conn.commit()

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
        async with self._write_connection() as conn:
            await conn.executemany(UPSERT_LATENCY_EVENT, rows)
            await conn.commit()

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

    async def update_call(self, call_id: str, **values: Any) -> bool:
        if not values:
            return True
        values["last_event_at"] = _iso_now()
        columns = ", ".join(f"{key}=?" for key in values)
        params = [
            json.dumps(value) if key == "advisory_outcome_json" and value is not None else value
            for key, value in values.items()
        ]
        params.append(call_id)
        return await self.execute(f"UPDATE calls SET {columns} WHERE call_id=?", params) == 1

    async def cas_state(self, call_id: str, expected: CallState, replacement: CallState) -> bool:
        return (
            await self.execute(
                """UPDATE calls SET state=?, last_event_at=?
                   WHERE call_id=? AND state=?""",
                (replacement.value, _iso_now(), call_id, expected.value),
            )
            == 1
        )

    async def set_flag_once(self, call_id: str, flag: str) -> bool:
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
        return (
            await self.execute(
                f"UPDATE calls SET {flag}=1, last_event_at=? WHERE call_id=? AND {flag}=0",  # noqa: S608
                (_iso_now(), call_id),
            )
            == 1
        )

    async def set_amd_once(self, call_id: str, answered_by: str, handling: str) -> bool:
        return (
            await self.execute(
                """UPDATE calls
                   SET amd_result=?, answered_by=?, answer_handling=?, last_event_at=?
                   WHERE call_id=? AND amd_result IS NULL""",
                (answered_by, answered_by, handling, _iso_now(), call_id),
            )
            == 1
        )

    async def claim_opening_if_not_voicemail(self, call_id: str) -> bool:
        """Atomically claim the opening unless AMD has already classified voicemail."""
        return (
            await self.execute(
                """UPDATE calls SET opening_sent=1, last_event_at=?
                   WHERE call_id=? AND opening_sent=0 AND answer_handling IS NOT ?""",
                (_iso_now(), call_id, "voicemail"),
            )
            == 1
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

        async with self._write_connection() as conn:
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
                    (json.dumps(advisory_outcome), now, call_id),
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
            await conn.commit()

    async def mark_tool_continuation_observed(self, call_id: str) -> bool:
        """Record a continuation only when at least one tool call was durably received."""

        return (
            await self.execute(
                """UPDATE calls
                   SET tool_continuation_observed=1, last_event_at=?
                   WHERE call_id=?
                     AND tool_call_count>0
                     AND tool_continuation_observed=0""",
                (_iso_now(), call_id),
            )
            == 1
        )

    async def claim_transfer_joining(self, call_id: str, reason: str) -> bool:
        """Durably allow at most one owner-transfer attempt for a live call.

        Eligible once the callee has joined, even before activation completes.
        """

        placeholders = ",".join("?" for _ in TRANSFER_ELIGIBLE_STATES)
        return (
            await self.execute(
                f"""UPDATE calls SET transfer_outcome=?, last_event_at=?
                   WHERE call_id=?
                     AND transfer_outcome IS NULL
                     AND termination_claimed=0
                     AND callee_joined=1
                     AND state IN ({placeholders})""",  # noqa: S608
                (f"joining:{reason}", _iso_now(), call_id, *TRANSFER_ELIGIBLE_STATES),
            )
            == 1
        )

    async def record_transfer_owner_sid(
        self, call_id: str, expected: str, owner_call_sid: str
    ) -> bool:
        """Persist the owner leg before transfer promotion can become recoverable."""

        placeholders = ",".join("?" for _ in TRANSFER_ELIGIBLE_STATES)
        return (
            await self.execute(
                f"""UPDATE calls SET twilio_owner_call_sid=?, last_event_at=?
                   WHERE call_id=?
                     AND transfer_outcome=?
                     AND termination_claimed=0
                     AND state IN ({placeholders})
                     AND (twilio_owner_call_sid IS NULL OR twilio_owner_call_sid=?)""",  # noqa: S608
                (
                    owner_call_sid,
                    _iso_now(),
                    call_id,
                    expected,
                    *TRANSFER_ELIGIBLE_STATES,
                    owner_call_sid,
                ),
            )
            == 1
        )

    async def promote_transfer(self, call_id: str, reason: str) -> dict[str, Any] | None:
        """Claim teardown ownership while promoting a joined owner transfer."""

        placeholders = ",".join("?" for _ in TRANSFER_ELIGIBLE_STATES)
        async with self._write_connection() as conn:
            cursor = await conn.execute(
                f"""UPDATE calls
                   SET transfer_outcome=?,
                       termination_claimed=1,
                       state=?,
                       termination_reason='transfer_completed',
                       last_event_at=?
                   WHERE call_id=?
                     AND transfer_outcome=?
                     AND termination_claimed=0
                     AND state IN ({placeholders})
                   RETURNING *""",  # noqa: S608
                (
                    f"in_progress:{reason}",
                    CallState.TERMINATING.value,
                    _iso_now(),
                    call_id,
                    f"joining:{reason}",
                    *TRANSFER_ELIGIBLE_STATES,
                ),
            )
            row = await cursor.fetchone()
            await conn.commit()
        return _decode_json_columns(dict(row)) if row else None

    async def fail_joining_transfer(self, call_id: str, expected: str, failure: str) -> bool:
        placeholders = ",".join("?" for _ in TRANSFER_ELIGIBLE_STATES)
        return (
            await self.execute(
                f"""UPDATE calls SET transfer_outcome=?, last_event_at=?
                   WHERE call_id=?
                     AND transfer_outcome=?
                     AND termination_claimed=0
                     AND state IN ({placeholders})""",  # noqa: S608
                (failure, _iso_now(), call_id, expected, *TRANSFER_ELIGIBLE_STATES),
            )
            == 1
        )

    async def complete_promoted_transfer(self, call_id: str, expected: str, completed: str) -> bool:
        return (
            await self.execute(
                """UPDATE calls SET transfer_outcome=?, last_event_at=?
                   WHERE call_id=?
                     AND transfer_outcome=?
                     AND termination_claimed=1
                     AND state='terminating'
                     AND termination_reason='transfer_completed'""",
                (completed, _iso_now(), call_id, expected),
            )
            == 1
        )

    async def fail_promoted_transfer(
        self,
        call_id: str,
        expected: str,
        failure: str,
        failure_reason: str,
    ) -> bool:
        return (
            await self.execute(
                """UPDATE calls
                   SET transfer_outcome=?, termination_reason=?, last_event_at=?
                   WHERE call_id=?
                     AND transfer_outcome=?
                     AND termination_claimed=1
                     AND state='terminating'
                     AND termination_reason='transfer_completed'""",
                (failure, failure_reason, _iso_now(), call_id, expected),
            )
            == 1
        )

    async def claim_termination(self, call_id: str, reason: str) -> dict[str, Any] | None:
        """Atomically claim and enter termination, returning the claimed current row."""

        async with self._write_connection() as conn:
            cursor = await conn.execute(
                """UPDATE calls
                   SET termination_claimed=1,
                       state=?,
                       termination_reason=?,
                       transfer_outcome=CASE
                           WHEN transfer_outcome LIKE 'joining:%'
                           THEN 'failed:termination_won'
                           ELSE transfer_outcome
                       END,
                       last_event_at=?
                   WHERE call_id=?
                     AND termination_claimed=0
                     AND state NOT IN ('completed','failed','timed_out','transferred')
                     AND COALESCE(transfer_outcome, '') NOT LIKE 'in_progress:%'
                     AND COALESCE(transfer_outcome, '') NOT LIKE 'completed:%'
                   RETURNING *""",
                (CallState.TERMINATING.value, reason, _iso_now(), call_id),
            )
            row = await cursor.fetchone()
            await conn.commit()
        return _decode_json_columns(dict(row)) if row else None

    async def claim_startup_recovery(
        self,
        call_id: str,
        *,
        expected_transfer_outcome: str | None,
        completed_transfer: bool,
    ) -> dict[str, Any] | None:
        """Adopt a stranded nonterminal call without a reset/reclaim race."""

        reason = "transfer_completed" if completed_transfer else "startup_recovery"
        replacement = (
            expected_transfer_outcome
            if completed_transfer
            else ("failed:startup_recovery" if expected_transfer_outcome is not None else None)
        )
        async with self._write_connection() as conn:
            if expected_transfer_outcome is None:
                cursor = await conn.execute(
                    """UPDATE calls
                       SET termination_claimed=1,
                           state='terminating',
                           termination_reason=?,
                           last_event_at=?
                       WHERE call_id=?
                         AND transfer_outcome IS NULL
                         AND state NOT IN ('completed','failed','timed_out','transferred')
                       RETURNING *""",
                    (reason, _iso_now(), call_id),
                )
            else:
                cursor = await conn.execute(
                    """UPDATE calls
                       SET termination_claimed=1,
                           state='terminating',
                           termination_reason=?,
                           transfer_outcome=?,
                           last_event_at=?
                       WHERE call_id=?
                         AND transfer_outcome=?
                         AND state NOT IN ('completed','failed','timed_out','transferred')
                       RETURNING *""",
                    (
                        reason,
                        replacement,
                        _iso_now(),
                        call_id,
                        expected_transfer_outcome,
                    ),
                )
            row = await cursor.fetchone()
            await conn.commit()
        return _decode_json_columns(dict(row)) if row else None

    async def finish_claimed_termination(
        self,
        call_id: str,
        *,
        expected_reason: str,
        terminal_state: CallState,
        ended_at: str,
        duration_seconds: int,
        expected_transfer_outcome: str | None = None,
    ) -> bool:
        params: list[Any] = [
            terminal_state.value,
            ended_at,
            duration_seconds,
            _iso_now(),
            call_id,
            expected_reason,
        ]
        if expected_transfer_outcome is None:
            transfer_predicate = " AND transfer_outcome IS NULL"
        else:
            transfer_predicate = " AND transfer_outcome=?"
            params.append(expected_transfer_outcome)
        return (
            await self.execute(
                f"""UPDATE calls
                    SET state=?, ended_at=?, duration_seconds=?, last_event_at=?
                    WHERE call_id=?
                      AND state='terminating'
                      AND termination_claimed=1
                      AND termination_reason=?{transfer_predicate}""",  # noqa: S608
                params,
            )
            == 1
        )

    async def reset_termination_claim(self, call_id: str) -> None:
        await self.execute(
            """UPDATE calls SET termination_claimed=0
               WHERE call_id=? AND state NOT IN ('completed','failed','timed_out','transferred')""",
            (call_id,),
        )

    async def record_webhook_once(self, webhook_id: str) -> bool:
        try:
            return (
                await self.execute(
                    "INSERT INTO webhook_deliveries(webhook_id, received_at) VALUES (?, ?)",
                    (webhook_id, _iso_now()),
                )
                == 1
            )
        except aiosqlite.IntegrityError:
            return False

    async def add_transcript_turn(
        self,
        *,
        call_id: str,
        turn_id: str,
        speaker: str,
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
            sequence = (await cursor.fetchone())[0]
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

    async def get_transcript(self, call_id: str) -> list[TranscriptTurn]:
        rows = await self.fetch_all(
            "SELECT * FROM transcripts WHERE call_id=? ORDER BY sequence_number", (call_id,)
        )
        return [TranscriptTurn.model_validate(row) for row in rows]

    async def save_result_with_transcript(
        self,
        call_id: str,
        result: StoredCallResult,
        transcript: list[TranscriptTurn],
    ) -> None:
        now = _iso_now()
        async with self._write_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
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
            await conn.commit()

    async def get_result(self, call_id: str) -> StoredCallResult | None:
        row = await self.fetch_one(
            "SELECT result_json FROM call_results WHERE call_id=?", (call_id,)
        )
        return StoredCallResult.model_validate(row["result"]) if row else None
