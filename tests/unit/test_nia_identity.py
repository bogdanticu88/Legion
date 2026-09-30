# NIA as the identity authority (ADR 0020), against a stand-in NIA over a real socket. Active
# agents run; killed, unknown, unmapped or unconfirmable ones don't; nothing NIA says or fails to
# say turns into "allowed".

from pathlib import Path
from typing import Any

import pytest

from legion.access.secrets import EnvResolver
from legion.adapters.nia import NiaIdentityConfig, NiaIdentityPort
from legion.authority.policy import Rule, Verdict
from legion.domain.agent import AgentSpec, ModelRequirement
from legion.domain.errors import ConfigError, IdentityUnavailable, IdentityUnknown
from legion.domain.grant import DelegationLimits
from legion.domain.states import RunStatus
from legion.events.sqlite_store import SqliteEventStore
from legion.events.types import EventType
from legion.kernel import operator
from legion.models.scripted import call, reply
from legion.ports.identity import AgentIdentity, KillState
from tests.nia_lab import TOKEN, FakeNia, serve
from tests.support import OPERATOR, SimulatedCrash, agent, build, crash_at

E = EventType
READ = call("read_file", {"path": "docs/a.md"})
AGENTS = {"tester": "agent:tester", "helper": "agent:helper"}


def config(endpoint: str, **kw: Any) -> NiaIdentityConfig:
    return NiaIdentityConfig(
        provider="nia",
        endpoint=endpoint,
        credential="env:NIA_TOKEN",
        timeout_s=kw.pop("timeout_s", 1.0),
        agents=kw.pop("agents", AGENTS),
        **kw,
    )


def port(endpoint: str, **kw: Any) -> NiaIdentityPort:
    return NiaIdentityPort(config(endpoint, **kw), EnvResolver({"NIA_TOKEN": TOKEN}))


def harness(endpoint: str, steps: list[Any], **kw: Any) -> Any:
    identity = port(endpoint, **kw.pop("port", {}))
    return build(steps, identity=identity, **kw), identity


async def reads(h: Any, run_id: str) -> int:
    return len(await h.payloads(run_id, E.TOOL_COMPLETED))


# configuration


@pytest.mark.parametrize(
    ("endpoint", "ok"),
    [
        ("https://nia.example.com", True),
        ("http://127.0.0.1:8080", True),
        ("http://[::1]:8080", True),
        # the name resolves to both loopback addresses; only a literal one is allowed
        ("http://localhost:8080", False),
        ("http://nia.example.com", False),
        ("http://localhost.evil.example", False),
        ("https://user:pw@nia.example.com", False),
        ("ftp://nia.example.com", False),
        ("nia.example.com", False),
        ("", False),
    ],
)
def test_endpoint_is_checked(endpoint: str, ok: bool) -> None:
    if ok:
        config(endpoint)
    else:
        with pytest.raises(ValueError):
            config(endpoint)


@pytest.mark.parametrize(
    "ref",
    ["", "agent/x", "agent:x\n", "agent:\x1bx", "agent:\u0430", "a" * 129, "agent x"],
)
def test_nia_refs_are_checked(ref: str) -> None:
    with pytest.raises(ValueError):
        config("https://nia.example.com", agents={"tester": ref})


def test_config_refuses_the_rest() -> None:
    with pytest.raises(ValueError):
        config("https://n.example", credential_literal="x")  # unknown option
    with pytest.raises((ValueError, ConfigError)):
        NiaIdentityConfig(
            provider="nia",
            endpoint="https://n.example",
            credential="op-token-literal",
            agents=AGENTS,
        )
    with pytest.raises(ValueError):
        config("https://n.example", timeout_s=0)
    with pytest.raises(ValueError):
        config("https://n.example", timeout_s=float("inf"))
    with pytest.raises(ValueError):
        config("https://n.example", agents={})


# identity mapping


async def test_active_agent_runs_and_the_decision_is_recorded() -> None:
    nia = FakeNia(agents={"agent:tester": "active"})
    with serve(nia) as url:
        h, identity = harness(url, [READ, reply("ok")])
        outcome = await h.run(agent())
        await identity.aclose()
    assert outcome.status is RunStatus.COMPLETED and await reads(h, outcome.run_id) == 1
    [authorized] = await h.payloads(outcome.run_id, E.ACTION_AUTHORIZED)
    assert authorized["external"]["ref"] == "agent:tester"
    assert authorized["external"]["state"] == "active"
    assert authorized["external"]["provider"] == "nia"
    assert "checked_at" in authorized["external"]
    # NIA is asked with the operator token, for the mapped ref only
    assert {path for path, _ in nia.requests} == {"/agents/agent%3Atester"}


async def test_unmapped_agent_never_starts() -> None:
    nia = FakeNia(agents={"agent:tester": "active"})
    with serve(nia) as url:
        h, identity = harness(url, [READ, reply("ok")])
        with pytest.raises(IdentityUnknown, match="no NIA identity is configured"):
            await h.run(agent(name="stranger"))
        await identity.aclose()
    assert nia.requests == []


async def test_agent_nia_does_not_know_never_starts() -> None:
    nia = FakeNia(agents={})
    with serve(nia) as url:
        h, identity = harness(url, [READ, reply("ok")])
        with pytest.raises(IdentityUnknown, match="NIA has no agent agent:tester"):
            await h.run(agent())
        await identity.aclose()


async def test_identity_from_elsewhere_cannot_name_its_own_ref() -> None:
    nia = FakeNia(agents={"agent:tester": "active", "agent:admin": "active"})
    with serve(nia) as url:
        identity = port(url)
        forged = AgentIdentity(agent_ref="tester", source="nia", external_id="agent:admin")
        with pytest.raises(IdentityUnavailable, match="mapping now says agent:tester"):
            await identity.kill_state(forged)
        assert (
            await identity.kill_state(AgentIdentity(agent_ref="tester", source="local"))
            is KillState.ACTIVE
        )
        await identity.aclose()
    assert all(path == "/agents/agent%3Atester" for path, _ in nia.requests)


# kill state


async def test_kill_stops_the_next_action_and_restore_lets_it_run_again() -> None:
    nia = FakeNia(agents={"agent:tester": "active"})
    with serve(nia) as url:
        h, identity = harness(url, [READ, reply("ok")])
        first = await h.run(agent())
        assert first.status is RunStatus.COMPLETED and await reads(h, first.run_id) == 1

        nia.kill("agent:tester")
        h2, identity2 = harness(url, [READ, reply("ok")])
        second = await h2.run(agent())
        assert second.status is RunStatus.FAILED and second.error_code == "killed"
        assert await reads(h2, second.run_id) == 0
        assert "agent:tester" in second.error_message

        # NIA's restore clears the kill sentinel; that's all Legion depends on
        nia.restore("agent:tester")
        h3, identity3 = harness(url, [READ, reply("ok")])
        third = await h3.run(agent())
        assert third.status is RunStatus.COMPLETED
        for i in (identity, identity2, identity3):
            await i.aclose()


async def test_kill_between_two_actions_of_one_run() -> None:
    # killed as soon as the first read completes; the second read never starts
    nia = FakeNia(agents={"agent:tester": "active"})
    steps = [READ, call("read_file", {"path": "docs/c.md"}), reply("ok")]
    with serve(nia) as url:
        h, identity = harness(url, steps, faults=_kill_at("after:tool.completed", nia))
        outcome = await h.run(agent())
        await identity.aclose()
    assert await reads(h, outcome.run_id) == 1
    assert outcome.status is RunStatus.FAILED and outcome.error_code == "killed"


async def test_kill_just_before_dispatch_is_seen() -> None:
    # every check up to the last one says active; the last one, just before tool.started, sees
    # the kill
    nia = FakeNia(agents={"agent:tester": "active"})
    with serve(nia) as url:
        h, identity = harness(url, [READ, reply("ok")], faults=_kill_at("credential:checked", nia))
        outcome = await h.run(agent())
        await identity.aclose()
    assert outcome.status is RunStatus.FAILED and outcome.error_code == "killed"
    assert await reads(h, outcome.run_id) == 0
    kinds = [e.type for e in await h.events(outcome.run_id)]
    assert E.ACTION_AUTHORIZED in kinds and E.TOOL_STARTED not in kinds


async def test_kill_after_the_last_check_is_not_seen_for_that_call() -> None:
    # the window Legion can't close: killed after the last check, the call still runs; the run
    # stops at the next check
    nia = FakeNia(agents={"agent:tester": "active"})
    with serve(nia) as url:
        h, identity = harness(url, [READ, reply("ok")], faults=_kill_at("tool:before_invoke", nia))
        outcome = await h.run(agent())
        await identity.aclose()
    assert await reads(h, outcome.run_id) == 1
    assert outcome.status is RunStatus.FAILED and outcome.error_code == "killed"


def _kill_at(point: str, nia: FakeNia) -> Any:
    def hook(name: str) -> None:
        if name == point:
            nia.kill("agent:tester")

    return hook


# NIA failing: nothing runs


@pytest.mark.parametrize(
    ("mode", "error", "message"),
    [
        ("401", "identity_unavailable", "refused Legion's credential (401)"),
        ("403", "identity_unavailable", "refused Legion's credential (403)"),
        ("404", "identity_unknown", "NIA has no agent"),
        ("500", "identity_unavailable", "answered 500"),
        ("503", "identity_unavailable", "answered 503"),
        ("redirect", "identity_unavailable", "answered 302"),
        ("wrong_agent", "identity_unavailable", "about some other agent"),
        ("escape_ref", "identity_unavailable", "about some other agent"),
        ("unchecked", "identity_unavailable", "couldn't confirm"),
        ("odd_state", "identity_unavailable", "no usable state"),
        ("wrong_types", "identity_unavailable", "no usable state"),
        ("not_object", "identity_unavailable", "about some other agent"),
        ("malformed", "identity_unavailable", "isn't valid JSON"),
        ("nan", "identity_unavailable", "isn't valid JSON"),
        ("huge", "identity_unavailable", "too large"),
        # a parser may or may not manage the nesting; either way it isn't an agent record
        ("deep", "identity_unavailable", "NIA"),
        ("slow", "identity_unavailable", "couldn't reach NIA"),
        ("drop", "identity_unavailable", "couldn't reach NIA"),
    ],
)
async def test_nia_failing_mid_run_stops_it(mode: str, error: str, message: str) -> None:
    # the first lookup (starting the run) works; then NIA misbehaves
    nia = FakeNia(agents={"agent:tester": "active"}, mode=mode, mode_from=2)
    with serve(nia) as url:
        h, identity = harness(url, [READ, reply("ok")], port={"timeout_s": 0.5})
        outcome = await h.run(agent())
        await identity.aclose()
    assert outcome.status is RunStatus.FAILED, outcome
    assert outcome.error_code == error
    assert message in outcome.error_message
    assert await reads(h, outcome.run_id) == 0
    # the redirect went nowhere
    assert all(not path.startswith("/steal") for path, _ in nia.requests)


async def test_nia_down_at_the_start_means_no_run() -> None:
    with serve(FakeNia()) as url:
        pass  # closed: nothing listens there any more
    h, identity = harness(url, [READ, reply("ok")], port={"timeout_s": 0.5})
    with pytest.raises(IdentityUnavailable, match="couldn't reach NIA"):
        await h.run(agent())
    await identity.aclose()


async def test_nia_restarting_is_retried_once() -> None:
    # one 503, then fine: a restart shouldn't fail the run, and doesn't make anything allowed
    # that NIA didn't confirm
    nia = FakeNia(agents={"agent:tester": "active"})

    def flaky(n: int, path: str) -> None:
        nia.mode = "503" if n == 2 else None

    nia.on_request = flaky
    with serve(nia) as url:
        h, identity = harness(url, [READ, reply("ok")])
        outcome = await h.run(agent())
        await identity.aclose()
    assert outcome.status is RunStatus.COMPLETED


# delegation

HELPER = AgentSpec(
    name="helper",
    instructions="read",
    model=ModelRequirement(profile="child/helper"),
    tools=("read_file",),
    capabilities=("files.read:docs/**",),
)
BOSS = dict(
    tools=["read_file", "delegate"],
    capabilities=["files.read:docs/**", "agent.delegate:**"],
    delegation=DelegationLimits(max_depth=1, max_children=1),
)
DELEGATE = call("delegate", {"agent": "helper", "objective": "read"})


def delegation(url: str, nia_agents: dict[str, str], **kw: Any) -> Any:
    return harness(
        url,
        [DELEGATE, reply("done")],
        agents={"helper": HELPER},
        scripts={"child/helper": [READ, reply("read")]},
        grantable=("files.read:**", "agent.delegate:**"),
        **kw,
    )


@pytest.mark.parametrize(
    ("nia_agents", "mapping", "child_runs", "refused"),
    [
        ({"agent:tester": "active", "agent:helper": "active"}, AGENTS, True, False),
        ({"agent:tester": "active"}, AGENTS, False, True),
        (
            {"agent:tester": "active", "agent:helper": "active"},
            {"tester": "agent:tester"},
            False,
            True,
        ),
        ({"agent:tester": "active", "agent:helper": "killed"}, AGENTS, False, True),
    ],
    ids=["both-known", "child-unknown-to-nia", "child-not-mapped", "child-killed"],
)
async def test_child_needs_its_own_active_identity(
    nia_agents: dict[str, str], mapping: dict[str, str], child_runs: bool, refused: bool
) -> None:
    nia = FakeNia(agents=dict(nia_agents))
    with serve(nia) as url:
        h, identity = delegation(url, nia_agents, port={"agents": mapping})
        outcome = await h.run(agent(**BOSS))
        await identity.aclose()
    child_reads = [
        e
        for e in await h.events(outcome.run_id)
        if e.type is E.TOOL_COMPLETED and e.agent_id == "helper"
    ]
    assert bool(child_reads) is child_runs
    if refused:
        [refusal] = await h.payloads(outcome.run_id, E.ACTION_REFUSED)
        assert refusal["reason_code"] == "delegation_refused"
    else:
        [record] = [
            e.payload
            for e in await h.events(outcome.run_id)
            if e.type is E.ACTION_AUTHORIZED and e.agent_id == "helper"
        ]
        # the child is asked about under its own ref, not its parent's
        assert record["external"]["ref"] == "agent:helper"


async def test_killed_parent_cannot_delegate() -> None:
    nia = FakeNia(agents={"agent:tester": "killed", "agent:helper": "active"})
    with serve(nia) as url:
        h, identity = delegation(url, nia.agents)
        outcome = await h.run(agent(**BOSS))
        await identity.aclose()
    assert outcome.status is RunStatus.FAILED and outcome.error_code == "killed"
    assert await reads(h, outcome.run_id) == 0


async def test_child_killed_after_delegation_does_nothing() -> None:
    nia = FakeNia(agents={"agent:tester": "active", "agent:helper": "active"})
    with serve(nia) as url:
        h, identity = delegation(
            url, nia.agents, faults=_kill_ref_at("after:task.started", nia, "agent:helper", nth=2)
        )
        outcome = await h.run(agent(**BOSS))
        await identity.aclose()
    assert await reads(h, outcome.run_id) == 0 or all(
        e.agent_id != "helper" for e in await h.events(outcome.run_id) if e.type is E.TOOL_COMPLETED
    )
    child_failed = [
        e
        for e in await h.events(outcome.run_id)
        if e.type is E.TASK_FAILED and e.agent_id == "helper"
    ]
    assert child_failed and child_failed[0].payload["error_code"] == "killed"


async def test_nia_down_during_the_child_stops_the_child() -> None:
    nia = FakeNia(agents={"agent:tester": "active", "agent:helper": "active"})
    with serve(nia) as url:
        h, identity = delegation(
            url,
            nia.agents,
            faults=_mode_at("after:task.started", nia, "500", nth=2),
            port={"attempts": 1},
        )
        outcome = await h.run(agent(**BOSS))
        await identity.aclose()
    assert not [
        e
        for e in await h.events(outcome.run_id)
        if e.type is E.TOOL_COMPLETED and e.agent_id == "helper"
    ]


def _kill_ref_at(point: str, nia: FakeNia, ref: str, nth: int = 1) -> Any:
    # the nth time `point` is reached: the root task starts before the child does
    seen = [0]

    def hook(name: str) -> None:
        if name == point:
            seen[0] += 1
            if seen[0] == nth:
                nia.kill(ref)

    return hook


def _mode_at(point: str, nia: FakeNia, mode: str, nth: int = 1) -> Any:
    seen = [0]

    def hook(name: str) -> None:
        if name == point:
            seen[0] += 1
            if seen[0] == nth:
                nia.mode = mode

    return hook


# resume


async def test_resume_after_pause_rechecks_nia() -> None:
    nia = FakeNia(agents={"agent:tester": "active"})
    rules = [Rule(decision=Verdict.REQUIRE_APPROVAL, tool="read_file")]
    with serve(nia) as url:
        h, identity = harness(url, [READ, reply("ok")], rules=rules, by_turn=True)
        first = await h.run(agent())
        assert first.status is RunStatus.PAUSED
        await operator.decide(h.store, h.legion.locks, first.approval_id, approve=True, by=OPERATOR)
        nia.kill("agent:tester")
        second = await h.restart().resume(first.run_id)
        await identity.aclose()
    assert second.status is RunStatus.FAILED and second.error_code == "killed"
    assert await reads(h, first.run_id) == 0


async def test_resume_after_crash_rechecks_nia() -> None:
    nia = FakeNia(agents={"agent:tester": "active"})
    with serve(nia) as url:
        h, identity = harness(
            url, [READ, reply("ok")], by_turn=True, faults=crash_at("model:after_response")
        )
        with pytest.raises(SimulatedCrash):
            await h.run(agent())
        nia.kill("agent:tester")
        [summary] = await h.store.runs()
        outcome = await h.restart(faults=None).resume(summary.run_id)
        await identity.aclose()
    assert outcome.status is RunStatus.FAILED and outcome.error_code == "killed"
    assert await reads(h, summary.run_id) == 0


async def test_in_doubt_write_is_not_repeated_whatever_nia_says() -> None:
    nia = FakeNia(agents={"agent:tester": "active"})
    write = call("write_file", {"path": "out/x", "text": "hi"}, id="w1")
    with serve(nia) as url:
        h, identity = harness(
            url, [write, reply("ok")], by_turn=True, faults=crash_at("tool:after_invoke")
        )
        with pytest.raises(SimulatedCrash):
            await h.run(agent())
        writes = list(h.files.writes)
        assert len(writes) == 1
        [summary] = await h.store.runs()
        # restored and active: still in doubt, still not repeated
        outcome = await h.restart(faults=None).resume(summary.run_id)
        assert outcome.status is RunStatus.PAUSED and outcome.blocked_call == "w1"
        # killed: still not repeated either
        nia.kill("agent:tester")
        again = await h.restart(faults=None).resume(summary.run_id)
        await identity.aclose()
    assert again.status in (RunStatus.PAUSED, RunStatus.FAILED)
    assert h.files.writes == writes


# secrets


async def test_the_nia_token_goes_nowhere(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    store = SqliteEventStore(tmp_path / "e.db")
    outcomes = []
    for mode in (None, "401", "500", "secrets", "malformed", "wrong_agent", "redirect"):
        nia = FakeNia(agents={"agent:tester": "active"}, mode=mode, mode_from=2)
        with serve(nia) as url:
            h, identity = harness(url, [READ, reply("ok")], store=store, port={"attempts": 1})
            outcomes.append(await h.run(agent()))
            await identity.aclose()
        # it was really sent, to NIA only
        assert all(auth == f"Bearer {TOKEN}" for _, auth in nia.requests)
    everything = caplog.text + "".join(repr(o) for o in outcomes)
    for summary in await store.runs():
        everything += "".join(e.model_dump_json() for e in await store.read(summary.run_id))
    store.close()
    assert TOKEN not in everything
    assert TOKEN.encode() not in (tmp_path / "e.db").read_bytes()
    assert "\x1b" not in everything


# through legion.yaml and the CLI


def test_nia_from_legion_yaml_and_the_token_stays_out_of_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import json
    import logging

    import yaml
    from typer.testing import CliRunner

    from legion.cli import app
    from legion.config.templates import write_project

    caplog.set_level(logging.DEBUG)
    write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NIA_TOKEN", TOKEN)

    def cli(*args: str) -> str:
        result = CliRunner().invoke(app, list(args))
        return result.output + (str(result.exception) if result.exception else "")

    outputs = []
    nia = FakeNia(agents={"agent:notes": "active"})
    with serve(nia) as url:
        config = yaml.safe_load((tmp_path / "legion.yaml").read_text())
        config["identity"] = {
            "provider": "nia",
            "endpoint": url,
            "credential": "env:NIA_TOKEN",
            "timeout_s": 1,
            "attempts": 1,
            "agents": {"notes-assistant": "agent:notes"},
        }
        (tmp_path / "legion.yaml").write_text(yaml.safe_dump(config))
        for mode in (None, "401", "500", "secrets"):
            nia.mode, nia.mode_from = mode, len(nia.requests) + 2
            outputs.append(cli("run", "agents/assistant.yaml", "go", "--json"))
        nia.kill("agent:notes")
        outputs.append(cli("run", "agents/assistant.yaml", "go", "--json"))
    # NIA gone
    outputs.append(cli("run", "agents/assistant.yaml", "go", "--json"))
    run_ids = [w for w in cli("runs").split() if w.startswith("run_")]
    for run_id in run_ids:
        outputs += [cli("inspect", run_id), cli("inspect", run_id, "--json"), cli("tasks", run_id)]
    outputs.append(cli("runs"))

    first = json.loads(outputs[0])
    assert first["status"] == "completed"
    assert any('"error_code": "killed"' in o for o in outputs)
    assert any("couldn't reach NIA" in o for o in outputs)
    everything = "\n".join(outputs) + caplog.text
    assert TOKEN not in everything
    assert "\x1b" not in everything
    data = b"".join(p.read_bytes() for p in (tmp_path / ".legion").rglob("*") if p.is_file())
    assert TOKEN.encode() not in data
    # the token really was used
    assert all(auth == f"Bearer {TOKEN}" for _, auth in nia.requests)


def test_identity_config_is_checked(tmp_path: Path) -> None:
    import yaml

    from legion.config.loader import load_config
    from legion.config.templates import write_project

    write_project(tmp_path)
    base = yaml.safe_load((tmp_path / "legion.yaml").read_text())
    good = {
        "provider": "nia",
        "endpoint": "https://nia.example.com",
        "credential": "env:T",
        "agents": {"notes-assistant": "agent:notes"},
    }
    for bad in (
        {**good, "provider": "other"},
        {**good, "endpoint": "http://nia.example.com"},
        {**good, "endpoint": "https://u:p@nia.example.com"},
        {**good, "credential": "literal-token-value"},
        {**good, "surprise": 1},
        {**good, "agents": {"notes-assistant": "agent/x"}},
        {**good, "timeout_s": -1},
    ):
        (tmp_path / "legion.yaml").write_text(yaml.safe_dump({**base, "identity": bad}))
        with pytest.raises(ConfigError) as caught:
            load_config(tmp_path / "legion.yaml")
        assert "literal-token-value" not in str(caught.value)
    (tmp_path / "legion.yaml").write_text(yaml.safe_dump({**base, "identity": good}))
    load_config(tmp_path / "legion.yaml")
