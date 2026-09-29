# Where credentials meet retries, kill state, delegation, approvals and crashes. Each test pins
# down one boundary Legion can control; ADR 0019 has the tables these add up to.

from pathlib import Path
from typing import Any

import pytest

from legion.authority.policy import Rule, Verdict
from legion.domain.action import EffectClass
from legion.domain.agent import AgentSpec, ModelRequirement
from legion.domain.grant import DelegationLimits
from legion.domain.states import RunStatus
from legion.events.sqlite_store import SqliteEventStore
from legion.events.types import EventType
from legion.kernel import operator
from legion.models.scripted import call, reply
from legion.ports.credentials import Assurance
from legion.ports.identity import AgentIdentity, KillState, NullIdentityPort
from tests.credential_lab import LabAuthority, effect_tool, repo_tools
from tests.support import OPERATOR, SimulatedCrash, agent, build, crash_at
from tests.unit.test_credential_policy import MAPPING, Clock

E = EventType
TOUCH_A = call("touch_repo", {"repo": "repo-A"})
READ_A = call("read_repo", {"repo": "repo-A"})


class Switch(NullIdentityPort):
    """Kill state a test can flip."""

    def __init__(self) -> None:
        self.dead = False

    async def kill_state(self, identity: AgentIdentity) -> KillState:
        return KillState.KILLED if self.dead else KillState.ACTIVE


def harness(
    steps: list[Any],
    tools: list[Any],
    lab: LabAuthority,
    *,
    mapping: Any = MAPPING,
    clock: Clock | None = None,
    **kw: Any,
) -> Any:
    clock = clock or Clock()
    lab.clock = clock
    return build(
        steps,
        extra_tools=tools,
        grantable=kw.pop("grantable", ("repo.*", "agent.delegate:**")),
        options={
            "credential_mappings": {"github": mapping},
            "credential_authorities": {"lab": lab},
            "trusted_authorities": ("lab",),
            "credential_timeout_s": 0.2,
        },
        now=clock,
        **kw,
    )


def touch_spec() -> AgentSpec:
    return agent(tools=["touch_repo"], capabilities=["repo.read:repo-A"])


async def payloads(h: Any, run_id: str, kind: EventType) -> list[dict[str, Any]]:
    return await h.payloads(run_id, kind)


# Step 14: the retry matrix. The first attempt fails retryably; something changes before the
# retry; what the retry does depends only on what changed.

STATES = ["expired", "revoked", "unknown", "unavailable", "killed"]


def induce(state: str, lab: LabAuthority, clock: Clock, identity: Switch) -> Any:
    def on_call(n: int) -> None:
        if n != 1:
            return
        if state == "expired":
            clock.advance(120)
        elif state == "revoked":
            lab.revoked.add("cred-001")
        elif state == "unknown":
            lab.unknown_active = True
        elif state == "unavailable":
            lab.fail_active = True
        elif state == "killed":
            identity.dead = True

    return on_call


@pytest.mark.parametrize(
    "effect", [EffectClass.PURE, EffectClass.READ, EffectClass.WRITE_IDEMPOTENT]
)
@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("required", [Assurance.UNVERIFIED, Assurance.BOUND])
async def test_retry_matrix(effect: EffectClass, state: str, required: Assurance) -> None:
    lab, clock, identity, seen = LabAuthority(), Clock(), Switch(), []
    tool = effect_tool(effect, seen, fail_first=True, on_call=induce(state, lab, clock, identity))
    mapping = MAPPING.model_copy(update={"minimum": required})
    h = harness(
        [TOUCH_A, reply("ok")], [tool], lab, mapping=mapping, clock=clock, identity=identity
    )
    outcome = await h.run(touch_spec())
    if state == "expired":
        # the same call, a new credential, the same authority
        assert outcome.status is RunStatus.COMPLETED
        assert seen == lab.secrets[:2] and len(set(seen)) == 2
        records = await payloads(h, outcome.run_id, E.CREDENTIAL_RESOLVED)
        assert {r["call_id"] for r in records} == {"call_1"}
        assert all(r["permissions"] == ["contents:read"] for r in records)
        return
    # everything else: no second attempt, no second credential
    assert len(seen) == 1 and len(lab.issued) == 1
    if state == "killed":
        assert outcome.status is RunStatus.FAILED
        return
    [record] = await payloads(h, outcome.run_id, E.CREDENTIAL_REFUSED)
    expected = {
        "revoked": "no longer active at the authority",
        "unknown": "didn't say whether it's still active",
        "unavailable": "couldn't check it's still active: ConnectionError",
    }[state]
    assert any(expected in p for p in record["problems"])


@pytest.mark.parametrize("effect", [EffectClass.WRITE, EffectClass.EXTERNAL_IRREVERSIBLE])
async def test_non_idempotent_writes_are_never_retried(effect: EffectClass) -> None:
    lab, seen = LabAuthority(), []
    tool = effect_tool(effect, seen, fail_first=True)
    lenient = MAPPING.model_copy(update={"minimum": Assurance.UNVERIFIED})
    h = harness([TOUCH_A, reply("ok")], [tool], lab, mapping=lenient, rules=[])
    await h.run(touch_spec())
    assert len(seen) == 1 and len(lab.issued) == 1


# Step 15: kill state, revocation and the moment of dispatch


def at(point: str, action: Any) -> Any:
    def hook(name: str) -> None:
        if name == point:
            action()

    return hook


async def test_killed_after_issue_before_start_runs_nothing() -> None:
    lab, identity, seen = LabAuthority(), Switch(), []
    h = harness(
        [READ_A, reply("ok")],
        repo_tools(seen),
        lab,
        identity=identity,
        faults=at("after:credential.resolved", lambda: setattr(identity, "dead", True)),
    )
    outcome = await h.run(agent(tools=["read_repo"], capabilities=["repo.read:repo-A"]))
    assert outcome.status is RunStatus.FAILED and seen == []
    # handed back, best effort
    assert "cred-001" in lab.revoked


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ("revoked", "no longer active at the authority"),
        ("unavailable", "couldn't check it's still active"),
        ("unknown", "didn't say whether it's still active"),
    ],
)
async def test_change_after_issue_before_start_runs_nothing(change: str, problem: str) -> None:
    lab, seen = LabAuthority(), []

    def flip() -> None:
        if change == "revoked":
            lab.revoked.add("cred-001")
        elif change == "unavailable":
            lab.fail_active = True
        else:
            lab.unknown_active = True

    h = harness(
        [READ_A, reply("ok")], repo_tools(seen), lab, faults=at("after:credential.resolved", flip)
    )
    outcome = await h.run(agent(tools=["read_repo"], capabilities=["repo.read:repo-A"]))
    assert seen == []
    [record] = await payloads(h, outcome.run_id, E.CREDENTIAL_REFUSED)
    assert any(problem in p for p in record["problems"])


async def test_kill_after_start_leaves_the_effect_recorded_not_undone() -> None:
    lab, identity, seen = LabAuthority(), Switch(), []
    tool = effect_tool(EffectClass.WRITE, seen, on_call=lambda n: setattr(identity, "dead", True))
    lenient = MAPPING.model_copy(update={"minimum": Assurance.UNVERIFIED})
    h = harness([TOUCH_A, reply("ok")], [tool], lab, mapping=lenient, identity=identity, rules=[])
    outcome = await h.run(touch_spec())
    # the call happened; Legion stops afterwards and says so, it doesn't pretend otherwise
    assert len(seen) == 1
    assert len(await payloads(h, outcome.run_id, E.TOOL_COMPLETED)) == 1
    assert outcome.status is RunStatus.FAILED


async def test_revocation_during_the_call_does_not_undo_it() -> None:
    lab, seen = LabAuthority(), []
    tool = effect_tool(EffectClass.WRITE, seen, on_call=lambda n: lab.revoked.add("cred-001"))
    lenient = MAPPING.model_copy(update={"minimum": Assurance.UNVERIFIED})
    h = harness([TOUCH_A, reply("ok")], [tool], lab, mapping=lenient, rules=[])
    outcome = await h.run(touch_spec())
    assert len(seen) == 1
    assert len(await payloads(h, outcome.run_id, E.TOOL_COMPLETED)) == 1


async def test_revocation_after_the_last_check_is_not_caught() -> None:
    # The residual window: after Legion's last check (credential:checked, then the kill check,
    # then tool.started) the tool is called. A revocation in there isn't seen by Legion; only
    # the downstream system can refuse a revoked credential at that point (ADR 0019).
    lab, seen = LabAuthority(), []
    h = harness(
        [READ_A, reply("ok")],
        repo_tools(seen),
        lab,
        faults=at("tool:before_invoke", lambda: lab.revoked.add("cred-001")),
    )
    await h.run(agent(tools=["read_repo"], capabilities=["repo.read:repo-A"]))
    assert len(seen) == 1


# Step 16: delegation. Credential(child) <= Grant(child) <= Grant(parent).

READER = AgentSpec(
    name="reader",
    instructions="read",
    model=ModelRequirement(profile="child/reader"),
    tools=("read_repo", "delegate"),
    capabilities=("repo.read:repo-A", "agent.delegate:**"),
    delegation=DelegationLimits(max_depth=1, max_children=1),
)


def boss(caps: tuple[str, ...] = ("repo.read:repo-A",), depth: int = 2) -> AgentSpec:
    return agent(
        tools=["read_repo", "delegate"],
        capabilities=[*caps, "agent.delegate:**"],
        delegation=DelegationLimits(max_depth=depth, max_children=2),
    )


def delegation(
    lab: LabAuthority,
    parent_steps: list[Any],
    child_steps: list[Any],
    seen: list[tuple[str, str]],
    child: AgentSpec = READER,
    **kw: Any,
) -> Any:
    return harness(
        parent_steps,
        repo_tools(seen),
        lab,
        agents={"reader": child},
        scripts={"child/reader": child_steps},
        max_delegation_depth=3,
        **kw,
    )


DELEGATE = call("delegate", {"agent": "reader", "objective": "read"})


async def test_child_credential_matching_its_grant_runs() -> None:
    lab, seen = LabAuthority(), []
    h = delegation(lab, [DELEGATE, reply("done")], [READ_A, reply("read")], seen)
    outcome = await h.run(boss())
    assert len(seen) == 1
    [record] = await payloads(h, outcome.run_id, E.CREDENTIAL_RESOLVED)
    assert record["subject"] == "reader" and record["assurance"] == "bound"


@pytest.mark.parametrize(
    "mods",
    [
        {"permissions": ["contents:read", "contents:write"]},
        {"principal": "human:mallory"},
        {"resource": None},
        {"resource": "repo-*"},
        {"expires_at": "far"},
    ],
    ids=["write", "principal", "any-resource", "glob-resource", "lifetime"],
)
async def test_child_credential_wider_than_its_grant_is_refused(mods: dict[str, Any]) -> None:
    from datetime import timedelta

    clock = Clock()
    if mods.get("expires_at") == "far":
        mods = {"expires_at": clock() + timedelta(days=1)}
    lab, seen = LabAuthority(mods=mods), []
    h = delegation(lab, [DELEGATE, reply("done")], [READ_A, reply("read")], seen, clock=clock)
    outcome = await h.run(boss())
    assert seen == []
    [record] = await payloads(h, outcome.run_id, E.CREDENTIAL_REFUSED)
    assert record["subject"] == "reader" and record["assurance"] is None


async def test_child_asking_for_another_resource_gets_no_credential() -> None:
    lab, seen = LabAuthority(), []
    other = AgentSpec.model_validate(
        {**READER.model_dump(), "capabilities": ["repo.read:repo-B"], "tools": ["read_repo"]}
    )
    h = delegation(
        lab,
        [DELEGATE, reply("done")],
        [call("read_repo", {"repo": "repo-B"}), reply("x")],
        seen,
        child=other,
    )
    outcome = await h.run(boss())
    assert lab.issued == [] and seen == []
    [refusal] = await payloads(h, outcome.run_id, E.ACTION_REFUSED)
    assert refusal["reason_code"] == "delegation_refused"


async def test_grandchild_asking_for_more_gets_nothing() -> None:
    lab, seen = LabAuthority(), []
    grandchild_call = call(
        "delegate", {"agent": "reader", "objective": "more", "capabilities": ["repo.read:repo-B"]}
    )
    h = delegation(
        lab,
        [DELEGATE, reply("done")],
        [grandchild_call, reply("read")],
        seen,
    )
    outcome = await h.run(boss())
    assert lab.issued == [] and seen == []
    refusals = [r["reason_code"] for r in await payloads(h, outcome.run_id, E.ACTION_REFUSED)]
    assert "delegation_refused" in refusals


async def test_sibling_credential_is_refused_for_the_second_child() -> None:
    lab, seen = LabAuthority(replay_first=True), []
    h = delegation(
        lab,
        [DELEGATE, call("delegate", {"agent": "reader", "objective": "again"}), reply("done")],
        [READ_A, reply("read"), READ_A, reply("read")],
        seen,
    )
    outcome = await h.run(boss())
    assert len(seen) == 1
    [record] = await payloads(h, outcome.run_id, E.CREDENTIAL_REFUSED)
    assert "bound to a different grant" in record["problems"]


async def test_child_reusing_a_reference_after_resume_is_refused() -> None:
    lab, seen = LabAuthority(fixed_ref="same"), []
    h = delegation(
        lab,
        [DELEGATE, reply("done")],
        [READ_A, call("read_repo", {"repo": "repo-A"}, id="again"), reply("read")],
        seen,
        by_turn=True,
        faults=crash_at("after:tool.completed"),
    )
    with pytest.raises(SimulatedCrash):
        await h.run(boss())
    [summary] = await h.store.runs()
    await h.restart(faults=None).resume(summary.run_id)
    assert len(seen) == 1
    assert any(
        "already used" in r["problems"][0]
        for r in await payloads(h, summary.run_id, E.CREDENTIAL_REFUSED)
    )


async def test_child_out_of_reserved_budget_gets_no_credential() -> None:
    lab, seen = LabAuthority(), []
    tight = AgentSpec.model_validate(
        {**READER.model_dump(), "tools": ["read_repo"], "budget": {"tool_calls": 1}}
    )
    h = delegation(
        lab,
        [DELEGATE, reply("done")],
        [READ_A, call("read_repo", {"repo": "repo-A"}), reply("read")],
        seen,
        child=tight,
    )
    await h.run(boss())
    assert len(lab.issued) == 1 and len(seen) == 1


# Step 17: approvals. A credential failure never makes an approval reusable.

APPROVE = [Rule(decision=Verdict.REQUIRE_APPROVAL, tool="create_issue")]
ISSUE = {"repo": "repo-A", "title": "x"}


async def approved_run(lab: LabAuthority, *, clock: Clock | None = None, **kw: Any) -> Any:
    seen: list[tuple[str, str]] = []
    steps = [
        call("create_issue", ISSUE, id="c1"),
        call("create_issue", ISSUE, id="c2"),
        reply("done"),
    ]
    strict = MAPPING.model_copy(update={"minimum": Assurance.BOUND})
    h = harness(
        steps,
        repo_tools(seen),
        lab,
        mapping=strict,
        clock=clock,
        rules=APPROVE,
        by_turn=True,
        **kw,
    )
    first = await h.run(agent(tools=["create_issue"], capabilities=["repo.issue.create:repo-A"]))
    assert first.status is RunStatus.PAUSED
    await operator.decide(
        h.store, h.legion.locks, first.approval_id, approve=True, by=OPERATOR, now=h.legion.now
    )
    return h, first, seen


@pytest.mark.parametrize(
    "lab",
    [
        LabAuthority(mods={"permissions": ["issues:write", "admin"]}),
        LabAuthority(mods={"principal": "human:mallory"}),
        LabAuthority(mods={"call_id": "call_9"}),
        LabAuthority(evidence="malformed"),
        LabAuthority(fail="crash"),
        LabAuthority(mods={"resource": "repo-B"}),
    ],
    ids=["too-broad", "principal", "call", "malformed", "unavailable", "different"],
)
async def test_credential_failure_consumes_the_approval_and_the_next_call_needs_another(
    lab: LabAuthority,
) -> None:
    h, first, seen = await approved_run(lab)
    second = await h.restart().resume(first.run_id)
    assert seen == []
    [refused] = await payloads(h, first.run_id, E.CREDENTIAL_REFUSED)
    assert refused["call_id"] == "c1"
    state_after = await operator.find(h.store, first.approval_id)
    assert state_after[1].status == "consumed"
    # the same arguments again, as a new call: its own approval, not the old one
    assert second.status is RunStatus.PAUSED and second.approval_id != first.approval_id


async def test_approved_call_with_a_good_credential_runs_once() -> None:
    lab = LabAuthority()
    h, first, seen = await approved_run(lab)
    second = await h.restart().resume(first.run_id)
    assert len(seen) == 1
    assert second.approval_id != first.approval_id


async def test_expiry_before_execution_is_replaced_under_the_same_approval() -> None:
    clock = Clock()
    lab = LabAuthority()
    h, first, seen = await approved_run(lab, clock=clock, faults=None)
    fresh = h.restart(faults=at_once("after:credential.resolved", lambda: clock.advance(120)))
    await fresh.resume(first.run_id)
    # one approval, one call, a replaced credential: the call didn't change, so neither did the
    # approval's binding
    assert len(seen) == 1 and len(lab.issued) == 2
    assert {r["call_id"] for r in await payloads(h, first.run_id, E.CREDENTIAL_RESOLVED)} == {"c1"}


def at_once(point: str, action: Any) -> Any:
    done = [False]

    def hook(name: str) -> None:
        if name == point and not done[0]:
            done[0] = True
            action()

    return hook


async def test_crash_after_the_approval_is_used_runs_the_same_call_on_resume() -> None:
    lab = LabAuthority()
    h, first, seen = await approved_run(lab)
    crashing = h.restart(faults=crash_at("after:credential.resolved"))
    with pytest.raises(SimulatedCrash):
        await crashing.resume(first.run_id)
    assert seen == []
    await h.restart(faults=None).resume(first.run_id)
    # the approval was for c1 and c1 ran once, with a new credential
    assert len(seen) == 1 and len(lab.issued) == 2


async def test_kill_before_execution_leaves_the_approval_used_and_nothing_run() -> None:
    identity = Switch()
    lab = LabAuthority()
    h, first, seen = await approved_run(lab, identity=identity)
    identity.dead = True
    outcome = await h.restart().resume(first.run_id)
    assert outcome.status is RunStatus.FAILED and seen == [] and lab.issued == []


# Step 18: crashes around the credential boundary

BEFORE_START = [
    "after:budget.consumed",
    "during-issue",
    "credential:obtained",
    "after:credential.resolved",
    "credential:checked",
    "before:tool.started",
]
AFTER_START = [
    "after:tool.started",
    "tool:before_invoke",
    "inside-tool",
    "tool:after_invoke",
    "before:tool.completed",
]


class Crash:
    def __init__(self, point: str) -> None:
        self.point = point
        self.fired = False

    def fault(self, name: str) -> None:
        if name == self.point and not self.fired:
            self.fired = True
            raise SimulatedCrash(name)

    def in_code(self, where: str) -> None:
        if where == self.point and not self.fired:
            self.fired = True
            raise SimulatedCrash(where)


@pytest.mark.parametrize("effect", [EffectClass.READ, EffectClass.WRITE])
@pytest.mark.parametrize("point", BEFORE_START + AFTER_START)
async def test_crash_matrix(point: str, effect: EffectClass, tmp_path: Path) -> None:
    crash = Crash(point)
    lab = LabAuthority(on_issue=lambda n: crash.in_code("during-issue"))
    effects: list[str] = []

    def on_call(n: int) -> None:
        effects.append("done")
        crash.in_code("inside-tool")

    seen: list[str] = []
    tool = effect_tool(effect, seen, on_call=on_call)
    store = SqliteEventStore(tmp_path / "e.db")
    strict = MAPPING.model_copy(update={"minimum": Assurance.BOUND})
    h = harness(
        [call("touch_repo", {"repo": "repo-A"}, id="t1"), reply("ok")],
        [tool],
        lab,
        mapping=strict,
        store=store,
        by_turn=True,
        faults=crash.fault,
        rules=[],
    )
    with pytest.raises(SimulatedCrash):
        await h.run(touch_spec())
    sent_before_crash = len(effects)
    issued_before = len(lab.issued)
    [summary] = await store.runs()
    outcome = await h.restart(faults=None).resume(summary.run_id)

    if point in BEFORE_START:
        # definitely not sent: a fresh credential, and the call runs once
        assert sent_before_crash == 0
        assert outcome.status is RunStatus.COMPLETED
        assert len(effects) == 1
        assert len(lab.issued) == issued_before + 1
    elif effect is EffectClass.WRITE:
        # may have been sent: in doubt, not repeated, no new credential
        assert outcome.status is RunStatus.PAUSED and outcome.blocked_call == "t1"
        assert len(effects) == sent_before_crash <= 1
        assert len(lab.issued) == issued_before
    else:
        # a read may run again, with a fresh credential
        assert outcome.status is RunStatus.COMPLETED
        assert len(lab.issued) == issued_before + 1

    refs = [r["credential_ref"] for r in await h.payloads(summary.run_id, E.CREDENTIAL_RESOLVED)]
    assert len(refs) == len(set(refs))
    store.close()
    data = (tmp_path / "e.db").read_bytes()
    assert all(secret.encode() not in data for secret in lab.secrets)
