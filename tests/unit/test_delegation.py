import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from legion.authority.policy import Rule, Verdict, default_rules
from legion.domain.action import EffectClass
from legion.domain.agent import AgentSpec, ModelRequirement
from legion.domain.budget import BudgetLimits
from legion.domain.grant import DelegationLimits, Grant
from legion.domain.states import RunStatus, TaskStatus
from legion.events.projections import RunState
from legion.events.types import EventType
from legion.kernel import operator
from legion.models.scripted import call, reply
from legion.ports.identity import AgentIdentity, KillState, NullIdentityPort
from legion.tools.base import ToolContext
from legion.tools.native import tool
from tests.support import OPERATOR, SimulatedCrash, agent, build, crash_at

E = EventType
GRANTABLE = ("files.read:**", "files.write:**", "agent.delegate:**", "hosts.isolate:**")


def child(
    name: str = "reader",
    tools: tuple[str, ...] = ("read_file",),
    caps: tuple[str, ...] = ("files.read:docs/**",),
    delegation: DelegationLimits | None = None,
    budget: BudgetLimits | None = None,
) -> AgentSpec:
    return AgentSpec(
        name=name,
        description=f"the {name}",
        instructions="Do the sub-task.",
        model=ModelRequirement(profile=f"child/{name}"),
        tools=tools,
        capabilities=caps,
        delegation=delegation or DelegationLimits(),
        budget=budget or BudgetLimits(),
    )


def boss(
    caps: tuple[str, ...] = ("files.read:**", "agent.delegate:**"),
    tools: tuple[str, ...] = ("delegate", "read_file"),
    delegation: DelegationLimits | None = None,
    budget: BudgetLimits | None = None,
) -> AgentSpec:
    return agent(
        tools=list(tools),
        capabilities=list(caps),
        delegation=delegation or DelegationLimits(max_depth=1, max_children=2),
        budget=budget or BudgetLimits(),
    )


def delegate(agent_name: str = "reader", call_id: str = "d1", **extra: Any) -> Any:
    return call(
        "delegate", {"agent": agent_name, "objective": "read docs/a.md", **extra}, id=call_id
    )


READER_SCRIPT = [call("read_file", {"path": "docs/a.md"}, id="r1"), reply("it says alpha")]


def setup(parent_steps: list[Any], scripts: dict[str, list[Any]] | None = None, **kw: Any) -> Any:
    agents = kw.pop("agents", {"reader": child()})
    scripts = scripts if scripts is not None else {"child/reader": READER_SCRIPT}
    return build(parent_steps, agents=agents, scripts=scripts, grantable=GRANTABLE, **kw)


async def refusal(h: Any, run_id: str) -> str:
    [refused] = [p for p in await h.payloads(run_id, E.ACTION_REFUSED) if p["tool"] == "delegate"]
    return str(refused["message"])


async def children(h: Any, run_id: str) -> list[dict[str, Any]]:
    return [
        e.payload
        for e in await h.events(run_id)
        if e.type is E.TASK_CREATED and e.parent_task_id is not None
    ]


# the basic path


async def test_child_runs_and_returns_a_structured_result() -> None:
    h = setup([delegate(), reply("boss done")])
    outcome = await h.run(boss())
    assert outcome.status is RunStatus.COMPLETED
    state = RunState.from_events(outcome.run_id, await h.events(outcome.run_id))
    root = state.tasks[state.root_task_id]  # type: ignore[index]
    [(call_id, child_id)] = root.children.items()
    kid = state.tasks[child_id]
    assert call_id == "d1" and kid.parent_id == root.id and kid.status is TaskStatus.COMPLETED

    # The parent gets a summary, not the child's conversation.
    result = json.loads(root.transcript[-2].parts[0].content)  # type: ignore[union-attr]
    assert result["status"] == "completed" and result["output"] == "it says alpha"
    assert all("read_file" not in m.text for m in root.transcript)


async def test_child_identity_is_derived_and_traceable() -> None:
    h = setup([delegate(), reply("done")])
    outcome = await h.run(boss())
    [created] = await children(h, outcome.run_id)
    grant = Grant.model_validate(created["grant"])
    assert grant.identity.agent_ref == "reader"
    assert grant.identity.on_behalf_of == ("human:tester", "tester")
    assert grant.issuer.startswith("delegation:") and grant.depth == 1
    events = await h.events(outcome.run_id)
    child_events = [e for e in events if e.task_id == created["task"]["id"]]
    assert {e.agent_id for e in child_events} == {"reader"}


async def test_child_gets_only_what_it_is_given() -> None:
    h = setup([delegate(context={"hint": "look at a.md"}), reply("done")])
    await h.run(boss())
    child_requests = h.legion.resolver.providers["child-reader"].requests
    first = child_requests[0].messages[0].text
    assert "read docs/a.md" in first and "look at a.md" in first
    assert "Do the task." not in child_requests[0].system


# attenuation


async def test_child_cannot_get_a_capability_the_parent_lacks() -> None:
    wide = child(
        tools=("read_file", "write_file"), caps=("files.read:docs/**", "files.write:out/**")
    )
    h = setup([delegate(), reply("done")], agents={"reader": wide})
    outcome = await h.run(boss())
    assert "files.write:out/**" in await refusal(h, outcome.run_id)
    assert await children(h, outcome.run_id) == []


async def test_child_cannot_widen_resource_scope() -> None:
    h = setup([delegate(capabilities=["files.read:**"]), reply("done")])
    outcome = await h.run(boss(caps=("files.read:docs/**", "agent.delegate:**")))
    assert "files.read:**" in await refusal(h, outcome.run_id)


async def test_child_cannot_ask_for_more_than_its_spec_uses() -> None:
    h = setup([delegate(capabilities=["files.read:secret/**"]), reply("done")])
    outcome = await h.run(boss())
    assert "not defined to use" in await refusal(h, outcome.run_id)


async def test_narrower_request_is_honoured() -> None:
    h = setup([delegate(capabilities=["files.read:docs/a.md"]), reply("done")])
    outcome = await h.run(boss())
    [created] = await children(h, outcome.run_id)
    assert created["grant"]["capabilities"] == ["files.read:docs/a.md"]


async def test_delegation_needs_the_capability_for_that_agent() -> None:
    h = setup([delegate(), reply("done")])
    outcome = await h.run(boss(caps=("files.read:**", "agent.delegate:somebody-else")))
    [refused] = await h.payloads(outcome.run_id, E.ACTION_REFUSED)
    assert refused["reason_code"] == "capability_denied"


@pytest.mark.parametrize("field", ["identity", "grant", "depth", "on_behalf_of"])
async def test_identity_and_grant_cannot_be_passed_in(field: str) -> None:
    h = setup([delegate(**{field: "boss"}), reply("done")])
    outcome = await h.run(boss())
    [refused] = await h.payloads(outcome.run_id, E.ACTION_REFUSED)
    assert refused["reason_code"] == "invalid_arguments"


def test_grants_are_immutable() -> None:
    grant = Grant.model_validate(
        {
            "id": "g",
            "capabilities": ["a.b"],
            "budget": {},
            "identity": {"principal": {"kind": "human", "id": "x"}, "agent_ref": "a"},
            "issuer": "t",
        }
    )
    with pytest.raises(ValidationError):
        grant.capabilities = frozenset()  # type: ignore[misc]


# budget


async def test_child_budget_comes_out_of_the_parent() -> None:
    h = setup([delegate(budget={"tool_calls": 2}), reply("done")])
    outcome = await h.run(boss(budget=BudgetLimits(tool_calls=5)))
    events = await h.events(outcome.run_id)
    reserved = {
        e.payload["dimension"]: Decimal(e.payload["amount"])
        for e in events
        if e.type is E.BUDGET_RESERVED
    }
    settled = {e.payload["dimension"]: e.payload for e in events if e.type is E.BUDGET_SETTLED}
    assert reserved["tool_calls"] == 2
    assert Decimal(settled["tool_calls"]["used"]) == 1


async def test_asking_for_more_than_is_left_is_refused() -> None:
    h = setup([delegate(budget={"tool_calls": 10}), reply("done")])
    outcome = await h.run(boss(budget=BudgetLimits(tool_calls=5)))
    assert "only 4 is left" in await refusal(h, outcome.run_id)


async def test_children_cannot_multiply_the_budget() -> None:
    # Parent has 6 tool calls. Each delegation uses one; each child reserves what's left.
    steps = [delegate(call_id="d1"), delegate(call_id="d2"), delegate(call_id="d3"), reply("done")]
    loop_reader = [call("read_file", {"path": "docs/a.md"}, id=f"r{i}") for i in range(8)]
    h = setup(
        steps,
        scripts={"child/reader": loop_reader},
        agents={"reader": child(budget=BudgetLimits(tool_calls=None))},
    )
    outcome = await h.run(
        boss(
            budget=BudgetLimits(tool_calls=6),
            delegation=DelegationLimits(max_depth=1, max_children=3),
        )
    )
    events = await h.events(outcome.run_id)
    tool_calls = sum(
        Decimal(e.payload["amount"])
        for e in events
        if e.type is E.BUDGET_CONSUMED and e.payload["dimension"] == "tool_calls"
    )
    assert tool_calls <= 6


async def test_child_that_runs_out_of_budget_fails_and_parent_carries_on() -> None:
    spinning = [call("read_file", {"path": "docs/a.md"}, id=f"r{i}") for i in range(3)]
    h = setup(
        [delegate(budget={"steps": 2}), reply("carried on")],
        scripts={"child/reader": spinning},
    )
    outcome = await h.run(boss())
    assert outcome.status is RunStatus.COMPLETED and outcome.output == "carried on"
    result = json.loads((await h.payloads(outcome.run_id, E.TOOL_COMPLETED))[-1]["content"])
    assert result["status"] == "failed" and result["error_code"] == "budget_exceeded"


# limits on delegation


async def test_depth_limit_stops_grandchildren() -> None:
    mid = child(
        name="mid",
        tools=("delegate",),
        caps=("agent.delegate:**",),
        delegation=DelegationLimits(max_depth=1, max_children=1),
    )
    leaf = child(name="leaf")
    scripts = {
        "child/mid": [delegate("leaf", call_id="m1"), reply("mid done")],
        "child/leaf": [delegate("leaf", call_id="l1"), reply("leaf done")],
    }
    leaf = child(
        name="leaf",
        tools=("delegate",),
        caps=("agent.delegate:**",),
        delegation=DelegationLimits(max_depth=1, max_children=1),
    )
    h = setup(
        [delegate("mid"), reply("boss done")],
        scripts=scripts,
        agents={"mid": mid, "leaf": leaf},
    )
    outcome = await h.run(
        boss(
            caps=("agent.delegate:**",),
            tools=("delegate",),
            delegation=DelegationLimits(max_depth=2, max_children=1),
        )
    )
    assert outcome.status is RunStatus.COMPLETED
    created = await children(h, outcome.run_id)
    assert [c["agent"] for c in created] == ["mid", "leaf"]
    assert [Grant.model_validate(c["grant"]).depth for c in created] == [1, 2]
    refused = [p for p in await h.payloads(outcome.run_id, E.ACTION_REFUSED)]
    assert any("not allowed to delegate" in p["message"] for p in refused)


async def test_fan_out_limit() -> None:
    steps = [delegate(call_id=f"d{i}", objective=f"part {i}") for i in range(3)] + [reply("done")]
    reads = [call("read_file", {"path": "docs/a.md"}, id="r"), reply("ok")] * 3
    h = setup(steps, scripts={"child/reader": reads})
    outcome = await h.run(boss(delegation=DelegationLimits(max_depth=1, max_children=2)))
    assert len(await children(h, outcome.run_id)) == 2
    assert "already has 2 children" in await refusal(h, outcome.run_id)


async def test_run_wide_task_limit() -> None:
    steps = [delegate(call_id=f"d{i}", objective=f"part {i}") for i in range(3)] + [reply("done")]
    reads = [call("read_file", {"path": "docs/a.md"}, id="r"), reply("ok")] * 3
    h = setup(steps, scripts={"child/reader": reads}, max_tasks=2)
    outcome = await h.run(boss(delegation=DelegationLimits(max_depth=1, max_children=5)))
    assert len(await children(h, outcome.run_id)) == 1


async def test_agent_that_may_not_delegate() -> None:
    h = setup([delegate(), reply("done")])
    with pytest.raises(Exception, match="max_depth is 0"):
        await h.run(boss(delegation=DelegationLimits()))


async def test_unknown_agent() -> None:
    h = setup([delegate("ghost"), reply("done")])
    outcome = await h.run(boss())
    assert "no agent named 'ghost'" in await refusal(h, outcome.run_id)


# approvals


class Host(BaseModel):
    host: str


def isolation_tool(log: list[str]) -> Any:
    @tool(
        effect=EffectClass.EXTERNAL_IRREVERSIBLE,
        capabilities=["hosts.isolate"],
        resource=lambda a: a.host,
    )
    def isolate(args: Host, ctx: ToolContext) -> str:
        """Cut a host off the network."""
        log.append(f"{ctx.task_id}:{args.host}")
        return "isolated"

    return isolate


ISOLATOR = child(name="isolator", tools=("isolate",), caps=("hosts.isolate:**",))


async def test_child_approval_pauses_the_whole_run_and_resumes() -> None:
    log: list[str] = []
    h = setup(
        [delegate("isolator", objective="isolate HOST-A"), reply("boss done")],
        scripts={
            "child/isolator": [call("isolate", {"host": "HOST-A"}, id="i1"), reply("isolated it")]
        },
        agents={"isolator": ISOLATOR},
        extra_tools=[isolation_tool(log)],
        by_turn=True,
    )
    spec = boss(caps=("hosts.isolate:**", "agent.delegate:**"), tools=("delegate",))
    outcome = await h.run(spec)
    assert outcome.status is RunStatus.PAUSED and log == []
    _, approval = await operator.find(h.store, outcome.approval_id)
    [created] = await children(h, outcome.run_id)
    assert approval.task_id == created["task"]["id"] and approval.subject["agent"] == "isolator"

    await operator.decide(h.store, h.legion.locks, outcome.approval_id, approve=True, by=OPERATOR)
    done = await h.restart(extra_tools=[isolation_tool(log)]).resume(outcome.run_id)
    assert done.status is RunStatus.COMPLETED
    assert len(log) == 1 and log[0].endswith("HOST-A")
    assert len(await children(h, outcome.run_id)) == 1


async def test_sibling_cannot_use_its_siblings_approval() -> None:
    log: list[str] = []
    same = call("isolate", {"host": "HOST-A"}, id="i1")
    h = setup(
        [
            delegate("isolator", call_id="d1", objective="isolate HOST-A"),
            delegate("isolator", call_id="d2", objective="isolate HOST-A"),
            reply("boss done"),
        ],
        scripts={"child/isolator": [same, reply("isolated")]},
        agents={"isolator": ISOLATOR},
        extra_tools=[isolation_tool(log)],
        by_turn=True,
    )
    spec = boss(caps=("hosts.isolate:**", "agent.delegate:**"), tools=("delegate",))
    first = await h.run(spec)
    await operator.decide(h.store, h.legion.locks, first.approval_id, approve=True, by=OPERATOR)
    second = await h.restart(extra_tools=[isolation_tool(log)]).resume(first.run_id)
    # the second child's identical action needs its own approval
    assert second.status is RunStatus.PAUSED and second.approval_id != first.approval_id
    assert len(log) == 1


async def test_policy_can_require_approval_to_delegate() -> None:
    rules = [*default_rules(), Rule(decision=Verdict.REQUIRE_APPROVAL, tool="delegate")]
    h = setup([delegate(), reply("done")], rules=rules, by_turn=True)
    outcome = await h.run(boss())
    assert outcome.status is RunStatus.PAUSED
    assert await children(h, outcome.run_id) == []
    _, approval = await operator.find(h.store, outcome.approval_id)
    assert approval.subject["tool"] == "delegate" and approval.subject["resource"] == "reader"
    await operator.decide(h.store, h.legion.locks, outcome.approval_id, approve=True, by=OPERATOR)
    done = await h.restart().resume(outcome.run_id)
    assert done.status is RunStatus.COMPLETED
    assert len(await children(h, outcome.run_id)) == 1


# in doubt, crashes, cancellation


class Line(BaseModel):
    line: str


def append_tool(log: list[str], hang: bool = False) -> Any:
    @tool(
        effect=EffectClass.WRITE,
        capabilities=["files.write"],
        resource=lambda a: "out/log",
        timeout_s=60,
    )
    async def append(args: Line, ctx: ToolContext) -> str:
        """Append a line."""
        log.append(args.line)
        if hang:
            await asyncio.sleep(60)
        return "ok"

    return append


WRITER = child(name="writer", tools=("append",), caps=("files.write:out/**",))
WRITER_SCRIPT = [call("append", {"line": "hello"}, id="w1"), reply("wrote it")]


def writer_setup(log: list[str], **kw: Any) -> Any:
    return setup(
        [delegate("writer", objective="append hello"), reply("boss done")],
        scripts={"child/writer": WRITER_SCRIPT},
        agents={"writer": WRITER},
        extra_tools=[append_tool(log, kw.pop("hang", False))],
        by_turn=True,
        **kw,
    )


WRITER_BOSS = boss(caps=("files.write:**", "agent.delegate:**"), tools=("delegate",))


@pytest.mark.parametrize(
    ("point", "nth"),
    [
        ("before:task.created", 2),
        ("after:task.created", 2),
        ("after:task.waiting", 1),
        ("after:model.responded", 2),
        ("before:budget.settled", 1),
        ("after:task.resumed", 1),
    ],
)
async def test_crash_around_delegation_never_duplicates_the_child(point: str, nth: int) -> None:
    log: list[str] = []
    h = writer_setup(log, faults=crash_at(point, nth))
    with pytest.raises(SimulatedCrash):
        await h.run(WRITER_BOSS)
    [summary] = await h.store.runs()
    again = h.restart(extra_tools=[append_tool(log)])
    outcome = await again.resume(summary.run_id)
    assert outcome.status is RunStatus.COMPLETED, outcome
    assert len(await children(again, summary.run_id)) == 1
    assert log == ["hello"]
    assert (await again.store.verify(summary.run_id)).ok


async def test_crash_during_childs_write_is_reconciled_not_repeated() -> None:
    log: list[str] = []
    h = writer_setup(log, faults=crash_at("tool:after_invoke"))
    with pytest.raises(SimulatedCrash):
        await h.run(WRITER_BOSS)
    [summary] = await h.store.runs()
    again = h.restart(extra_tools=[append_tool(log)])
    blocked = await again.resume(summary.run_id)
    assert blocked.status is RunStatus.PAUSED and blocked.blocked_call == "w1"
    state = RunState.from_events(summary.run_id, await again.events(summary.run_id))
    [kid] = [t for t in state.tasks.values() if t.parent_id]
    assert kid.status is TaskStatus.BLOCKED
    await operator.reconcile(
        again.store, again.legion.locks, summary.run_id, "w1", outcome="applied", by=OPERATOR
    )
    done = await again.resume(summary.run_id)
    assert done.status is RunStatus.COMPLETED
    assert log == ["hello"]


async def test_cancelling_the_parent_marks_the_childs_write_in_doubt() -> None:
    log: list[str] = []
    h = writer_setup(log, hang=True)
    running = asyncio.create_task(h.run(WRITER_BOSS))
    while not log:
        await asyncio.sleep(0.01)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    [summary] = await h.store.runs()
    state = RunState.from_events(summary.run_id, await h.events(summary.run_id))
    assert state.status is RunStatus.CANCELLED
    assert all(t.status is TaskStatus.CANCELLED for t in state.tasks.values())
    [kid] = [t for t in state.tasks.values() if t.parent_id]
    assert "w1" in kid.in_doubt


class KillsTheBoss(NullIdentityPort):
    def __init__(self) -> None:
        self.armed = False

    async def kill_state(self, identity: AgentIdentity) -> KillState:
        if self.armed and identity.agent_ref == "tester":
            return KillState.KILLED
        return KillState.ACTIVE

    async def agent_identity(self, agent_ref: str) -> AgentIdentity:
        if agent_ref == "reader":
            self.armed = True
        return await super().agent_identity(agent_ref)


async def test_killing_the_parent_stops_the_child() -> None:
    h = setup([delegate(), reply("done")], identity=KillsTheBoss())
    outcome = await h.run(boss())
    assert outcome.error_code == "killed"
    state = RunState.from_events(outcome.run_id, await h.events(outcome.run_id))
    assert all(t.status is TaskStatus.FAILED for t in state.tasks.values())


async def test_delegating_with_one_tool_call_left_is_refused() -> None:
    # Found by the property test: the delegate call itself costs a tool call, so a parent with
    # one left has nothing to give. It used to reserve that last call for the child and then
    # charge the delegation on top of it.
    h = setup([call("read_file", {"path": "docs/a.md"}, id="r0"), delegate(), reply("done")])
    outcome = await h.run(boss(budget=BudgetLimits(tool_calls=2)))
    assert "no tool_calls budget left" in await refusal(h, outcome.run_id)
    consumed = [
        Decimal(p["total"])
        for p in await h.payloads(outcome.run_id, E.BUDGET_CONSUMED)
        if p["dimension"] == "tool_calls"
    ]
    assert max(consumed) <= 2


class VetoesChildren(NullIdentityPort):
    async def on_delegation(self, parent: Grant, child: Grant) -> None:
        from legion.domain.errors import LegionError

        raise LegionError(f"identity service won't register {child.identity.agent_ref}")


async def test_identity_service_veto_is_a_clean_refusal() -> None:
    h = setup([delegate(), reply("carried on")], identity=VetoesChildren())
    outcome = await h.run(boss())
    assert outcome.status is RunStatus.COMPLETED and outcome.in_doubt == ()
    assert "won't register reader" in await refusal(h, outcome.run_id)
    assert await children(h, outcome.run_id) == []
    types = await h.types(outcome.run_id)
    assert E.TOOL_STARTED not in types
