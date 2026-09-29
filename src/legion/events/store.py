from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from legion.canonical import GENESIS_HASH, chain_hash
from legion.domain.errors import LegionError
from legion.events.types import Event, EventDraft, EventType


class ConcurrentAppend(LegionError):
    code = "concurrent_append"


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checked: int
    bad_seq: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    agent: str
    created_at: datetime
    status: str
    events: int


class EventStore(Protocol):
    async def append(
        self, drafts: Sequence[EventDraft], *, expected_seq: int | None = None
    ) -> list[Event]: ...

    async def read(self, run_id: str, *, after_seq: int = 0) -> list[Event]: ...

    async def runs(self) -> list[RunSummary]: ...

    async def verify(self, run_id: str) -> VerifyResult: ...


def seal(draft: EventDraft, seq: int, prev_hash: str) -> Event:
    body = draft.model_dump(mode="json")
    body["seq"] = seq
    body["prev_hash"] = prev_hash
    return Event.model_validate({**body, "hash": chain_hash(prev_hash, body)})


def verify_bodies(bodies: Sequence[tuple[dict[str, Any], str]]) -> VerifyResult:
    """Check a run's chain from stored bodies and hashes, in sequence order."""
    prev = GENESIS_HASH
    for index, (body, stored_hash) in enumerate(bodies, start=1):
        if body.get("seq") != index:
            return VerifyResult(False, index - 1, index, "sequence gap or reorder")
        if body.get("prev_hash") != prev:
            return VerifyResult(False, index - 1, index, "prev_hash does not match")
        if chain_hash(prev, body) != stored_hash:
            return VerifyResult(False, index - 1, index, "hash does not match body")
        prev = stored_hash
    return VerifyResult(True, len(bodies))


def summarize(run_id: str, events: Sequence[Event]) -> RunSummary:
    created = events[0]
    agent = str(created.payload.get("agent", "?")) if created.type is EventType.RUN_CREATED else "?"
    status = "created"
    for event in events:
        if event.type.value.startswith("run."):
            status = event.type.value.removeprefix("run.")
    return RunSummary(run_id, agent, created.ts, status, len(events))


def _check_single_run(drafts: Sequence[EventDraft]) -> str:
    run_ids = {d.run_id for d in drafts}
    if len(run_ids) != 1:
        raise ValueError("one append must target exactly one run")
    return run_ids.pop()


class MemoryEventStore:
    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = {}
        self._lock = asyncio.Lock()

    async def append(
        self, drafts: Sequence[EventDraft], *, expected_seq: int | None = None
    ) -> list[Event]:
        if not drafts:
            return []
        run_id = _check_single_run(drafts)
        async with self._lock:
            log = self._events.setdefault(run_id, [])
            if expected_seq is not None and expected_seq != len(log):
                raise ConcurrentAppend(f"expected seq {expected_seq}, log is at {len(log)}")
            prev = log[-1].hash if log else GENESIS_HASH
            sealed = []
            for offset, draft in enumerate(drafts, start=1):
                event = seal(draft, len(log) + offset, prev)
                sealed.append(event)
                prev = event.hash
            log.extend(sealed)
            return sealed

    async def read(self, run_id: str, *, after_seq: int = 0) -> list[Event]:
        return [e for e in self._events.get(run_id, []) if e.seq > after_seq]

    async def runs(self) -> list[RunSummary]:
        return sorted(
            (summarize(run_id, log) for run_id, log in self._events.items() if log),
            key=lambda s: s.created_at,
        )

    async def verify(self, run_id: str) -> VerifyResult:
        return verify_bodies([(e.body(), e.hash) for e in self._events.get(run_id, [])])
