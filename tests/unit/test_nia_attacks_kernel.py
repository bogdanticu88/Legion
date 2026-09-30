# Attacks on how the kernel uses the NIA port, from the Phase 5B.1 review: start, kill races,
# delegation, resume, model influence, and Legion without NIA. test_observe_* pin behaviour that
# isn't a vulnerability under ADR 0020 but should stay visible.

from __future__ import annotations

from typing import Any

import pytest

from legion.authority.policy import Rule, Verdict
from legion.domain.states import RunStatus
from legion.events.types import EventType
from legion.kernel import operator
from legion.models.scripted import call, reply
from tests.nia_lab import FakeNia, serve
from tests.support import OPERATOR, agent, build
from tests.unit.test_nia_identity import (
    AGENTS,
    BOSS,
    HELPER,
    READ,
    _kill_ref_at,
    delegation,
    harness,
    port,
)

E = EventType


def tool_runs(events: list[Any], who: str | None = None) -> list[Any]:
    return [
        e
        for e in events
        if e.type is E.TOOL_STARTED
        and (who is None or e.agent_id == who)
        # delegate itself shows up as a tool on the parent; only count real tools
        and e.payload.get("call_id") != "d1"
    ]


async def test_killed_at_start_never_reaches_the_model() -> None:
    nia = FakeNia(agents={"agent:tester": "killed"})
    with serve(nia) as url:
        h, identity = harness(url, [reply("final answer from a killed agent")])
        outcome = await h.run(agent())
        await identity.aclose()
    kinds = await h.types(outcome.run_id)
    assert outcome.status is RunStatus.FAILED and outcome.error_code == "killed"
    assert E.MODEL_REQUESTED not in kinds
    # agent_identity doesn't refuse a killed agent: a run is created and then fails
    assert E.RUN_CREATED in kinds


async def test_suspended_reported_as_killed() -> None:
    nia = FakeNia(agents={"agent:tester": "suspended"})
    with serve(nia) as url:
        h, identity = harness(url, [READ, reply("ok")])
        outcome = await h.run(agent())
        await identity.aclose()
    assert outcome.status is RunStatus.FAILED and outcome.error_code == "killed"


async def test_parent_killed_while_child_runs_stops_child() -> None:
    nia = FakeNia(agents={"agent:tester": "active", "agent:helper": "active"})
    with serve(nia) as url:
        h, identity = delegation(
            url, nia.agents, faults=_kill_ref_at("after:task.started", nia, "agent:tester", nth=2)
        )
        outcome = await h.run(agent(**BOSS))
        await identity.aclose()
    events = await h.events(outcome.run_id)
    assert not [e for e in events if e.type is E.TOOL_COMPLETED and e.agent_id == "helper"]
    assert outcome.status is RunStatus.FAILED and outcome.error_code == "killed"


async def test_child_mapped_to_parent_ref_is_allowed_and_shares_fate() -> None:
    nia = FakeNia(agents={"agent:tester": "active"})
    with serve(nia) as url:
        h, identity = delegation(
            url, nia.agents, port={"agents": {"tester": "agent:tester", "helper": "agent:tester"}}
        )
        outcome = await h.run(agent(**BOSS))
        await identity.aclose()
    events = await h.events(outcome.run_id)
    child = [e for e in events if e.type is E.ACTION_AUTHORIZED and e.agent_id == "helper"]
    assert child and child[0].payload["external"]["ref"] == "agent:tester"


@pytest.mark.parametrize(
    "args",
    [
        {"agent": "helper ", "objective": "read"},
        {"agent": "Helper", "objective": "read"},
        {"agent": "agent:helper", "objective": "read"},
        {"agent": "he\u200blper", "objective": "read"},
        {"agent": "helper", "objective": "read", "nia_ref": "agent:admin"},
        {"agent": "helper", "objective": "read", "context": {"external_id": "agent:admin"}},
    ],
)
async def test_model_cannot_pick_or_smuggle_a_ref(args: dict[str, Any]) -> None:
    nia = FakeNia(
        agents={"agent:tester": "active", "agent:helper": "active", "agent:admin": "active"}
    )
    with serve(nia) as url:
        identity = port(url, agents={**AGENTS, "admin": "agent:admin"})
        h = build(
            [call("delegate", args, id="d1"), reply("done")],
            identity=identity,
            agents={"helper": HELPER},
            scripts={"child/helper": [READ, reply("read")]},
            grantable=("files.read:**", "agent.delegate:**"),
        )
        outcome = await h.run(agent(**BOSS))
        await identity.aclose()
    asked = {p for p, _ in nia.requests}
    assert "/agents/agent%3Aadmin" not in asked
    events = await h.events(outcome.run_id)
    refs = {
        e.payload["external"]["ref"]
        for e in events
        if e.type is E.ACTION_AUTHORIZED and e.payload.get("external")
    }
    assert refs <= {"agent:tester", "agent:helper"}


async def test_observe_resume_after_mapping_change_rebinds_the_run() -> None:
    # A paused run started as agent:tester resumes under whatever the mapping says now; nothing
    # ties the run to the NIA ref it was created under (the "external_id != mapping" check can't
    # fire because the identity is rebuilt from the new mapping on resume).
    nia = FakeNia(agents={"agent:tester": "killed", "agent:other": "active"})
    nia.agents["agent:tester"] = "active"
    rules = [Rule(decision=Verdict.REQUIRE_APPROVAL, tool="read_file")]
    with serve(nia) as url:
        h, identity = harness(url, [READ, reply("ok")], rules=rules, by_turn=True)
        first = await h.run(agent())
        assert first.status is RunStatus.PAUSED
        await operator.decide(h.store, h.legion.locks, first.approval_id, approve=True, by=OPERATOR)
        nia.kill("agent:tester")
        remapped = port(url, agents={"tester": "agent:other", "helper": "agent:helper"})
        second = await h.restart(identity=remapped).resume(first.run_id)
        await identity.aclose()
        await remapped.aclose()
    # the run carries on (model calls checked against agent:other only); the approval, which
    # binds identity.external_id, is invalidated so the approved read doesn't run
    assert second.status is RunStatus.COMPLETED
    kinds = await h.types(first.run_id)
    assert E.APPROVAL_INVALIDATED in kinds and E.TOOL_STARTED not in kinds
    after_restart = [p for p, _ in nia.requests[4:]]
    assert after_restart and set(after_restart) == {"/agents/agent%3Aother"}


async def test_resume_existing_child_after_its_mapping_is_removed() -> None:
    # child pauses on approval; the operator then removes the child's mapping; resume must not
    # let the child act
    nia = FakeNia(agents={"agent:tester": "active", "agent:helper": "active"})
    rules = [Rule(decision=Verdict.REQUIRE_APPROVAL, tool="read_file")]
    with serve(nia) as url:
        h, identity = delegation(url, nia.agents, rules=rules, by_turn=True)
        first = await h.run(agent(**BOSS))
        assert first.status is RunStatus.PAUSED, first
        await operator.decide(h.store, h.legion.locks, first.approval_id, approve=True, by=OPERATOR)
        unmapped = port(url, agents={"tester": "agent:tester"})
        try:
            await h.restart(identity=unmapped).resume(first.run_id)
        except Exception as exc:
            ("raised", type(exc).__name__)
        await identity.aclose()
        await unmapped.aclose()
    events = await h.events(first.run_id)
    assert not [e for e in events if e.type is E.TOOL_COMPLETED and e.agent_id == "helper"]


async def test_resume_existing_child_after_child_killed() -> None:
    nia = FakeNia(agents={"agent:tester": "active", "agent:helper": "active"})
    rules = [Rule(decision=Verdict.REQUIRE_APPROVAL, tool="read_file")]
    with serve(nia) as url:
        h, identity = delegation(url, nia.agents, rules=rules, by_turn=True)
        first = await h.run(agent(**BOSS))
        assert first.status is RunStatus.PAUSED, first
        await operator.decide(h.store, h.legion.locks, first.approval_id, approve=True, by=OPERATOR)
        nia.kill("agent:helper")
        await h.restart().resume(first.run_id)
        await identity.aclose()
    events = await h.events(first.run_id)
    assert not [e for e in events if e.type is E.TOOL_COMPLETED and e.agent_id == "helper"]


async def test_no_identity_configured_still_runs_and_records_external_null() -> None:
    h = build([READ, reply("ok")])
    outcome = await h.run(agent())
    assert outcome.status is RunStatus.COMPLETED
    [authorized] = await h.payloads(outcome.run_id, E.ACTION_AUTHORIZED)
    # new key in every action.authorized, even without NIA
    assert "external" in authorized and authorized["external"] is None
    assert authorized["reasons"][-1] == "local: no external authority"
