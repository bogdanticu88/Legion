# How much assurance a call's credential needs, how long a credential lasts, and when a retry may
# reuse one. A requirement is never lowered because it couldn't be met, and a credential that
# expires or is withdrawn doesn't quietly keep working.

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from legion.access.secrets import EnvResolver, SecretRef
from legion.authority.policy import Rule, Verdict
from legion.domain.states import RunStatus
from legion.events.types import EventType
from legion.kernel import operator
from legion.kernel.credentials import CredentialMapping, assess, derive_request
from legion.models.scripted import call, reply
from legion.ports.credentials import Assurance
from legion.ports.identity import AgentIdentity, KillState, NullIdentityPort
from tests.credential_lab import LabAuthority, repo_tools
from tests.support import OPERATOR, SimulatedCrash, agent, build, crash_at
from tests.unit.test_credentials import _action, _grants

E = EventType
MAPPING = CredentialMapping(
    authority="lab",
    provider="github",
    permissions={"repo.read": ("contents:read",), "repo.issue.create": ("issues:write",)},
)
READ_A = call("read_repo", {"repo": "repo-A"})


class Clock:
    def __init__(self) -> None:
        self.t = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def after_issue(clock: Clock, seconds: float, times: int) -> Any:
    # moves the clock right after a credential is recorded, before the tool starts
    count = [0]

    def hook(point: str) -> None:
        if point == "after:credential.resolved" and count[0] < times:
            count[0] += 1
            clock.advance(seconds)

    return hook


def setup(
    steps: list[Any],
    lab: LabAuthority | None = None,
    *,
    mapping: CredentialMapping | None = MAPPING,
    trusted: tuple[str, ...] = ("lab",),
    minimum: Assurance = Assurance.UNVERIFIED,
    clock: Clock | None = None,
    tools: dict[str, Any] | None = None,
    **kw: Any,
) -> tuple[Any, LabAuthority, list[tuple[str, str]]]:
    clock = clock or Clock()
    lab = lab or LabAuthority()
    lab.clock = clock
    seen: list[tuple[str, str]] = []
    options: dict[str, Any] = {
        "credential_mappings": {"github": mapping} if mapping else {},
        "credential_authorities": {"lab": lab},
        "trusted_authorities": trusted,
        "credential_minimum": minimum,
        "credential_timeout_s": 0.2,
    }
    h = build(
        steps,
        extra_tools=repo_tools(seen, **(tools or {})),
        grantable=("repo.*", "files.read:**"),
        options=options,
        now=clock,
        **kw,
    )
    return h, lab, seen


def spec(*caps: str) -> Any:
    caps = caps or ("repo.read:repo-A",)
    names = {"repo.read": "read_repo", "repo.issue.create": "create_issue"}
    tools = [tool for cap, tool in names.items() if any(c.startswith(cap + ":") for c in caps)]
    return agent(tools=tools, capabilities=list(caps))


async def events(h: Any, run_id: str, kind: EventType) -> list[dict[str, Any]]:
    return await h.payloads(run_id, kind)


STATIC = {"credential_bindings": {"github": SecretRef.parse("env:GH")}}
STATIC_ENV = EnvResolver({"GH": "static-token-abcdef123"})


# Step 11: static credentials are exactly what they are


@pytest.mark.parametrize(
    ("minimum", "runs"),
    [
        (Assurance.UNVERIFIED, True),
        (Assurance.DECLARED, False),
        (Assurance.VERIFIED, False),
        (Assurance.BOUND, False),
    ],
)
async def test_static_credential_runs_only_where_unverified_is_allowed(
    minimum: Assurance, runs: bool
) -> None:
    h, _, seen = setup(
        [READ_A, reply("ok")], mapping=None, minimum=minimum, credentials=STATIC_ENV, **STATIC
    )
    outcome = await h.run(spec())
    assert bool(seen) is runs
    if runs:
        [record] = await events(h, outcome.run_id, E.CREDENTIAL_RESOLVED)
        assert record["assurance"] == "unverified" and record["authority"] == "static"
    else:
        [record] = await events(h, outcome.run_id, E.CREDENTIAL_REFUSED)
        assert record["assurance"] == "unverified" and record["required"] == minimum.value


def test_absence_of_evidence_is_never_raised() -> None:

    grant = _grants()[0]
    request = derive_request(MAPPING, _action(grant, "t"), grant, run_id="r", call_id="c")
    assert not isinstance(request, str)
    now = datetime.now(UTC)
    for trusted in (True, False):
        for raw in (None, {}, {"verified": True}, "verified", {"scope": "admin", "trust_me": True}):
            result = assess(request, raw, trusted=trusted, now=now)
            assert result.assurance is Assurance.UNVERIFIED, raw


async def test_valid_looking_evidence_from_an_untrusted_authority_stays_declared() -> None:
    # well formed, exactly what was asked for, says verified: still only declared
    h, _, seen = setup([READ_A, reply("ok")], trusted=(), minimum=Assurance.VERIFIED)
    outcome = await h.run(spec())
    assert seen == []
    [record] = await events(h, outcome.run_id, E.CREDENTIAL_REFUSED)
    assert record["assurance"] == "declared" and record["required"] == "bound"


# Step 12: the requirement comes from configuration and policy, and only goes up


async def test_policy_rule_can_raise_the_requirement() -> None:
    rules = [Rule(decision=Verdict.ALLOW, tool="read_repo", credential_assurance=Assurance.BOUND)]
    lenient = MAPPING.model_copy(update={"minimum": Assurance.UNVERIFIED})
    # untrusted authority: declared, below what the rule asks
    h, _, seen = setup([READ_A, reply("ok")], trusted=(), mapping=lenient, rules=rules)
    outcome = await h.run(spec())
    assert seen == []
    [record] = await events(h, outcome.run_id, E.CREDENTIAL_REFUSED)
    assert record["required"] == "bound"
    # trusted and bound: fine
    h, _, seen = setup([READ_A, reply("ok")], mapping=lenient, rules=rules)
    await h.run(spec())
    assert len(seen) == 1


async def test_policy_rule_cannot_lower_the_requirement() -> None:
    rules = [
        Rule(decision=Verdict.ALLOW, tool="read_repo", credential_assurance=Assurance.UNVERIFIED)
    ]
    unbound = LabAuthority(mods={"action_hash": None, "call_id": None, "grant_fingerprint": None})
    h, _, seen = setup([READ_A, reply("ok")], unbound, rules=rules)
    outcome = await h.run(spec())
    assert seen == []
    [record] = await events(h, outcome.run_id, E.CREDENTIAL_REFUSED)
    assert record["assurance"] == "verified" and record["required"] == "bound"


async def test_policy_rule_applies_to_static_credentials() -> None:
    rules = [
        Rule(decision=Verdict.ALLOW, tool="read_repo", credential_assurance=Assurance.VERIFIED)
    ]
    h, _, seen = setup(
        [READ_A, reply("ok")], mapping=None, rules=rules, credentials=STATIC_ENV, **STATIC
    )
    outcome = await h.run(spec())
    assert seen == []
    [record] = await events(h, outcome.run_id, E.CREDENTIAL_REFUSED)
    assert record["required"] == "verified"


@pytest.mark.parametrize(
    ("lab", "required"),
    [
        (LabAuthority(fail="crash"), Assurance.VERIFIED),
        (LabAuthority(fail="timeout"), Assurance.VERIFIED),
        (LabAuthority(evidence="missing"), Assurance.VERIFIED),
        (LabAuthority(mods={"permissions": ["contents:read", "admin"]}), Assurance.VERIFIED),
    ],
    ids=["unavailable", "timeout", "no-evidence", "broader"],
)
async def test_unmet_requirement_refuses_and_never_falls_back(
    lab: LabAuthority, required: Assurance, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a static secret for the same service is sitting in the environment; it must not be used
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-token-should-not-appear")
    mapping = MAPPING.model_copy(update={"minimum": required})
    h, _, seen = setup([READ_A, reply("ok")], lab, mapping=mapping)
    outcome = await h.run(spec())
    assert seen == []
    assert await events(h, outcome.run_id, E.CREDENTIAL_RESOLVED) == []
    assert [r["reason_code"] for r in await events(h, outcome.run_id, E.ACTION_REFUSED)] == [
        "credential_refused"
    ]


async def test_mcp_server_credential_is_held_to_the_same_requirement() -> None:
    pytest.importorskip("mcp")
    from tests.mcp_lab import Lab, connect, pinned, server_config

    lab = Lab()
    config = await pinned(lab, server_config(read_note={"effect": "read", "resource_arg": "path"}))
    conn, found = await connect(lab, config)
    mcp_spec = agent(tools=["mcp_lab_read_note"], capabilities=["mcp.lab.read_note:notes/**"])
    steps = [call("mcp_lab_read_note", {"path": "notes/a.md"}), reply("ok")]
    for minimum, runs in ((Assurance.DECLARED, True), (Assurance.VERIFIED, False)):
        lab.effects.clear()
        h = build(
            steps,
            extra_tools=found.tools,
            grantable=("mcp.*",),
            options={"credential_minimum": minimum},
        )
        outcome = await h.run(mcp_spec)
        assert bool(lab.effects) is runs
        if not runs:
            [record] = await h.payloads(outcome.run_id, E.CREDENTIAL_REFUSED)
            assert record["assurance"] == "declared" and record["authority"] == "server"
    await conn.aclose()


# Step 13: lifetime


async def test_expiry_between_issue_and_start_gets_one_fresh_credential() -> None:
    clock = Clock()
    h, lab, seen = setup([READ_A, reply("ok")], clock=clock, faults=after_issue(clock, 120, 1))
    outcome = await h.run(spec())
    assert outcome.status is RunStatus.COMPLETED
    assert [
        r["credential_ref"] for r in await events(h, outcome.run_id, E.CREDENTIAL_RESOLVED)
    ] == [
        "cred-001",
        "cred-002",
    ]
    assert seen == [("read_repo", lab.secrets[1])]


async def test_credential_that_keeps_expiring_before_start_is_refused() -> None:
    clock = Clock()
    h, _, seen = setup([READ_A, reply("ok")], clock=clock, faults=after_issue(clock, 120, 10))
    outcome = await h.run(spec())
    assert seen == []
    [record] = await events(h, outcome.run_id, E.CREDENTIAL_REFUSED)
    assert "expired again before the call started" in record["problems"]


async def test_nothing_is_issued_while_waiting_for_approval() -> None:
    clock = Clock()
    rules = [Rule(decision=Verdict.REQUIRE_APPROVAL, tool="create_issue")]
    steps = [call("create_issue", {"repo": "repo-A", "title": "x"}), reply("ok")]
    h, lab, seen = setup(steps, clock=clock, rules=rules, by_turn=True)
    first = await h.run(spec("repo.issue.create:repo-A"))
    assert first.status is RunStatus.PAUSED and lab.issued == []
    clock.advance(1800)  # far longer than a credential lives, within the approval's hour
    await operator.decide(
        h.store, h.legion.locks, first.approval_id, approve=True, by=OPERATOR, now=clock
    )
    await h.restart().resume(first.run_id)
    [record] = await events(h, first.run_id, E.CREDENTIAL_RESOLVED)
    assert datetime.fromisoformat(record["expires_at"]) > clock()
    assert len(seen) == 1


async def test_crash_after_issue_then_a_long_wait_gets_a_fresh_credential() -> None:
    clock = Clock()
    h, lab, seen = setup(
        [READ_A, reply("ok")],
        clock=clock,
        by_turn=True,
        faults=crash_at("after:credential.resolved"),
    )
    with pytest.raises(SimulatedCrash):
        await h.run(spec())
    clock.advance(86400)
    [summary] = await h.store.runs()
    outcome = await h.restart(faults=None).resume(summary.run_id)
    assert outcome.status is RunStatus.COMPLETED
    assert seen == [("read_repo", lab.secrets[1])]


async def test_write_that_may_have_been_sent_is_not_repeated_with_a_new_credential() -> None:
    steps = [call("create_issue", {"repo": "repo-A", "title": "x"}, id="w1"), reply("ok")]
    h, lab, seen = setup(steps, by_turn=True, faults=crash_at("tool:after_invoke"))
    with pytest.raises(SimulatedCrash):
        await h.run(spec("repo.issue.create:repo-A"))
    [summary] = await h.store.runs()
    outcome = await h.restart(faults=None).resume(summary.run_id)
    assert outcome.status is RunStatus.PAUSED and outcome.blocked_call == "w1"
    assert len(lab.issued) == 1 and len(seen) == 1


async def test_expired_before_a_retry_gets_a_fresh_credential() -> None:
    clock = Clock()
    tools = {"fail_first": True, "on_call": lambda n: clock.advance(120) if n == 1 else None}
    h, lab, seen = setup([READ_A, reply("ok")], clock=clock, tools=tools)
    outcome = await h.run(spec())
    assert outcome.status is RunStatus.COMPLETED
    assert [s for _, s in seen] == lab.secrets[:2] and len(set(lab.secrets[:2])) == 2


async def test_revoked_before_a_retry_is_refused_not_replaced() -> None:
    lab = LabAuthority()
    tools = {"fail_first": True, "on_call": lambda n: lab.revoked.add("cred-001")}
    h, lab, seen = setup([READ_A, reply("ok")], lab, tools=tools)
    outcome = await h.run(spec())
    assert len(seen) == 1 and len(lab.issued) == 1
    [record] = await events(h, outcome.run_id, E.CREDENTIAL_REFUSED)
    assert "no longer active at the authority" in record["problems"]


async def test_authority_that_cant_answer_before_a_retry_stops_it() -> None:
    lab = LabAuthority()
    tools = {"fail_first": True, "on_call": lambda n: setattr(lab, "fail_active", True)}
    h, lab, seen = setup([READ_A, reply("ok")], lab, tools=tools)
    outcome = await h.run(spec())
    assert len(seen) == 1
    [record] = await events(h, outcome.run_id, E.CREDENTIAL_REFUSED)
    assert any("ConnectionError" in p for p in record["problems"])


# Step 14: a retry reuses the call's credential only while it still holds


async def test_retry_reuses_a_credential_that_is_still_good() -> None:
    h, lab, seen = setup([READ_A, reply("ok")], tools={"fail_first": True})
    outcome = await h.run(spec())
    assert outcome.status is RunStatus.COMPLETED
    assert len(lab.issued) == 1
    assert seen == [("read_repo", lab.secrets[0])] * 2


class KillOnFlag(NullIdentityPort):
    def __init__(self) -> None:
        self.dead = False

    async def kill_state(self, identity: AgentIdentity) -> KillState:
        return KillState.KILLED if self.dead else KillState.ACTIVE


async def test_retry_after_the_agent_is_killed_gets_nothing() -> None:
    identity = KillOnFlag()
    tools = {"fail_first": True, "on_call": lambda n: setattr(identity, "dead", True)}
    h, lab, seen = setup([READ_A, reply("ok")], tools=tools, identity=identity)
    outcome = await h.run(spec())
    assert outcome.status is RunStatus.FAILED
    assert len(seen) == 1 and len(lab.issued) == 1
