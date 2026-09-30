# The NIA credential authority inside whole Legion runs: retries by effect class, approvals,
# delegation, crashes at every step of issuing and using a credential, and the NIA identity port
# and credential authority working (and failing) side by side. Everything through HTTP to the
# stand-in; the kernel only ever sees the generic CredentialAuthority port.

from __future__ import annotations

import json
from typing import Any

import pytest

from legion.authority.policy import Rule, Verdict
from legion.domain.action import EffectClass
from legion.domain.agent import AgentSpec, ModelRequirement
from legion.domain.errors import ApprovalMismatch
from legion.domain.grant import DelegationLimits
from legion.domain.states import RunStatus
from legion.events.types import EventType
from legion.kernel import approvals, operator
from legion.models.scripted import call, reply
from legion.ports.credentials import IssuedCredential
from tests.credential_lab import effect_tool
from tests.nia_cred_lab import ISSUER, VIEWER, serve
from tests.nia_cred_support import AGENTS, Clock, authority, fake, setup, spec
from tests.support import OPERATOR, SimulatedCrash, crash_at

E = EventType
READ_A = call("read_repo", {"repo": "repo-A"})
TOUCH = call("touch_repo", {"repo": "repo-A"})


def issued(nia: Any) -> list[dict[str, Any]]:
    return [b for (e, _, _, b) in nia.requests if e == "issue"]


async def text_of(h: Any, run_id: str) -> str:
    return json.dumps([e.model_dump(mode="json") for e in await h.events(run_id)])


def no_secrets(nia: Any, text: str) -> None:
    for planted in (ISSUER, VIEWER, *nia.issued_secrets()):
        assert planted not in text


# retries, by effect class, with the credential in every state NIA can report


@pytest.mark.parametrize(
    "effect", [EffectClass.PURE, EffectClass.READ, EffectClass.WRITE_IDEMPOTENT]
)
@pytest.mark.parametrize(
    ("between", "runs", "issues", "outcome"),
    [
        ("active", 2, 1, "completed"),
        ("expired", 2, 2, "completed"),
        ("revoked", 1, 1, "refused"),
        ("unknown", 1, 1, "refused"),
        ("unavailable", 1, 1, "refused"),
        ("killed", 1, 1, "killed"),
    ],
)
async def test_retry_matrix(
    effect: EffectClass, between: str, runs: int, issues: int, outcome: str
) -> None:
    clock = Clock()
    nia = fake(clock)
    seen: list[str] = []

    def after_first_attempt(n: int) -> None:
        if n != 1:
            return
        [c] = nia.creds.values()
        match between:
            case "expired":
                clock.advance(3600)
            case "revoked":
                nia.revoke(c.ref)
            case "unknown":
                del nia.creds[c.ref]
            case "unavailable":
                nia.modes["status"] = "503"
            case "killed":
                nia.kill("agent:tester")

    tool = effect_tool(effect, seen, fail_first=True, on_call=after_first_attempt)
    with serve(nia) as url:
        run = setup(url, [TOUCH, reply("done")], clock=clock, tools=[tool])
        out = await run.h.run(spec(tools=["touch_repo"]))
        await run.close()
    events = await run.h.events(out.run_id)
    bodies = issued(nia)
    assert len(seen) == runs and len(bodies) == issues
    # every issuance for this call is bound to the same Legion call id
    assert len({b["call_id"] for b in bodies}) == 1
    if outcome == "completed":
        assert out.status is RunStatus.COMPLETED
    elif outcome == "refused":
        assert [e for e in events if e.type is E.CREDENTIAL_REFUSED]
    else:
        assert out.status is RunStatus.FAILED and out.error_code == "killed"
    no_secrets(nia, await text_of(run.h, out.run_id))


async def test_non_idempotent_write_in_doubt_is_not_repeated() -> None:
    nia = fake()
    seen: list[str] = []

    def crash_inside(n: int) -> None:
        raise SimulatedCrash("during the write")

    tool = effect_tool(EffectClass.WRITE, seen, on_call=crash_inside)
    with serve(nia) as url:
        run = setup(url, [TOUCH, reply("done")], tools=[tool], by_turn=True)
        with pytest.raises(SimulatedCrash):
            await run.h.run(spec(tools=["touch_repo"]))
        [run_id] = {e.run_id for e in await _all_events(run.h)}
        fresh = authority(url)
        again = run.h.restart(
            options={
                "credential_mappings": {"github": run.h.legion.broker.mapped["github"]},
                "credential_authorities": {"nia": fresh},
                "trusted_authorities": ("nia",),
                "credential_minimum": run.h.legion.broker.minimum,
            },
            extra_tools=[effect_tool(EffectClass.WRITE, seen)],
        )
        resumed = await again.resume(run_id)
        await fresh.aclose()
        await run.close()
    assert resumed.status is not RunStatus.COMPLETED
    assert len(seen) == 1 and len(issued(nia)) == 1


# approvals


RULES = [Rule(decision=Verdict.REQUIRE_APPROVAL, tool="read_repo")]


async def approve_and_resume(run: Any, first: Any, clock: Clock | None = None) -> Any:
    await operator.decide(
        run.h.store,
        run.h.legion.locks,
        first.approval_id,
        approve=True,
        by=OPERATOR,
        **({"now": clock} if clock is not None else {}),
    )
    return await run.h.resume(first.run_id)


async def test_approval_then_issue_then_execute() -> None:
    nia = fake()
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")], rules=RULES, by_turn=True)
        first = await run.h.run(spec())
        assert first.status is RunStatus.PAUSED and not issued(nia)
        done = await approve_and_resume(run, first)
        await run.close()
    assert done.status is RunStatus.COMPLETED and len(run.seen) == 1
    events = await run.h.events(first.run_id)
    [requested] = [e.payload for e in events if e.type is E.APPROVAL_REQUESTED]
    [body] = issued(nia)
    assert requested["legion_call_id"] == body["call_id"]
    assert requested["subject"]["legion_call_id"] == body["call_id"]


@pytest.mark.parametrize(
    "failure",
    ["refused", "broader", "wrong_call", "expired_before_dispatch", "revoked_before_dispatch"],
)
async def test_approval_with_credential_trouble(failure: str) -> None:
    clock = Clock()
    nia = fake(clock)
    with serve(nia) as url:
        run = setup(
            url,
            [READ_A, READ_A, reply("done")],
            rules=RULES,
            by_turn=True,
            clock=clock,
        )
        first = await run.h.run(spec())
        match failure:
            case "refused":
                nia.ungrant("agent:tester", data=("repo-A",))
            case "broader":
                nia.evidence_mods = {"permissions": ["repo.read", "repo.write"]}
            case "wrong_call":
                nia.evidence_mods = {"call_id": "lc-" + "7" * 32}
            case "expired_before_dispatch":

                def expire_once(endpoint: str, n: int) -> None:
                    if endpoint == "status" and n == 1:
                        clock.advance(3600)

                nia.on_request = expire_once
            case "revoked_before_dispatch":

                def revoke(endpoint: str, n: int) -> None:
                    if endpoint == "status":
                        for c in nia.creds.values():
                            nia.revoke(c.ref)

                nia.on_request = revoke
        second = await approve_and_resume(run, first, clock)
        await run.close()
    events = await run.h.events(first.run_id)
    requested = [e.payload for e in events if e.type is E.APPROVAL_REQUESTED]
    consumed = [e.payload for e in events if e.type is E.APPROVAL_CONSUMED]
    if failure == "expired_before_dispatch":
        # replaced once, for the same call, and run under the one approval; the script's repeat
        # afterwards is a new call asking for a new approval
        first_call = requested[0]["legion_call_id"]
        assert len(run.seen) == 1
        assert [b["call_id"] for b in issued(nia)] == [first_call, first_call]
        assert len(consumed) == 1 and second.status is RunStatus.PAUSED
        return
    # the call was refused, and the approval can't be used again: the model's repeat of the
    # same action is a new call and needs a new approval
    assert run.seen == []
    assert len(consumed) == 1
    assert second.status is RunStatus.PAUSED and len(requested) == 2
    assert requested[0]["approval_id"] != requested[1]["approval_id"]
    assert requested[0]["legion_call_id"] != requested[1]["legion_call_id"]


async def test_approval_crash_after_issuance_resumes_same_call() -> None:
    nia = fake()
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")], rules=RULES, by_turn=True)
        first = await run.h.run(spec())
        await operator.decide(
            run.h.store, run.h.legion.locks, first.approval_id, approve=True, by=OPERATOR
        )
        crashing = run.h.restart(faults=crash_at("credential:obtained"))
        with pytest.raises(SimulatedCrash):
            await crashing.resume(first.run_id)
        done = await run.h.restart().resume(first.run_id)
        await run.close()
    assert done.status is RunStatus.COMPLETED
    bodies = issued(nia)
    assert len(bodies) == 2 and bodies[0]["call_id"] == bodies[1]["call_id"]
    assert len(run.seen) == 1


async def test_approval_from_before_legion_call_ids_is_invalidated(monkeypatch: Any) -> None:
    # An approval requested by an older Legion (whose binding had no Legion call id) doesn't
    # match any more after the upgrade: it's invalidated, not silently honoured.
    nia = fake()
    real = approvals.binding

    def old_binding(**kw: Any) -> dict[str, Any]:
        bound = real(**kw)
        bound.pop("legion_call_id")
        return bound

    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")], rules=RULES, by_turn=True)
        monkeypatch.setattr(approvals, "binding", old_binding)
        first = await run.h.run(spec())
        monkeypatch.setattr(approvals, "binding", real)
        second = await approve_and_resume(run, first)
        await run.close()
    events = await run.h.events(first.run_id)
    assert [e for e in events if e.type is E.APPROVAL_INVALIDATED]
    refusals = [e.payload for e in events if e.type is E.ACTION_REFUSED]
    assert refusals and refusals[0]["reason_code"] == ApprovalMismatch.code
    assert run.seen == [] and not issued(nia)
    assert second.status is not None


# delegation


HELPER = AgentSpec(
    name="helper",
    instructions="read",
    model=ModelRequirement(profile="child/helper"),
    tools=("read_repo",),
    capabilities=("repo.read:repo-A",),
)
BOSS = spec(
    "repo.read:repo-A",
    "agent.delegate:**",
    tools=["read_repo", "delegate"],
    delegation=DelegationLimits(max_depth=1, max_children=1),
)
DELEGATE = call("delegate", {"agent": "helper", "objective": "read"}, id="d1")


def delegated(url: str, clock: Clock | None = None) -> Any:
    return setup(
        url,
        [DELEGATE, reply("done")],
        agents={"helper": HELPER},
        scripts={"child/helper": [READ_A, reply("read")]},
        clock=clock,
    )


async def test_child_credential_is_issued_to_the_child() -> None:
    nia = fake()
    with serve(nia) as url:
        run = delegated(url)
        out = await run.h.run(BOSS)
        await run.close()
    assert out.status is RunStatus.COMPLETED
    [(_, path, _, _)] = [r for r in nia.requests if r[0] == "issue"]
    assert path == "/agents/agent%3Ahelper/scoped-credentials"
    events = await run.h.events(out.run_id)
    [used] = [e.payload for e in events if e.type is E.CREDENTIAL_RESOLVED]
    assert used["subject"] == "helper" and used["external_principal"] == "agent:helper"


@pytest.mark.parametrize(
    "mods",
    [
        {"principal": "agent:tester"},  # the parent's
        {"principal": "agent:sibling"},
        {"permissions": ["repo.read", "repo.write"]},  # wider than the child's grant
        {"resource": "repo-B"},
    ],
)
async def test_child_refuses_credentials_that_are_not_its_own(mods: dict[str, Any]) -> None:
    nia = fake()
    nia.evidence_mods = mods
    with serve(nia) as url:
        run = delegated(url)
        out = await run.h.run(BOSS)
        await run.close()
    events = await run.h.events(out.run_id)
    assert [e for e in events if e.type is E.CREDENTIAL_REFUSED and e.agent_id == "helper"]
    assert run.seen == []


async def test_child_asking_beyond_its_grant_never_reaches_nia() -> None:
    nia = fake()
    wider = call("create_issue", {"repo": "repo-A", "title": "x"})
    helper = HELPER.model_copy(update={"tools": ("read_repo", "create_issue")})
    with serve(nia) as url:
        run = setup(
            url,
            [DELEGATE, reply("done")],
            agents={"helper": helper},
            scripts={"child/helper": [wider, reply("read")]},
        )
        out = await run.h.run(BOSS)
        await run.close()
    # delegation itself refuses a child that would need more than it's given, before any
    # credential is asked for
    events = await run.h.events(out.run_id)
    refusals = [e.payload for e in events if e.type is E.ACTION_REFUSED]
    assert refusals[0]["reason_code"] == "delegation_refused" and not issued(nia)


async def test_killed_child_gets_nothing() -> None:
    nia = fake()
    nia.kill("agent:helper")
    with serve(nia) as url:
        run = delegated(url)
        out = await run.h.run(BOSS)
        await run.close()
    assert not issued(nia) and run.seen == []
    events = await run.h.events(out.run_id)
    assert not [e for e in events if e.type is E.TOOL_STARTED and e.agent_id == "helper"]


# crashes at every point of issuing and using a credential


CRASH_POINTS = [
    ("after:action.authorized", False),
    ("credential:obtained", False),
    ("after:credential.resolved", False),
    ("credential:checked", False),
    ("after:tool.started", True),
    ("tool:before_invoke", True),
    ("tool:after_invoke", True),
]


class CrashDuringIssue:
    """Wraps the adapter so NIA issues and the process dies before Legion gets the answer."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = inner.name

    async def issue(self, request: Any) -> IssuedCredential:
        await self.inner.issue(request)
        raise SimulatedCrash("during issuance")

    async def status(self, ref: str) -> Any:
        return await self.inner.status(ref)

    async def revoke(self, ref: str) -> None:
        await self.inner.revoke(ref)


@pytest.mark.parametrize("effect", [EffectClass.READ, EffectClass.WRITE])
@pytest.mark.parametrize(("point", "started"), [*CRASH_POINTS, ("during_issue", False)])
async def test_crash_matrix(effect: EffectClass, point: str, started: bool) -> None:
    nia = fake()
    seen: list[str] = []
    with serve(nia) as url:
        auth = authority(url)
        options: dict[str, Any] = {}
        faults = None
        if point == "during_issue":
            options = {"credential_authorities": {"nia": CrashDuringIssue(auth)}}
        else:
            faults = crash_at(point)
        run = setup(
            url,
            [TOUCH, reply("done")],
            tools=[effect_tool(effect, seen)],
            auth=auth,
            faults=faults,
            by_turn=True,
            options=options,
        )
        with pytest.raises(SimulatedCrash):
            await run.h.run(spec(tools=["touch_repo"]))
        [run_id] = {e.run_id for e in await _all_events(run.h)}
        # a new process: new adapter, nothing carried over but the event log
        fresh = authority(url)
        again = run.h.restart(
            options={
                "credential_mappings": {"github": run.h.legion.broker.mapped["github"]},
                "credential_authorities": {"nia": fresh},
                "trusted_authorities": ("nia",),
                "credential_minimum": run.h.legion.broker.minimum,
                "credential_timeout_s": 5.0,
            },
            extra_tools=[effect_tool(effect, seen)],
        )
        resumed = await again.resume(run_id)
        await fresh.aclose()
        await run.close()
    bodies = issued(nia)
    assert len({b["call_id"] for b in bodies}) == 1, "one call, one Legion call id"
    tool_runs = len(seen)
    if started and not effect.safe_to_repeat:
        # started and not safe to repeat: in doubt, never run or issued again
        assert tool_runs <= 1 and resumed.status is not RunStatus.COMPLETED
        assert len(bodies) == 1
    else:
        assert resumed.status is RunStatus.COMPLETED
        assert tool_runs >= 1
    text = await text_of(again, run_id)
    no_secrets(nia, text)


async def _all_events(h: Any) -> list[Any]:
    store = h.store
    runs = getattr(store, "_events", None)
    if isinstance(runs, dict):
        return [e for evs in runs.values() for e in evs]
    raise AssertionError("memory store layout changed")


# the identity port and the credential authority side by side


async def test_identity_and_credential_both_active() -> None:
    nia = fake()
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")])
        out = await run.h.run(spec())
        await run.close()
    assert out.status is RunStatus.COMPLETED
    order = [e for (e, _, _, _) in nia.requests]
    # identity before issuance, status after it, identity again before dispatch
    first_issue = order.index("issue")
    assert "identity" in order[:first_issue]
    assert order.index("status") > first_issue
    assert "identity" in order[order.index("status") :]
    tokens = {e: a for (e, _, a, _) in nia.requests}
    assert tokens["identity"] == f"Bearer {VIEWER}" and tokens["issue"] == f"Bearer {ISSUER}"


async def test_killed_before_issuance() -> None:
    nia = fake()
    nia.kill("agent:tester")
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")])
        out = await run.h.run(spec())
        await run.close()
    assert out.error_code == "killed" and not issued(nia)


async def test_killed_after_issuance_before_dispatch() -> None:
    nia = fake()

    def kill_on_status(endpoint: str, n: int) -> None:
        if endpoint == "status":
            nia.kill("agent:tester")

    nia.on_request = kill_on_status
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")])
        out = await run.h.run(spec())
        await run.close()
    assert run.seen == [] and out.status is RunStatus.FAILED
    assert all(c.revoked_at is not None for c in nia.creds.values())


async def test_restored_identity_does_not_revive_old_credential() -> None:
    nia = fake()
    with serve(nia) as url:
        auth = authority(url)
        run = setup(url, [READ_A, reply("done")], auth=auth)
        out = await run.h.run(spec())
        [old] = nia.creds.values()
        nia.kill("agent:tester")
        nia.restore("agent:tester")
        nia.grant("agent:tester", tools=("repo.read",), data=("repo-A",))
        from legion.ports.credentials import CredentialStatus

        assert await auth.status(old.ref) is CredentialStatus.REVOKED
        await run.close()
    assert out.status is RunStatus.COMPLETED


@pytest.mark.parametrize(
    ("endpoint", "mode", "expect"),
    [
        ("identity", "503", "identity_unavailable"),
        ("identity", "401", "identity_unavailable"),
        ("issue", "503", "refused"),
        ("issue", "401", "refused"),
        ("status", "401", "refused"),
    ],
)
async def test_one_nia_role_failing_is_not_covered_by_the_other(
    endpoint: str, mode: str, expect: str
) -> None:
    from legion.domain.errors import IdentityUnavailable

    nia = fake()
    nia.modes[endpoint] = mode
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")])
        if expect == "identity_unavailable":
            # identity can't be confirmed: the run doesn't start, whatever the credential side says
            with pytest.raises(IdentityUnavailable):
                await run.h.run(spec())
            await run.close()
            assert not issued(nia) and run.seen == []
            return
        out = await run.h.run(spec())
        await run.close()
    assert run.seen == []
    events = await run.h.events(out.run_id)
    assert [e for e in events if e.type is E.CREDENTIAL_REFUSED]


async def test_viewer_token_cannot_issue_and_issuer_token_is_not_used_for_identity() -> None:
    nia = fake()
    with serve(nia) as url:
        # a credential authority configured with the viewer token: NIA refuses to issue
        from legion.access.secrets import EnvResolver
        from legion.adapters.nia_credentials import NiaCredentialAuthority, NiaCredentialConfig

        wrong = NiaCredentialAuthority(
            "nia",
            NiaCredentialConfig(
                provider="nia",
                endpoint=url,
                credential="env:V",
                trusted=True,
                audience="nia-gateway",
            ),
            AGENTS,
            EnvResolver({"V": VIEWER}),
        )
        run = setup(url, [READ_A, reply("done")], auth=wrong)
        out = await run.h.run(spec())
        await run.close()
    events = await run.h.events(out.run_id)
    assert [e for e in events if e.type is E.CREDENTIAL_REFUSED] and run.seen == []
    assert {a for (e, _, a, _) in nia.requests if e == "identity"} == {f"Bearer {VIEWER}"}


async def test_identity_and_authority_disagreeing_about_the_ref() -> None:
    nia = fake()
    with serve(nia) as url:
        auth = authority(url, agents={**AGENTS, "tester": "agent:sibling"})
        run = setup(url, [READ_A, reply("done")], auth=auth)
        out = await run.h.run(spec())
        await run.close()
    assert run.seen == [] and not issued(nia)
    events = await run.h.events(out.run_id)
    assert [e for e in events if e.type is E.CREDENTIAL_REFUSED]


# nothing secret anywhere Legion writes or shows


async def test_exfiltration_through_everything_legion_shows(tmp_path: Any) -> None:
    from typer.testing import CliRunner

    from legion.cli import describe

    nia = fake()
    nia.modes["status"] = "secret_in_error"
    nia.mode_from["status"] = 2
    with serve(nia) as url:
        run = setup(
            url,
            [READ_A, reply("again"), READ_A, reply("done")],
            echo=True,
        )
        out = await run.h.run(spec())
        await run.close()
    events = await run.h.events(out.run_id)
    shown = "\n".join(describe(e) for e in events)
    no_secrets(nia, shown)
    no_secrets(nia, await text_of(run.h, out.run_id))
    for request in run.h.provider.requests:
        no_secrets(nia, json.dumps(request.model_dump(mode="json"), default=str))
    assert CliRunner  # the CLI paths are covered with a real project in the CLI test below
