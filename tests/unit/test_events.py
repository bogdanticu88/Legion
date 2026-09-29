import sqlite3
from pathlib import Path

import pytest

from legion.canonical import GENESIS_HASH, canonical_json, digest
from legion.domain.errors import InvalidTransition
from legion.events.projections import RunState
from legion.events.sqlite_store import SqliteEventStore
from legion.events.store import ConcurrentAppend, EventStore, MemoryEventStore
from legion.events.types import Cancelled, Empty, EventType, RunCompleted, draft


def drafts(run_id: str, n: int) -> list:
    return [draft(run_id, EventType.RUN_CANCELLED, Cancelled(reason=f"r{i}")) for i in range(n)]


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> EventStore:
    if request.param == "memory":
        return MemoryEventStore()
    return SqliteEventStore(tmp_path / "events.db")


async def test_append_assigns_sequence_and_chain(store: EventStore) -> None:
    first = await store.append(drafts("r1", 2))
    second = await store.append(drafts("r1", 1))
    events = [*first, *second]
    assert [e.seq for e in events] == [1, 2, 3]
    assert events[0].prev_hash == GENESIS_HASH
    assert events[1].prev_hash == events[0].hash
    assert (await store.verify("r1")).ok
    assert [e.seq for e in await store.read("r1", after_seq=1)] == [2, 3]


async def test_runs_are_independent_chains(store: EventStore) -> None:
    await store.append(drafts("r1", 2))
    other = await store.append(drafts("r2", 1))
    assert other[0].seq == 1 and other[0].prev_hash == GENESIS_HASH


async def test_expected_seq_rejects_concurrent_writer(store: EventStore) -> None:
    await store.append(drafts("r1", 1), expected_seq=0)
    with pytest.raises(ConcurrentAppend):
        await store.append(drafts("r1", 1), expected_seq=0)


async def test_single_run_per_append(store: EventStore) -> None:
    with pytest.raises(ValueError):
        await store.append([*drafts("r1", 1), *drafts("r2", 1)])


async def test_read_back_equals_written(store: EventStore) -> None:
    written = await store.append(drafts("r1", 3))
    assert await store.read("r1") == written


def test_draft_checks_payload_type() -> None:
    with pytest.raises(TypeError):
        draft("r", EventType.RUN_COMPLETED, Empty())
    draft("r", EventType.RUN_COMPLETED, RunCompleted(output="x"))


async def test_sqlite_is_append_only(tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "e.db")
    await store.append(drafts("r1", 2))
    conn = sqlite3.connect(tmp_path / "e.db")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET body = '{}' WHERE seq = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events")


@pytest.mark.parametrize("change", ["body", "hash", "delete"])
async def test_sqlite_tampering_is_detected(tmp_path: Path, change: str) -> None:
    store = SqliteEventStore(tmp_path / "e.db")
    await store.append(drafts("r1", 3))
    store.close()
    conn = sqlite3.connect(tmp_path / "e.db", isolation_level=None)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("DROP TRIGGER events_no_delete")
    if change == "body":
        conn.execute("UPDATE events SET body = replace(body, '\"r1\"', '\"rX\"') WHERE seq = 2")
        conn.execute(
            "UPDATE events SET body = replace(body, 'reason\":\"r1', 'reason\":\"forged') "
            "WHERE seq = 2"
        )
    elif change == "hash":
        conn.execute("UPDATE events SET hash = ? WHERE seq = 2", ("0" * 64,))
    else:
        conn.execute("DELETE FROM events WHERE seq = 2")
    conn.close()
    result = await SqliteEventStore(tmp_path / "e.db").verify("r1")
    assert not result.ok
    assert result.bad_seq in (2, 3)


@pytest.mark.parametrize(
    ("sql", "run_id"),
    [
        # rows relabelled as another run: every body still chains, but names r2
        ("UPDATE events SET run_id = 'r3' WHERE run_id = 'r2'", "r3"),
        # SQL sees seq 999 or a different type; the hashed body doesn't
        ("UPDATE events SET seq = 999 WHERE run_id = 'r1' AND seq = 3", "r1"),
        ("UPDATE events SET type = 'approval.granted' WHERE run_id = 'r1' AND seq = 2", "r1"),
        # a key twice: Python reads the last copy, SQLite's json_extract the first
        (
            'UPDATE events SET body = \'{"type":"approval.granted",\' || substr(body, 2) '
            "WHERE run_id = 'r1' AND seq = 2",
            "r1",
        ),
    ],
    ids=["relabelled", "seq-column", "type-column", "duplicate-key"],
)
async def test_column_and_body_tampering_is_detected(tmp_path: Path, sql: str, run_id: str) -> None:
    store = SqliteEventStore(tmp_path / "e.db")
    await store.append(drafts("r1", 3))
    await store.append(drafts("r2", 3))
    store.close()
    conn = sqlite3.connect(tmp_path / "e.db", isolation_level=None)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute(sql)
    conn.close()
    assert not (await SqliteEventStore(tmp_path / "e.db").verify(run_id)).ok


def test_canonical_json_is_order_independent() -> None:
    assert canonical_json({"b": 1, "a": [1, {"d": 2, "c": 3}]}) == canonical_json(
        {"a": [1, {"c": 3, "d": 2}], "b": 1}
    )
    assert digest({"x": 1}) != digest({"x": 2})


async def test_projection_rejects_bad_order_and_transitions() -> None:
    store = MemoryEventStore()
    events = await store.append(
        [
            draft("r1", EventType.RUN_STARTED, Empty()),
            draft("r1", EventType.RUN_COMPLETED, RunCompleted(output="done")),
            draft("r1", EventType.RUN_STARTED, Empty()),
        ]
    )
    state = RunState("r1")
    with pytest.raises(ValueError):
        state.apply(events[1])
    state.apply(events[0])
    state.apply(events[1])
    with pytest.raises(InvalidTransition):
        state.apply(events[2])
