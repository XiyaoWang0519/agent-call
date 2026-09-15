"""Typing protocol for the composed Database facade used by mixins.

At runtime each concern mixin is composed onto ``DatabaseEngine`` via
``app.db.Database``. Declaring the shared engine surface here lets mypy
understand ``self.execute`` / ``self.fetch_one`` calls inside mixins without
forcing mixins to inherit ``DatabaseEngine`` (which would break the MRO).
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

import aiosqlite

from app.models import CallState


class DatabaseAccess(Protocol):
    """Minimal engine surface mixins rely on."""

    _latency_clock_id: str

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int: ...

    async def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None: ...

    async def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]: ...

    async def _execute_cas(self, sql: str, params: Iterable[Any] = ()) -> bool: ...

    def _in_clause(self, values: Iterable[Any]) -> tuple[str, tuple[Any, ...]]: ...

    def _write_connection(self) -> AbstractAsyncContextManager[aiosqlite.Connection]: ...

    def _read_connection(self) -> AbstractAsyncContextManager[aiosqlite.Connection]: ...

    def _immediate_transaction(self) -> AbstractAsyncContextManager[aiosqlite.Connection]: ...

    def _serialize_advisory_outcome(self, value: dict[str, Any] | None) -> Any: ...

    # Sibling-mixin reads a typed adapter may delegate to. Declared here for the
    # same MRO reason as the engine surface above.
    async def get_plan(self, plan_id: str) -> dict[str, Any] | None: ...

    async def get_call(self, call_id: str) -> dict[str, Any] | None: ...

    async def create_question(
        self,
        call_id: str,
        *,
        tool_call_id: str,
        question: str,
        reason: str | None,
        deadline_at: str,
        max_questions: int,
    ) -> tuple[dict[str, Any] | None, str | None]: ...

    async def claim_question_answer(
        self, call_id: str, question_id: str, answer: str
    ) -> dict[str, Any] | None: ...

    async def claim_question_expiry(self, question_id: str) -> dict[str, Any] | None: ...

    async def get_question(self, question_id: str) -> dict[str, Any] | None: ...

    async def get_questions_after(
        self, call_id: str, after_sequence: int
    ) -> list[dict[str, Any]]: ...

    async def cancel_pending_questions(self, call_id: str) -> list[dict[str, Any]]: ...

    async def _promote_call_state(
        self, call_id: str, expected: CallState, replacement: CallState
    ) -> bool: ...

    async def record_latency_events(
        self,
        call_id: str,
        events: Iterable[tuple[Any, Any, str]],
    ) -> None: ...

    async def oauth_record_audit(self, event: str, extra: dict[str, Any] | None = None) -> None: ...

    async def oauth_revoke_family(self, family_id: str) -> None: ...
