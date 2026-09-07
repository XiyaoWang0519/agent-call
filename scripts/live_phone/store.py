from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Store:
    """Durable single-suite lease and run ownership shared with the external reaper."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "runs.db"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs "
                "(id TEXT PRIMARY KEY, deadline REAL NOT NULL, done INTEGER NOT NULL, data TEXT NOT NULL)"
            )
        self.path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, run_id: str, data: dict[str, Any], seconds: int) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM runs WHERE done=0").fetchone():
                raise ValueError("unfinished run exists; reconcile it before starting another")
            deadline = time.time() + seconds
            data.setdefault("created_at", time.time())
            data.update(id=run_id, deadline=deadline, calls=[], done=False)
            db.execute("INSERT INTO runs VALUES (?, ?, 0, ?)", (run_id, deadline, json.dumps(data)))
        (self.root / run_id).mkdir(mode=0o700)

    def get(self, run_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row[0])

    def update(self, run_id: str, **values: Any) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            data = json.loads(row[0])
            data.update(values)
            db.execute(
                "UPDATE runs SET data=?, done=? WHERE id=?",
                (json.dumps(data), int(data["done"]), run_id),
            )

    def add_call(self, run_id: str, sid: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            data = json.loads(row[0])
            if data["done"] or data["deadline"] <= time.time():
                raise ValueError("reservation expired")
            if sid not in data["calls"]:
                data["calls"].append(sid)
            db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(data), run_id))

    def unfinished(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM runs WHERE done=0")]

    def candidate(self, run_id: str, sid: str, role: str, correlation: dict[str, Any]) -> None:
        """Claim a provider SID once, without consuming the role's media reservation."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            data = None
            for other_id, raw in db.execute("SELECT id, data FROM runs"):
                other = json.loads(raw)
                if other_id == run_id:
                    data = other
                elif sid in other.get("calls", []) or sid in other.get("candidates", {}):
                    raise ValueError("call belongs to an earlier run")
            if (
                data is None
                or data["done"]
                or data.get("finalizing")
                or data["deadline"] <= time.time()
            ):
                raise ValueError("reservation expired")
            candidates = data.setdefault("candidates", {})
            if sid in candidates or len(candidates) >= 8:
                raise ValueError("duplicate or excessive candidate")
            if role in data.get("bindings", {}):
                raise ValueError("role already bound")
            candidates[sid] = {"role": role, **correlation}
            db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(data), run_id))

    def bind_candidate(self, run_id: str, sid: str, digits: str) -> None:
        import secrets

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            raw = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if raw is None:
                raise ValueError("unknown run")
            data = json.loads(raw[0])
            candidate = data.get("candidates", {}).get(sid)
            if (
                not candidate
                or candidate.get("attempted")
                or data["done"]
                or data.get("finalizing")
                or data["deadline"] <= time.time()
                or candidate["role"] in data.get("bindings", {})
            ):
                raise ValueError("invalid candidate")
            candidate["attempted"] = True
            matched = secrets.compare_digest(candidate["digits"], digits)
            if matched:
                data.setdefault("bindings", {})[candidate["role"]] = sid
                data["calls"] = sorted(set(data["calls"]) | {sid})
                candidate["verified"] = True
            db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(data), run_id))
        if not matched:
            raise ValueError("challenge mismatch")

    def candidate_error(
        self, run_id: str, sid: str, error: str, diagnostics: dict[str, Any]
    ) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            raw = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if raw is None:
                return
            data = json.loads(raw[0])
            candidate = data.get("candidates", {}).get(sid)
            if candidate is not None and not candidate.get("verified"):
                candidate["error"] = error
                candidate["diagnostics"] = diagnostics
                db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(data), run_id))

    def remember_resources(self, run_id: str, calls: set[str], conferences: set[str]) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            data = json.loads(row[0])
            data["calls"] = sorted(set(data["calls"]) | calls)
            data["conferences"] = sorted(set(data.get("conferences", [])) | conferences)
            db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(data), run_id))

    def write(self, run_id: str, filename: str, value: Any) -> None:
        path = self.root / run_id / filename
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        path.chmod(0o600)
