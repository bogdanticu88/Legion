from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from legion.domain.action import Action, EffectClass
from legion.domain.budget import BudgetLimits
from legion.domain.capability import Capability
from legion.domain.errors import RunLocked
from legion.domain.grant import Grant
from legion.domain.messages import ToolCallPart
from legion.domain.principal import IdentityContext
from legion.domain.states import RunStatus
from legion.events.types import EventType
from legion.kernel import approvals, operator
from legion.kernel.operator import OperatorError
from legion.kernel.services import TaskRuntime
from legion.models.scripted import call, reply
from legion.ports.identity import AgentIdentity
from legion.tools.base import ToolContext
from legion.tools.native import tool
from tests.support import OPERATOR, PRINCIPAL, SimulatedCrash, agent, build, crash_at

E = EventType


class HostArgs(BaseModel):
    host: str


class DeployArgs(BaseModel):
    env: str


class PayArgs(BaseModel):
    to: str
    amount: int


class World:
    def __init__(self) -> None:
        self.isolated: list[str] = []
        self.deployed: list[str] = []
        self.paid: list[tuple[str, int]] = []

    def tools(self) -> list[Any]:
        @tool(
            effect=EffectClass.EXTERNAL_IRREVERSIBLE,
            capabilities=["endpoint.isolate"],
            resource=lambda a: a.host,
        )
        def isolate(args: HostArgs, ctx: ToolContext) -> str:
            """Cut a host off the network."""
            self.isolated.append(args.host)
            return f"isolated {args.host}"

        @tool(
            effect=EffectClass.EXTERNAL_IRREVERSIBLE,
            capabilities=["deploy.run"],
            resource=lambda a: a.env,
        )
        def deploy(args: DeployArgs, ctx: ToolContext) -> str:
            """Deploy the current build."""
            self.deployed.append(args.env)
            return f"deployed to {args.env}"

        @tool(
            effect=EffectClass.EXTERNAL_IRREVERSIBLE,
            capabilities=["payments.send"],
            resource=lambda a: a.to,
        )
        def pay(args: PayArgs, ctx: ToolContext) -> str:
            """Send money."""
            self.paid.append((args.to, args.amount))
            return "paid"

        return [isolate, deploy, pay]


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


SPEC = agent(
    tools=["isolate", "deploy", "pay"],
    capabilities=["endpoint.isolate:**", "deploy.run:**", "payments.send:**"],
)
GRANTABLE = ("endpoint.isolate:**", "deploy.run:**", "payments.send:**")


def setup(steps: list[Any], world: World | None = None, **kw: Any) -> tuple[Any, World]:
    world = world or World()
    h = build(steps, extra_tools=world.tools(), grantable=GRANTABLE, by_turn=True, **kw)
    return h, world


async def paused(h: Any) -> Any:
    outcome = await h.run(SPEC)
    assert outcome.status is RunStatus.PAUSED
    assert outcome.approval_id
    return outcome


async def approve(h: Any, approval_id: str, **kw: Any) -> Any:
    return await operator.decide(
        h.store, h.legion.locks, approval_id, approve=True, by=OPERATOR, **kw
    )


async def test_pauses_and_runs_once_approved() -> None:
    h, world = setup([call("isolate", {"host": "HOST-A"}, id="c1"), reply("done")])
    outcome = await paused(h)
    assert world.isolated == []
    await approve(h, outcome.approval_id)
    done = await h.restart(extra_tools=world.tools()).resume(outcome.run_id)
    assert done.status is RunStatus.COMPLETED
    assert world.isolated == ["HOST-A"]


async def test_approval_for_host_a_does_not_cover_host_b() -> None:
    h, world = setup(
        [
            call("isolate", {"host": "HOST-A"}, id="c1"),
            call("isolate", {"host": "HOST-B"}, id="c2"),
            reply("done"),
        ]
    )
    first = await paused(h)
    await approve(h, first.approval_id)
    second = await h.restart(extra_tools=world.tools()).resume(first.run_id)
    # HOST-A ran; HOST-B is a new action and needs its own approval
    assert world.isolated == ["HOST-A"]
    assert second.status is RunStatus.PAUSED
    assert second.approval_id != first.approval_id


@pytest.mark.parametrize(
    ("tool_name", "a", "b"),
    [
        ("isolate", {"host": "HOST-A"}, {"host": "HOST-B"}),
        ("deploy", {"env": "staging"}, {"env": "production"}),
        ("pay", {"to": "acme", "amount": 100}, {"to": "acme", "amount": 1000}),
    ],
)
def test_binding_changes_with_security_relevant_arguments(
    tool_name: str, a: dict[str, Any], b: dict[str, Any]
) -> None:
    world = World()
    spec = next(t for t in world.tools() if t.spec.name == tool_name)
    hashes = []
    for arguments in (a, b):
        resource = spec.resource_of(arguments)
        action = Action(
            tool=tool_name,
            arguments=arguments,
            resource=resource,
            required=tuple(Capability(name=n, resource=resource) for n in spec.spec.capabilities),
            effect=spec.spec.effect,
            grant_id="g",
            task_id="t",
        )
        task = TaskRuntime(
            run_id="r",
            task_id="t",
            parent_task_id=None,
            agent=SPEC,
            grant=_grant(),
            identity=AgentIdentity(agent_ref="tester", source="local"),
            model=None,  # type: ignore[arg-type]
        )
        bound = approvals.binding(
            task=task,
            call=ToolCallPart(id="c1", name=tool_name, arguments=arguments),
            action=action,
            tool=spec.spec,
            settings={},
            credential_refs={},
        )
        hashes.append(approvals.binding_hash(bound))
    assert hashes[0] != hashes[1]


def _grant() -> Any:
    return Grant(
        id="g",
        capabilities=frozenset(Capability.parse(c) for c in GRANTABLE),
        budget=BudgetLimits(),
        identity=IdentityContext(principal=PRINCIPAL, agent_ref="tester"),
        issuer="t",
    )


async def test_changed_environment_invalidates_the_approval() -> None:
    h, world = setup(
        [call("deploy", {"env": "staging"}, id="c1"), reply("x")], settings={"region": "eu"}
    )
    outcome = await paused(h)
    await approve(h, outcome.approval_id)
    moved = h.restart(extra_tools=world.tools(), settings={"region": "us"})
    done = await moved.resume(outcome.run_id)
    assert world.deployed == []
    [refused] = await moved.payloads(outcome.run_id, E.ACTION_REFUSED)
    assert refused["reason_code"] == "approval_mismatch"
    assert await moved.payloads(outcome.run_id, E.APPROVAL_INVALIDATED)
    assert done.status is RunStatus.COMPLETED


async def test_approval_expires() -> None:
    clock = Clock()
    h, world = setup([call("pay", {"to": "acme", "amount": 100}, id="c1"), reply("x")], now=clock)
    outcome = await paused(h)
    await approve(h, outcome.approval_id, now=clock)
    clock.t += timedelta(hours=2)
    done = await h.restart(extra_tools=world.tools(), now=clock).resume(outcome.run_id)
    assert world.paid == []
    [refused] = await h.payloads(outcome.run_id, E.ACTION_REFUSED)
    assert refused["reason_code"] == "approval_expired"
    assert done.status is RunStatus.COMPLETED


async def test_cannot_approve_after_expiry() -> None:
    clock = Clock()
    h, _ = setup([call("pay", {"to": "acme", "amount": 100}, id="c1"), reply("x")], now=clock)
    outcome = await paused(h)
    clock.t += timedelta(hours=2)
    with pytest.raises(OperatorError, match="expired"):
        await approve(h, outcome.approval_id, now=clock)


async def test_approval_is_not_reusable() -> None:
    same = {"host": "HOST-A"}
    h, world = setup(
        [call("isolate", same, id="c1"), call("isolate", same, id="c2"), reply("done")]
    )
    first = await paused(h)
    await approve(h, first.approval_id)
    second = await h.restart(extra_tools=world.tools()).resume(first.run_id)
    # the identical second call gets its own request instead of riding on the first approval
    assert world.isolated == ["HOST-A"]
    assert second.status is RunStatus.PAUSED and second.approval_id != first.approval_id
    with pytest.raises(OperatorError, match="consumed"):
        await approve(h, first.approval_id)


async def test_denied_action_stays_denied_after_restart() -> None:
    h, world = setup([call("isolate", {"host": "HOST-A"}, id="c1"), reply("ok")])
    outcome = await paused(h)
    await operator.decide(
        h.store, h.legion.locks, outcome.approval_id, approve=False, by=OPERATOR, note="wrong host"
    )
    done = await h.restart(extra_tools=world.tools()).resume(outcome.run_id)
    assert done.status is RunStatus.COMPLETED
    assert world.isolated == []
    [refused] = await h.payloads(outcome.run_id, E.ACTION_REFUSED)
    assert refused["reason_code"] == "approval_denied"
    assert "wrong host" in refused["message"]
    with pytest.raises(OperatorError):
        await approve(h, outcome.approval_id)


async def test_resume_without_decision_keeps_waiting() -> None:
    h, world = setup([call("isolate", {"host": "HOST-A"}, id="c1"), reply("ok")])
    outcome = await paused(h)
    before = len(await h.events(outcome.run_id))
    again = await h.restart(extra_tools=world.tools()).resume(outcome.run_id)
    assert again.status is RunStatus.PAUSED and again.approval_id == outcome.approval_id
    assert len(await h.events(outcome.run_id)) == before
    assert world.isolated == []


async def test_crash_after_consuming_approval_but_before_running() -> None:
    world = World()
    h, _ = setup(
        [call("isolate", {"host": "HOST-A"}, id="c1"), reply("done")],
        world,
        faults=crash_at("after:approval.consumed"),
    )
    outcome = await paused(h)
    await approve(h, outcome.approval_id)
    with pytest.raises(SimulatedCrash):
        await h.resume(outcome.run_id)
    assert world.isolated == []
    done = await h.restart(extra_tools=world.tools()).resume(outcome.run_id)
    assert done.status is RunStatus.COMPLETED
    assert world.isolated == ["HOST-A"]


async def test_crash_after_irreversible_action_needs_reconciliation() -> None:
    world = World()
    h, _ = setup(
        [call("isolate", {"host": "HOST-A"}, id="c1"), reply("done")],
        world,
        faults=crash_at("tool:after_invoke"),
    )
    outcome = await paused(h)
    await approve(h, outcome.approval_id)
    with pytest.raises(SimulatedCrash):
        await h.resume(outcome.run_id)
    assert world.isolated == ["HOST-A"]
    blocked = await h.restart(extra_tools=world.tools()).resume(outcome.run_id)
    assert blocked.status is RunStatus.PAUSED and blocked.blocked_call == "c1"
    assert world.isolated == ["HOST-A"]


async def test_approval_needs_the_run_to_be_idle() -> None:
    h, _ = setup([call("isolate", {"host": "HOST-A"}, id="c1"), reply("ok")])
    outcome = await paused(h)
    with h.legion.locks.hold(outcome.run_id), pytest.raises(RunLocked):
        await approve(h, outcome.approval_id)


async def test_failed_write_leaves_approval_pending(tmp_path: Path) -> None:
    h, _ = setup([call("isolate", {"host": "HOST-A"}, id="c1"), reply("ok")])
    outcome = await paused(h)

    class Broken:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def append(self, *a: Any, **k: Any) -> Any:
            raise OSError("disk full")

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    with pytest.raises(OSError):
        await operator.decide(
            Broken(h.store), h.legion.locks, outcome.approval_id, approve=True, by=OPERATOR
        )
    [pending] = await operator.approvals(h.store)
    assert pending.approval.status == "requested"


async def test_approval_shows_what_it_allows() -> None:
    h, _ = setup([call("pay", {"to": "acme", "amount": 100}, id="c1", text="paying the invoice")])
    outcome = await paused(h)
    _, approval = await operator.find(h.store, outcome.approval_id)
    subject = approval.subject
    assert subject["tool"] == "pay" and subject["resource"] == "acme"
    assert subject["arguments"] == {"to": "acme", "amount": 100}
    assert subject["effect"] == "external_irreversible"
    assert subject["model_note"] == "paying the invoice"
    assert subject["on_behalf_of"] == ["human:tester"]
