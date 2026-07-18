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

    async def record_latency_events(
        self,
        call_id: str,
        events: Iterable[tuple[Any, Any, str]],
    ) -> None: ...
