# Legion's own call id: derived from the id Legion gave the model.responded event that recorded
# the call, never from what the model or provider called it. It binds credentials and approvals;
# the provider's id stays in the transcript as provenance. No NIA here: this is generic.

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from legion.authority.policy import Rule, Verdict
from legion.domain.action import EffectClass
from legion.events.projections import RunState, legion_call_id
from legion.events.store import GENESIS_HASH, seal
from legion.events.types import EventDraft, EventType
from legion.kernel import operator
from legion.models.scripted import call, calls, reply
from legion.tools.base import ToolContext
from legion.tools.native import tool
from tests.credential_lab import effect_tool
from tests.support import OPERATOR, SimulatedCrash, agent, crash_at
from tests.unit.test_credentials import READ_A, setup, spec

E = EventType


def proposed(events: list[Any]) -> list[dict[str, Any]]:
    return [e.payload for e in events if e.type is E.ACTION_PROPOSED]


async def test_two_identical_actions_have_one_hash_and_two_call_ids() -> None:
    same = {"repo": "repo-A"}
    h, lab, _ = setup([calls([("read_repo", same), ("read_repo", same)]), reply("done")])
    out = await h.run(spec())
    [a, b] = proposed(await h.events(out.run_id))
    assert a["action_hash"] == b["action_hash"]
    assert a["legion_call_id"] != b["legion_call_id"]
    assert [r.call_id for r in lab.issued] == [a["legion_call_id"], b["legion_call_id"]]


async def test_same_call_keeps_its_id_across_retries() -> None:
    seen: list[str] = []
    h, lab, _ = setup([call("touch_repo", {"repo": "repo-A"}), reply("done")])
    h.legion.tools.register(effect_tool(EffectClass.READ, seen, fail_first=True))
    out = await h.run(agent(tools=["touch_repo"], capabilities=["repo.read:repo-A"]))
    started = [e.payload for e in await h.events(out.run_id) if e.type is E.TOOL_STARTED]
    assert len(started) == 2 and len({s["legion_call_id"] for s in started}) == 1
    assert {r.call_id for r in lab.issued} == {started[0]["legion_call_id"]}


async def test_same_call_keeps_its_id_across_pause_and_resume() -> None:
    rules = [Rule(decision=Verdict.REQUIRE_APPROVAL, tool="read_repo")]
    h, lab, _ = setup([READ_A, reply("done")], rules=rules, by_turn=True)
    first = await h.run(spec())
    await operator.decide(h.store, h.legion.locks, first.approval_id, approve=True, by=OPERATOR)
    await h.resume(first.run_id)
    events = await h.events(first.run_id)
    [asked] = [e.payload for e in events if e.type is E.APPROVAL_REQUESTED]
    ids = {p["legion_call_id"] for p in proposed(events)}
    assert ids == {asked["legion_call_id"]} == {r.call_id for r in lab.issued}


async def test_same_call_keeps_its_id_across_a_crash() -> None:
    h, lab, _ = setup([READ_A, reply("done")], faults=crash_at("credential:obtained"), by_turn=True)
    with pytest.raises(SimulatedCrash):
        await h.run(spec())
    [run_id] = list(h.store._events)  # type: ignore[attr-defined]
    await h.restart().resume(run_id)
    assert len(lab.issued) == 2 and lab.issued[0].call_id == lab.issued[1].call_id


async def test_provider_ids_that_collide_across_turns_still_get_distinct_ids() -> None:
    h, _, _ = setup(
        [
            call("read_repo", {"repo": "repo-A"}, id="same"),
            call("read_repo", {"repo": "repo-A"}, id="same"),
            reply("done"),
        ]
    )
    out = await h.run(spec())
    ps = proposed(await h.events(out.run_id))
    assert len({p["call_id"] for p in ps}) == 2  # the loop renamed the second
    assert len({p["legion_call_id"] for p in ps}) == 2


async def test_model_cannot_name_the_call_id() -> None:
    forged = "lc-" + "0" * 32
    h, lab, _ = setup([call("read_repo", {"repo": "repo-A"}, id=forged), reply("done")])
    out = await h.run(spec())
    [p] = proposed(await h.events(out.run_id))
    assert p["call_id"] == forged and p["legion_call_id"] != forged
    assert lab.issued[0].call_id == p["legion_call_id"]


class RepoArgs(BaseModel):
    repo: str


SEEN_BY_TOOL: list[tuple[str, str]] = []


@tool(effect=EffectClass.READ, capabilities=["repo.read"], resource=lambda a: a.repo)
def look(args: RepoArgs, ctx: ToolContext) -> str:
    """Look."""
    SEEN_BY_TOOL.append((ctx.call_id, ctx.legion_call_id))
    return "ok"


async def test_tools_see_legions_call_id() -> None:
    SEEN_BY_TOOL.clear()
    h, _, _ = setup([call("look", {"repo": "repo-A"}, id="model-id"), reply("done")])
    h.legion.tools.register(look)
    out = await h.run(agent(tools=["look"], capabilities=["repo.read:repo-A"]))
    [p] = proposed(await h.events(out.run_id))
    assert [("model-id", p["legion_call_id"])] == SEEN_BY_TOOL


async def test_log_is_the_source_of_the_id() -> None:
    h, _, _ = setup([READ_A, reply("done")])
    out = await h.run(spec())
    events = await h.events(out.run_id)
    [responded] = [
        e
        for e in events
        if e.type is E.MODEL_RESPONDED and e.payload["message"]["parts"][0]["type"] == "tool_call"
    ]
    [p] = proposed(events)
    assert p["legion_call_id"] == legion_call_id(responded.event_id, 0)
    # rebuilding the state from the log gives the same ids, with or without the field recorded
    state = RunState.from_events(out.run_id, events)
    assert state.tasks[responded.task_id].legion_calls[p["call_id"]] == p["legion_call_id"]


async def test_events_from_before_legion_call_ids_still_replay() -> None:
    # A run recorded before this field existed: the same log, re-sealed without it, replays
    # and derives the same ids.
    h, _, _ = setup([READ_A, reply("done")])
    out = await h.run(spec())
    events = await h.events(out.run_id)
    old = []
    prev = GENESIS_HASH
    for e in events:
        payload = {k: v for k, v in e.payload.items() if k != "legion_call_id"}
        body = e.model_dump(exclude={"seq", "prev_hash", "hash"})
        body["payload"] = payload
        sealed = seal(EventDraft.model_validate(body), e.seq, prev)
        old.append(sealed)
        prev = sealed.hash
    state = RunState.from_events(out.run_id, old)
    [p] = proposed(events)
    task = next(iter(state.tasks.values()))
    assert task.legion_calls[p["call_id"]] == p["legion_call_id"]


async def test_forged_legion_call_id_breaks_replay() -> None:
    h, _, _ = setup([READ_A, reply("done")])
    out = await h.run(spec())
    events = await h.events(out.run_id)
    forged = []
    prev = GENESIS_HASH
    for e in events:
        body = e.model_dump(exclude={"seq", "prev_hash", "hash"})
        if e.type is E.ACTION_PROPOSED:
            body["payload"] = {**e.payload, "legion_call_id": "lc-" + "f" * 32}
        sealed = seal(EventDraft.model_validate(body), e.seq, prev)
        forged.append(sealed)
        prev = sealed.hash
    with pytest.raises(ValueError, match="wrong Legion call"):
        RunState.from_events(out.run_id, forged)
