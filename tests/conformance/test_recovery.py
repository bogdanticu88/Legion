# Recovery invariants. A run is stopped at a random point, the process is "restarted", and resume
# is driven until the run ends or is waiting for a person. Across all of that:
#   - a write that may have happened is never run again automatically
#   - consumed budget never goes down, and the grant never changes
#   - nothing runs without an authorization the grant covers
#   - the chain stays valid, and a corrupted log is refused before anything runs

from collections import Counter
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from legion.domain.action import EffectClass
from legion.domain.capability import Capability
from legion.domain.errors import ResumeRefused
from legion.domain.grant import Grant
from legion.domain.states import RunStatus
from legion.events.projections import RunState
from legion.events.store import MemoryEventStore
from legion.events.types import Event, EventType
from legion.kernel import operator
from legion.kernel.approvals import binding_hash
from legion.models.scripted import calls, reply
from legion.tools.base import ToolContext
from legion.tools.native import tool
from tests.support import OPERATOR, SimulatedCrash, agent, build, crash_at

E = EventType

POINTS = [
    "model:before_call",
    "model:after_response",
    "after:model.responded",
    "tool:before_invoke",
    "tool:after_invoke",
    "before:tool.completed",
    "after:tool.completed",
    "after:action.proposed",
    "after:budget.consumed",
    "before:run.completed",
]


class Args(BaseModel):
    path: str


def tracked_tools(runs: Counter[str]) -> list[Any]:
    @tool(effect=EffectClass.WRITE, capabilities=["files.write"], resource=lambda a: a.path)
    def log_write(args: Args, ctx: ToolContext) -> str:
        """A write that counts how often each call really ran."""
        runs[ctx.call_id] += 1
        return "written"

    @tool(effect=EffectClass.READ, capabilities=["files.read"], resource=lambda a: a.path)
    def peek(args: Args, ctx: ToolContext) -> str:
        """A read."""
        return "data"

    return [log_write, peek]


turn = st.lists(
    st.tuples(
        st.sampled_from(["log_write", "peek"]),
        st.fixed_dictionaries({"path": st.sampled_from(["out/a", "out/b", "docs/a", "secret/x"])}),
    ),
    min_size=1,
    max_size=2,
)
SPEC = agent(tools=["log_write", "peek"], capabilities=["files.write:out/**", "files.read:docs/**"])


def totals(events: list[Event]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for e in events:
        if e.type is E.BUDGET_CONSUMED:
            out[e.payload["dimension"]] = Decimal(e.payload["total"])
    return out


@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    script=st.lists(turn, min_size=1, max_size=4),
    point=st.sampled_from(POINTS),
    nth=st.integers(min_value=1, max_value=3),
    applied=st.booleans(),
)
async def test_recovery_never_repeats_or_widens(
    script: list[list[tuple[str, dict[str, str]]]], point: str, nth: int, applied: bool
) -> None:
    runs: Counter[str] = Counter()
    steps = [calls(t, ids=[f"t{i}_{j}" for j in range(len(t))]) for i, t in enumerate(script)]
    steps.append(reply("done"))
    h = build(steps, extra_tools=tracked_tools(runs), faults=crash_at(point, nth), by_turn=True)
    try:
        outcome = await h.run(SPEC)
        run_id = outcome.run_id
    except SimulatedCrash:
        [summary] = await h.store.runs()
        run_id = summary.run_id

    before = await h.events(run_id)
    grant = RunState.from_events(run_id, before).grant
    live = h.restart(extra_tools=tracked_tools(runs))
    for _ in range(6):
        state = RunState.from_events(run_id, await live.events(run_id))
        if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            break
        root = state.tasks[state.root_task_id]  # type: ignore[index]
        for call_id in list(root.in_doubt):
            await operator.reconcile(
                live.store,
                live.legion.locks,
                run_id,
                call_id,
                outcome="applied" if applied else "not_applied",
                by=OPERATOR,
            )
        await live.resume(run_id)

    after = await live.events(run_id)
    assert (await live.store.verify(run_id)).ok
    assert all(n == 1 for n in runs.values()), runs
    for dimension, total in totals(before).items():
        assert totals(after)[dimension] >= total
    state = RunState.from_events(run_id, after)
    assert state.grant == grant

    authorized = set()
    parsed = Grant.model_validate(grant)
    proposed: dict[str, list[str]] = {}
    for e in after:
        if e.type is E.ACTION_PROPOSED:
            proposed[e.payload["action_hash"]] = e.payload["required"]
        elif e.type is E.ACTION_AUTHORIZED:
            required = proposed[e.payload["action_hash"]]
            assert all(parsed.covers(Capability.parse(c)) for c in required)
            authorized.add(e.payload["action_hash"])
        elif e.type is E.TOOL_STARTED:
            assert e.payload["action_hash"] in authorized


class Mangled(MemoryEventStore):
    # a store whose history was edited behind Legion's back
    def __init__(self, events: list[Event], index: int, key: str) -> None:
        super().__init__()
        body = events[index].body()
        value = body[key]
        if isinstance(value, dict):
            body[key] = {**value, "forged": True}
        elif isinstance(value, int):
            body[key] = value + 1
        else:
            body[key] = "forged"
        forged = Event.model_validate({**body, "hash": events[index].hash})
        self._events = {events[0].run_id: [*events[:index], forged, *events[index + 1 :]]}


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
async def test_corrupted_history_is_refused(data: st.DataObject) -> None:
    runs: Counter[str] = Counter()
    steps = [calls([("log_write", {"path": "out/a"})], ids=["w1"]), reply("done")]
    h = build(
        steps,
        extra_tools=tracked_tools(runs),
        faults=crash_at("model:before_call", 2),
        by_turn=True,
    )
    with pytest.raises(SimulatedCrash):
        await h.run(SPEC)
    [summary] = await h.store.runs()
    events = await h.store.read(summary.run_id)
    index = data.draw(st.integers(min_value=0, max_value=len(events) - 1))
    key = data.draw(st.sampled_from(["payload", "type", "task_id", "seq", "prev_hash"]))
    if key == "type":
        return
    before = sum(runs.values())
    forged = h.restart(store=Mangled(events, index, key), extra_tools=tracked_tools(runs))
    with pytest.raises(ResumeRefused):
        await forged.resume(summary.run_id)
    assert sum(runs.values()) == before


arg_values = st.one_of(st.integers(), st.text(max_size=10), st.booleans(), st.none())


@given(
    base=st.dictionaries(st.text(min_size=1, max_size=5), arg_values, min_size=1, max_size=4),
    data=st.data(),
)
def test_any_argument_change_changes_the_binding(base: dict[str, Any], data: st.DataObject) -> None:
    key = data.draw(st.sampled_from(sorted(base)))
    changed = data.draw(
        arg_values.filter(lambda v: v != base[key] or type(v) is not type(base[key]))
    )
    other = {**base, key: changed}
    assert binding_hash({"arguments": base}) != binding_hash({"arguments": other})
