# Delegation invariants over random trees. One agent, "worker", may delegate to itself; a random
# script decides, turn by turn across the whole tree, whether the current task reads, delegates
# (with a random budget) or finishes. For every grant in the resulting tree:
#   - what it spent plus everything spent below it stays within its limits
#   - its capabilities, limits, expiry and depth are within its parent's
#   - each delegating call made at most one child
# and the same holds when the run is crashed at a random point and resumed.

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from legion.domain.action import EffectClass
from legion.domain.agent import AgentSpec, ModelRequirement
from legion.domain.budget import BudgetLimits, Dimension
from legion.domain.grant import DelegationLimits, Grant
from legion.domain.states import RunStatus
from legion.events.projections import RunState
from legion.events.types import Event, EventType
from legion.kernel import operator
from legion.models.scripted import call, reply
from legion.tools.base import ToolContext
from legion.tools.native import tool
from tests.support import OPERATOR, SimulatedCrash, agent, build, crash_at

E = EventType
COUNTED = (Dimension.STEPS, Dimension.MODEL_CALLS, Dimension.TOOL_CALLS)


class Line(BaseModel):
    line: str


def append_tool(runs: Counter[str]) -> Any:
    @tool(effect=EffectClass.WRITE, capabilities=["files.write"], resource=lambda a: "out/log")
    def append(args: Line, ctx: ToolContext) -> str:
        """Append a line."""
        runs[f"{ctx.task_id}/{ctx.call_id}"] += 1
        return "ok"

    return append


WORKER = AgentSpec(
    name="worker",
    description="does part of the job",
    instructions="Work.",
    model=ModelRequirement(profile="child/worker"),
    tools=("delegate", "read_file", "append"),
    capabilities=("agent.delegate:worker", "files.read:docs/**", "files.write:out/**"),
    delegation=DelegationLimits(max_depth=3, max_children=3),
)

budgets = st.fixed_dictionaries(
    {},
    optional={
        "tool_calls": st.integers(min_value=0, max_value=8),
        "model_calls": st.integers(min_value=0, max_value=8),
        "steps": st.integers(min_value=0, max_value=8),
    },
)
moves = st.one_of(
    st.just(("read", None)),
    st.just(("append", None)),
    st.just(("finish", None)),
    st.tuples(st.just("delegate"), budgets),
)


def script(plan: list[tuple[str, Any]]) -> list[Any]:
    steps: list[Any] = []
    for i, (kind, extra) in enumerate(plan):
        if kind == "read":
            steps.append(call("read_file", {"path": "docs/a.md"}, id=f"c{i}"))
        elif kind == "append":
            steps.append(call("append", {"line": f"l{i}"}, id=f"c{i}"))
        elif kind == "finish":
            steps.append(reply(f"finished {i}"))
        else:
            args: dict[str, Any] = {"agent": "worker", "objective": f"part {i}"}
            if extra:
                args["budget"] = extra
            steps.append(call("delegate", args, id=f"c{i}"))
    return [*steps, *[reply("out of script")] * 20]


def root_spec(limits: dict[str, int]) -> AgentSpec:
    return agent(
        tools=["delegate", "read_file", "append"],
        capabilities=["agent.delegate:worker", "files.read:**", "files.write:**"],
        delegation=DelegationLimits(max_depth=3, max_children=3),
        budget=BudgetLimits(**limits),
    )


def check_tree(events: list[Event]) -> None:
    grants: dict[str, Grant] = {}
    parent_of: dict[str, str] = {}
    creators: Counter[tuple[str | None, str]] = Counter()
    own: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for e in events:
        if e.type is E.RUN_CREATED:
            grants[e.payload["grant"]["id"]] = Grant.model_validate(e.payload["grant"])
        elif e.type is E.TASK_CREATED and e.payload.get("grant"):
            g = Grant.model_validate(e.payload["grant"])
            grants[g.id] = g
            assert g.parent_id is not None
            parent_of[g.id] = g.parent_id
            creators[(e.parent_task_id, e.payload["delegated_by"])] += 1
        elif e.type is E.BUDGET_CONSUMED:
            own[(e.payload["grant_id"], e.payload["dimension"])] = Decimal(e.payload["total"])
    assert all(n == 1 for n in creators.values()), creators

    def below(grant_id: str, dim: str) -> Decimal:
        kids = [g for g, p in parent_of.items() if p == grant_id]
        return own[(grant_id, dim)] + sum((below(k, dim) for k in kids), Decimal(0))

    for gid, g in grants.items():
        for dim in COUNTED:
            limit = g.budget.limit(dim)
            if limit is not None:
                assert below(gid, dim.value) <= limit, (gid, dim, below(gid, dim.value), limit)
        if gid in parent_of:
            parent = grants[parent_of[gid]]
            assert all(c.is_within(parent.capabilities) for c in g.capabilities)
            assert g.budget.is_within(parent.budget)
            assert g.depth == parent.depth + 1
            assert g.delegation.max_depth <= parent.delegation.max_depth - 1
            assert g.identity.on_behalf_of[:-1] == parent.identity.on_behalf_of


limit_sets = st.fixed_dictionaries(
    {
        "tool_calls": st.integers(min_value=1, max_value=12),
        "model_calls": st.integers(min_value=2, max_value=16),
        "steps": st.integers(min_value=2, max_value=16),
    }
)
GRANTABLE = ("files.read:**", "files.write:**", "agent.delegate:**")


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(plan=st.lists(moves, min_size=1, max_size=14), limits=limit_sets)
async def test_delegation_never_creates_budget_or_authority(
    plan: list[tuple[str, Any]], limits: dict[str, int]
) -> None:
    runs: Counter[str] = Counter()
    steps = script(plan)
    h = build(
        steps,
        agents={"worker": WORKER},
        scripts={"child/worker": steps},
        extra_tools=[append_tool(runs)],
        grantable=GRANTABLE,
        max_delegation_depth=3,
    )
    outcome = await h.run(root_spec(limits))
    events = await h.events(outcome.run_id)
    assert (await h.store.verify(outcome.run_id)).ok
    assert outcome.error_code != "internal_error", outcome.error_message
    check_tree(events)
    if outcome.status is RunStatus.COMPLETED:
        assert RunState.from_events(outcome.run_id, events).reservations == {}


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    plan=st.lists(moves, min_size=1, max_size=10),
    limits=limit_sets,
    point=st.sampled_from(
        [
            "before:task.created",
            "after:task.created",
            "after:task.waiting",
            "tool:after_invoke",
            "tool:before_invoke",
            "before:budget.settled",
            "after:budget.settled",
            "model:before_call",
        ]
    ),
    nth=st.integers(min_value=1, max_value=3),
)
async def test_crashes_inside_a_tree_never_duplicate_or_widen(
    plan: list[tuple[str, Any]], limits: dict[str, int], point: str, nth: int
) -> None:
    runs: Counter[str] = Counter()
    steps = script(plan)
    h = build(
        steps,
        agents={"worker": WORKER},
        scripts={"child/worker": steps},
        extra_tools=[append_tool(runs)],
        grantable=GRANTABLE,
        max_delegation_depth=3,
        faults=crash_at(point, nth),
    )
    try:
        outcome = await h.run(root_spec(limits))
        run_id = outcome.run_id
    except SimulatedCrash:
        summaries = await h.store.runs()
        if not summaries:
            # crashed before the run was recorded at all: nothing happened, nothing to resume
            assert not runs
            return
        [summary] = summaries
        run_id = summary.run_id
    # The restarted harness replays the same script from the start, which is what a model with
    # no memory of the crash would do; Legion has to cope with that without repeating writes.
    live = h.restart(extra_tools=[append_tool(runs)], by_turn=False)
    for _ in range(6):
        state = RunState.from_events(run_id, await live.events(run_id))
        if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            break
        if state.root_task_id is None or state.root_task_id not in state.tasks:
            break
        for view in state.tasks.values():
            for call_id in list(view.in_doubt):
                await operator.reconcile(
                    live.store, live.legion.locks, run_id, call_id, outcome="applied", by=OPERATOR
                )
        await live.resume(run_id)
    events = await live.events(run_id)
    assert (await live.store.verify(run_id)).ok
    assert all(n == 1 for n in runs.values()), runs
    check_tree(events)
