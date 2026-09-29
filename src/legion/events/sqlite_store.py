from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from legion.canonical import GENESIS_HASH, canonical_json
from legion.events.store import (
    ConcurrentAppend,
    RunSummary,
    VerifyResult,
    _check_single_run,
    seal,
    summarize,
    verify_bodies,
)
from legion.events.types import Event, EventDraft

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    run_id    TEXT    NOT NULL,
    seq       INTEGER NOT NULL,
    event_id  TEXT    NOT NULL UNIQUE,
    ts        TEXT    NOT NULL,
    type      TEXT    NOT NULL,
    task_id   TEXT,
    body      TEXT    NOT NULL,
    prev_hash TEXT    NOT NULL,
    hash      TEXT    NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS events_type ON events (type);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


# The triggers only stop casual edits. `verify` is what actually catches tampering.
class SqliteEventStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def append(
        self, drafts: Sequence[EventDraft], *, expected_seq: int | None = None
    ) -> list[Event]:
        if not drafts:
            return []
        return await asyncio.to_thread(self._append, drafts, expected_seq)

    def _append(self, drafts: Sequence[EventDraft], expected_seq: int | None) -> list[Event]:
        run_id = _check_single_run(drafts)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT seq, hash FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                last_seq, prev = (row[0], row[1]) if row else (0, GENESIS_HASH)
                if expected_seq is not None and expected_seq != last_seq:
                    raise ConcurrentAppend(f"expected seq {expected_seq}, log is at {last_seq}")
                sealed = []
                for offset, draft in enumerate(drafts, start=1):
                    event = seal(draft, last_seq + offset, prev)
                    self._conn.execute(
                        "INSERT INTO events (run_id, seq, event_id, ts, type, task_id, body, "
                        "prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            event.seq,
                            event.event_id,
                            event.body()["ts"],
                            event.type.value,
                            event.task_id,
                            canonical_json(event.body()),
                            event.prev_hash,
                            event.hash,
                        ),
                    )
                    sealed.append(event)
                    prev = event.hash
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return sealed

    async def read(self, run_id: str, *, after_seq: int = 0) -> list[Event]:
        return await asyncio.to_thread(self._read, run_id, after_seq)

    def _read(self, run_id: str, after_seq: int) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT body, hash FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [Event.model_validate({**_load(body), "hash": h}) for body, h in rows]

    async def runs(self) -> list[RunSummary]:
        return await asyncio.to_thread(self._runs)

    def _runs(self) -> list[RunSummary]:
        with self._lock:
            run_ids = [
                r[0]
                for r in self._conn.execute(
                    "SELECT run_id FROM events WHERE seq = 1 ORDER BY ts"
                ).fetchall()
            ]
        return [summarize(run_id, self._read(run_id, 0)) for run_id in run_ids]

    async def verify(self, run_id: str) -> VerifyResult:
        return await asyncio.to_thread(self._verify, run_id)

    def _verify(self, run_id: str) -> VerifyResult:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, type, body, hash FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        bodies = []
        for seq, kind, text, stored_hash in rows:
            try:
                body = _load(text)
            except ValueError as exc:
                return VerifyResult(False, seq - 1, seq, str(exc))
            # The columns are what SQL queries see; the body is what's hashed. They must agree.
            if body.get("seq") != seq or body.get("type") != kind:
                return VerifyResult(False, seq - 1, seq, "columns don't match the event body")
            bodies.append((body, stored_hash))
        return verify_bodies(bodies, run_id)


def _load(text: str) -> dict[str, Any]:
    # A body with a key twice hashes fine, but Python keeps the last value and SQLite's
    # json_extract the first, so two readers would see different events.
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"event body has {key!r} twice")
            out[key] = value
        return out

    loaded: dict[str, Any] = json.loads(text, object_pairs_hook=pairs)
    return loaded
