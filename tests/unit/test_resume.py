import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from legion.domain.action import EffectClass
from legion.domain.budget import BudgetLimits
from legion.domain.errors import ResumeRefused, RunLocked
from legion.domain.states import RunStatus
from legion.events.sqlite_store import SqliteEventStore
from legion.events.types import EventType
from legion.kernel import operator
from legion.kernel.locks import FileRunLocks
from legion.models.scripted import call, reply
from legion.tools.base import ToolContext
from legion.tools.native import tool
from tests.support import OPERATOR, SimulatedCrash, agent, build, crash_at

E = EventType

READ_THEN_WRITE = [
    call("read_file", {"path": "docs/a.md"}, id="r1"),
    call("write_file", {"path": "out/x", "text": "hello"}, id="w1"),
    reply("done"),
]


async def crashed(steps: list[Any], point: str, nth: int = 1, **kw: Any) -> tuple[Any, str]:
    h = build(steps, faults=crash_at(point, nth), by_turn=True, **kw)
    with pytest.raises(SimulatedCrash):
        await h.run()
    [summary] = await h.store.runs()
    assert summary.status == "running"
    return h.restart(), summary.run_id


async def test_crash_before_model_call() -> None:
    h, run_id = await crashed(READ_THEN_WRITE, "model:before_call", nth=2)
    outcome = await h.resume(run_id)
    assert outcome.status is RunStatus.COMPLETED
    assert h.files.writes == ["out/x"]
    assert (await h.store.verify(run_id)).ok


async def test_response_lost_before_it_was_recorded() -> None:
    h, run_id = await crashed(READ_THEN_WRITE, "model:after_response", nth=2)
    outcome = await h.resume(run_id)
    assert outcome.status is RunStatus.COMPLETED
    assert h.files.writes == ["out/x"]
    # the lost call still counts
    calls = [
        c for c in await h.payloads(run_id, E.BUDGET_CONSUMED) if c["dimension"] == "model_calls"
    ]
    assert calls[-1]["total"] == "4"


async def test_crash_after_response_before_tool() -> None:
    h, run_id = await crashed(READ_THEN_WRITE, "after:model.responded", nth=2)
    outcome = await h.resume(run_id)
    assert outcome.status is RunStatus.COMPLETED
    assert h.files.writes == ["out/x"]


async def test_read_interrupted_mid_run_is_retried() -> None:
    h, run_id = await crashed(READ_THEN_WRITE, "tool:after_invoke", nth=1)
    outcome = await h.resume(run_id)
    assert outcome.status is RunStatus.COMPLETED
    types = await h.types(run_id)
    assert E.ACTION_INTERRUPTED in types
    assert E.ACTION_IN_DOUBT not in types


async def test_write_interrupted_before_effect_is_not_retried() -> None:
    h, run_id = await crashed(READ_THEN_WRITE, "tool:before_invoke", nth=2)
    outcome = await h.resume(run_id)
    assert outcome.status is RunStatus.PAUSED
    assert outcome.blocked_call == "w1"
    assert h.files.writes == []

    # Resuming again without a decision changes nothing.
    again = await h.resume(run_id)
    assert again.status is RunStatus.PAUSED
    assert h.files.writes == []

    await operator.reconcile(
        h.store, h.legion.locks, run_id, "w1", outcome="not_applied", by=OPERATOR
    )
    done = await h.resume(run_id)
    assert done.status is RunStatus.COMPLETED
    # The operator said it didn't happen; the model was told, and Legion still didn't retry it.
    assert h.files.writes == []


async def test_write_that_happened_before_the_crash_runs_once() -> None:
    h, run_id = await crashed(READ_THEN_WRITE, "tool:after_invoke", nth=2)
    assert h.files.writes == ["out/x"]
    outcome = await h.resume(run_id)
    assert outcome.status is RunStatus.PAUSED
    await operator.reconcile(h.store, h.legion.locks, run_id, "w1", outcome="applied", by=OPERATOR)
    done = await h.resume(run_id)
    assert done.status is RunStatus.COMPLETED
    assert h.files.writes == ["out/x"]
    [reconciled] = await h.payloads(run_id, E.ACTION_RECONCILED)
    assert reconciled["by"] == str(OPERATOR)


async def test_crash_while_recording_the_result() -> None:
    h, run_id = await crashed(READ_THEN_WRITE, "before:tool.completed", nth=2)
    outcome = await h.resume(run_id)
    assert outcome.status is RunStatus.PAUSED
    assert h.files.writes == ["out/x"]


class Empty(BaseModel):
    pass


def put_tool(log: list[str]) -> Any:
    @tool(
        effect=EffectClass.WRITE_IDEMPOTENT,
        capabilities=["files.write"],
        resource=lambda a: "out/p",
    )
    async def put(args: Empty, ctx: ToolContext) -> str:
        """Sets a value. Safe to repeat."""
        log.append(ctx.idempotency_key)
        return "set"

    return put


async def test_idempotent_write_is_retried_with_the_same_key() -> None:
    keys: list[str] = []
    steps = [call("put", {}, id="p1"), reply("done")]
    spec = agent(tools=["put"], capabilities=["files.write:out/**"])
    h = build(
        steps, faults=crash_at("tool:after_invoke"), by_turn=True, extra_tools=[put_tool(keys)]
    )
    with pytest.raises(SimulatedCrash):
        await h.run(spec)
    [summary] = await h.store.runs()
    outcome = await h.restart(extra_tools=[put_tool(keys)]).resume(summary.run_id)
    assert outcome.status is RunStatus.COMPLETED
    assert len(keys) == 2 and keys[0] == keys[1]


async def test_crash_after_task_completed_just_closes_the_run() -> None:
    h, run_id = await crashed(READ_THEN_WRITE, "after:task.completed")
    before = len(h.provider.requests)
    outcome = await h.resume(run_id)
    assert outcome.status is RunStatus.COMPLETED
    assert len(h.provider.requests) == before


async def test_step_budget_is_not_reset_by_resume() -> None:
    steps = [
        call("read_file", {"path": "docs/a.md"}, id="r1"),
        call("write_file", {"path": "out/1", "text": "a"}, id="w1"),
        call("write_file", {"path": "out/2", "text": "b"}, id="w2"),
        reply("done"),
    ]
    spec = agent(budget=BudgetLimits(steps=3))
    h = build(steps, faults=crash_at("model:before_call", 3), by_turn=True)
    with pytest.raises(SimulatedCrash):
        await h.run(spec)
    [summary] = await h.store.runs()
    outcome = await h.restart().resume(summary.run_id)
    # three steps were already charged; the fourth one is refused
    assert outcome.error_code == "budget_exceeded"
    assert h.files.writes == ["out/1"]


async def test_wall_clock_of_crashed_stretch_is_charged() -> None:
    h, run_id = await crashed(READ_THEN_WRITE, "model:before_call", nth=2)
    await h.resume(run_id)
    wall = [
        c for c in await h.payloads(run_id, E.BUDGET_CONSUMED) if c["dimension"] == "wall_seconds"
    ]
    assert len(wall) == 2  # the crashed stretch, charged at resume, and the resumed one


async def test_repeat_count_does_not_grow_on_resume() -> None:
    # Two identical reads; the second is interrupted and proposed again on resume. Counting that
    # as a third attempt would trip the repeat refusal.
    steps = [call("read_file", {"path": "docs/a.md"}, id=f"c{i}") for i in range(2)] + [reply("x")]
    h, run_id = await crashed(steps, "tool:after_invoke", nth=2)
    outcome = await h.resume(run_id)
    assert outcome.status is RunStatus.COMPLETED
    assert await h.payloads(run_id, E.ACTION_REFUSED) == []


async def test_resume_refuses_terminal_runs() -> None:
    h = build([reply("done")])
    outcome = await h.run()
    with pytest.raises(ResumeRefused, match="completed"):
        await h.resume(outcome.run_id)


async def test_resume_refuses_unknown_run() -> None:
    h = build([reply("done")])
    with pytest.raises(ResumeRefused, match="no run"):
        await h.resume("run_nope")


async def test_resume_rechecks_grantable_capabilities() -> None:
    h, run_id = await crashed(READ_THEN_WRITE, "model:before_call", nth=2)
    narrower = h.restart(grantable=("files.read:**",))
    with pytest.raises(ResumeRefused, match="no longer grants"):
        await narrower.resume(run_id)
    assert h.files.writes == []


async def test_tampered_log_is_refused_before_anything_runs(tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "e.db")
    h = build(READ_THEN_WRITE, faults=crash_at("model:before_call", 2), by_turn=True, store=store)
    with pytest.raises(SimulatedCrash):
        await h.run()
    [summary] = await store.runs()
    store.close()
    conn = sqlite3.connect(tmp_path / "e.db", isolation_level=None)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET body = replace(body, 'docs/**', '**') WHERE seq = 1")
    conn.close()
    h2 = h.restart(store=SqliteEventStore(tmp_path / "e.db"))
    with pytest.raises(ResumeRefused, match="verification"):
        await h2.resume(summary.run_id)
    assert h.files.writes == []


async def test_file_locks_keep_one_holder(tmp_path: Path) -> None:
    locks = FileRunLocks(tmp_path)
    with locks.hold("run_a"):
        with pytest.raises(RunLocked), locks.hold("run_a"):
            pass
        with locks.hold("run_b"):
            pass
    with locks.hold("run_a"):
        pass


def test_file_locks_reject_odd_run_ids(tmp_path: Path) -> None:
    with pytest.raises(RunLocked), FileRunLocks(tmp_path).hold("../escape"):
        pass
