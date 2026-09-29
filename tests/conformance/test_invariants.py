# Properties that must keep holding as the kernel changes: attenuation never widens, a
# pre-checked budget never overspends, editing an event breaks the chain, and nothing runs
# without an authorization the grant covers.

from decimal import Decimal
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from legion.authority.ledger import Ledger
from legion.domain.budget import BudgetLimits, Dimension
from legion.domain.capability import Capability
from legion.domain.errors import BudgetExceeded
from legion.domain.grant import AttenuationError, Grant
from legion.domain.principal import IdentityContext
from legion.domain.states import is_terminal_run
from legion.events.projections import RunState
from legion.events.store import verify_bodies
from legion.events.types import EventType
from legion.models.scripted import calls, reply
from tests.support import PRINCIPAL, Files, build

NAMES = ["files.read", "files.write", "net.http", "files.*"]
CONCRETE = ["files.read", "files.write", "net.http", "files.delete"]
RESOURCES = [None, "**", "a/**", "a/b/**", "a/*", "a/x", "a/b/c", "b/**", "a?x"]
TARGETS = [None, "a/x", "a/b/c", "a/b", "b/y", "c", "a/../b/y"]

caps = st.builds(Capability, name=st.sampled_from(NAMES), resource=st.sampled_from(RESOURCES))
requirements = st.builds(
    Capability, name=st.sampled_from(CONCRETE), resource=st.sampled_from(TARGETS)
)
IDENTITY = IdentityContext(principal=PRINCIPAL, agent_ref="a")


def grant_of(capabilities: frozenset[Capability]) -> Grant:
    return Grant(
        id="p", capabilities=capabilities, budget=BudgetLimits(), identity=IDENTITY, issuer="t"
    )


@given(
    parent=st.frozensets(caps, max_size=4),
    child=st.frozensets(caps, max_size=4),
    probes=st.lists(requirements, min_size=1, max_size=10),
)
def test_attenuation_never_widens(
    parent: frozenset[Capability], child: frozenset[Capability], probes: list[Capability]
) -> None:
    try:
        derived = grant_of(parent).attenuate(
            id="c", capabilities=child, budget=BudgetLimits(), identity=IDENTITY, issuer="t"
        )
    except AttenuationError:
        return
    for probe in probes:
        if derived.covers(probe):
            assert grant_of(parent).covers(probe), (probe, derived.capabilities)


limits = st.one_of(st.none(), st.integers(min_value=0, max_value=50))


@given(parent=st.tuples(limits, limits), child=st.tuples(limits, limits))
def test_budget_attenuation_never_widens(
    parent: tuple[int | None, int | None], child: tuple[int | None, int | None]
) -> None:
    p = BudgetLimits(steps=parent[0], tokens=parent[1])
    c = BudgetLimits(steps=child[0], tokens=child[1])
    if c.is_within(p):
        for mine, theirs in zip(child, parent, strict=True):
            assert theirs is None or (mine is not None and mine <= theirs)


@given(
    limit=st.integers(min_value=0, max_value=20),
    amounts=st.lists(st.integers(min_value=1, max_value=5), max_size=30),
)
def test_prechecked_budget_never_overspends(limit: int, amounts: list[int]) -> None:
    grant = Grant(
        id="g",
        capabilities=frozenset(),
        budget=BudgetLimits(tool_calls=limit),
        identity=IDENTITY,
        issuer="t",
    )
    state = RunState("r")
    ledger = Ledger(grant, state)
    for amount in amounts:
        try:
            ledger.precheck(Dimension.TOOL_CALLS, amount)
        except BudgetExceeded:
            continue
        record, over = ledger.charge(Dimension.TOOL_CALLS, amount)
        assert not over
        state.consumed[("g", "tool_calls")] = record.total
    assert state.used("g", "tool_calls") <= Decimal(limit)


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.data())
async def test_edits_break_chain(data: st.DataObject) -> None:
    h = build([calls([("read_file", {"path": "docs/a.md"})]), reply("done")])
    outcome = await h.run()
    events = await h.events(outcome.run_id)
    bodies = [(e.body(), e.hash) for e in events]
    assert verify_bodies(bodies).ok
    index = data.draw(st.integers(min_value=0, max_value=len(bodies) - 1))
    body, digest = bodies[index]
    key = data.draw(st.sampled_from(sorted(body)))
    forged = {**body, key: _perturb(body[key])}
    tampered = [*bodies[:index], (forged, digest), *bodies[index + 1 :]]
    assert not verify_bodies(tampered).ok


def _perturb(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "x"
    if isinstance(value, dict):
        return {**value, "forged": True}
    return "forged"


tool_calls = st.lists(
    st.tuples(
        st.sampled_from(["read_file", "write_file", "missing_tool"]),
        st.fixed_dictionaries(
            {
                "path": st.sampled_from(
                    [
                        "docs/a.md",
                        "secret/b.md",
                        "out/x",
                        "docs/../out/y",
                        "out/../secret/z",
                        "/etc/passwd",
                    ]
                )
            },
            optional={"text": st.just("t")},
        ),
    ),
    min_size=1,
    max_size=3,
)


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(script=st.lists(tool_calls, min_size=1, max_size=6))
async def test_nothing_runs_unauthorized(
    script: list[list[tuple[str, dict[str, str]]]],
) -> None:
    files = Files({"docs/a.md": "alpha", "secret/b.md": "beta"})
    turns = [
        calls(turn, ids=[f"t{i}_{j}" for j in range(len(turn))]) for i, turn in enumerate(script)
    ]
    h = build([*turns, reply("done")], files=files)
    outcome = await h.run()
    events = await h.events(outcome.run_id)

    state = RunState.from_events(outcome.run_id, events)
    assert is_terminal_run(state.status)
    assert (await h.store.verify(outcome.run_id)).ok

    grant = Grant.model_validate(state.grant)
    proposed: dict[str, dict[str, Any]] = {}
    authorized: set[str] = set()
    for event in events:
        if event.type is EventType.ACTION_PROPOSED:
            proposed[event.payload["action_hash"]] = event.payload
        elif event.type is EventType.ACTION_AUTHORIZED:
            action_hash = event.payload["action_hash"]
            required = [Capability.parse(c) for c in proposed[action_hash]["required"]]
            assert all(grant.covers(c) for c in required)
            authorized.add(action_hash)
        elif event.type is EventType.TOOL_STARTED:
            assert event.payload["action_hash"] in authorized

    assert all(path.startswith("out/") and ".." not in path for path in files.writes)
    assert files.content["secret/b.md"] == "beta"
