import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from legion.access.secrets import EnvResolver, SecretRef
from legion.authority.policy import Rule, Verdict
from legion.domain.action import Action, EffectClass
from legion.domain.errors import ToolRetryable
from legion.domain.states import RunStatus
from legion.events.sqlite_store import SqliteEventStore
from legion.events.types import EventType
from legion.models.scripted import call, calls, reply
from legion.ports.identity import AgentIdentity, ExternalDecision, KillState, NullIdentityPort
from legion.tools.base import ToolContext, ToolResult
from legion.tools.native import tool
from tests.support import agent, build

E = EventType


async def test_happy_path_reads_and_writes() -> None:
    h = build(
        [
            call("read_file", {"path": "docs/a.md"}),
            call("write_file", {"path": "out/x.md", "text": "summary"}),
            reply("done"),
        ]
    )
    outcome = await h.run()
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.output == "done"
    assert h.files.content["out/x.md"] == "summary"
    types = await h.types(outcome.run_id)
    assert types[:3] == [E.RUN_CREATED, E.TASK_CREATED, E.RUN_STARTED]
    assert types[-1] is E.RUN_COMPLETED
    assert types.index(E.TASK_COMPLETED) < types.index(E.RUN_COMPLETED)
    assert types.count(E.TOOL_COMPLETED) == 2
    assert (await h.store.verify(outcome.run_id)).ok


async def test_model_gets_tool_results() -> None:
    h = build([call("read_file", {"path": "docs/a.md"}, id="c1"), reply("done")])
    await h.run()
    last = h.provider.requests[-1].messages
    assert last[-1].role == "tool"
    assert last[-1].parts[0].content == "alpha"  # type: ignore[union-attr]
    assert last[-1].parts[0].call_id == "c1"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("step", "code"),
    [
        (call("no_such_tool", {}), "unknown_tool"),
        (call("read_file", {"path": 7}), "invalid_arguments"),
        (call("read_file", {}), "invalid_arguments"),
        (call("read_file", {"path": "secret/b.md"}), "capability_denied"),
        (call("read_file", {"path": "docs/../secret/b.md"}), "capability_denied"),
        (call("write_file", {"path": "docs/a.md", "text": "x"}), "capability_denied"),
    ],
)
async def test_refusals_are_recoverable(step: Any, code: str) -> None:
    h = build([step, reply("gave up")])
    outcome = await h.run()
    assert outcome.status is RunStatus.COMPLETED
    refused = await h.payloads(outcome.run_id, E.ACTION_REFUSED)
    assert [r["reason_code"] for r in refused] == [code]
    assert E.TOOL_STARTED not in await h.types(outcome.run_id)
    assert h.files.writes == []
    seen = h.provider.requests[-1].messages[-1]
    assert seen.parts[0].is_error  # type: ignore[union-attr]
    assert code in seen.parts[0].content  # type: ignore[union-attr]


async def test_tool_not_offered() -> None:
    h = build([call("write_file", {"path": "out/x", "text": "x"}), reply("ok")])
    outcome = await h.run(agent(tools=["read_file"], capabilities=["files.read:docs/**"]))
    refused = await h.payloads(outcome.run_id, E.ACTION_REFUSED)
    assert refused[0]["reason_code"] == "tool_not_offered"
    assert h.files.writes == []


async def test_policy_deny() -> None:
    rules = [Rule(decision=Verdict.DENY, tool="write_file", reason="read-only today")]
    h = build([call("write_file", {"path": "out/x", "text": "x"}), reply("ok")], rules=rules)
    outcome = await h.run()
    refused = await h.payloads(outcome.run_id, E.ACTION_REFUSED)
    assert refused[0]["reason_code"] == "policy_denied"
    assert "read-only today" in refused[0]["message"]
    assert h.files.writes == []


class VetoingAuthority(NullIdentityPort):
    source = "nia"

    def __init__(self) -> None:
        self.kill_after: int | None = None
        self.kill_checks = 0

    async def authorize(self, action: Action, identity: AgentIdentity) -> ExternalDecision:
        if action.tool == "write_file":
            return ExternalDecision(False, self.source, "agent is not granted write_file")
        return ExternalDecision(True, self.source, "ok")

    async def kill_state(self, identity: AgentIdentity) -> KillState:
        self.kill_checks += 1
        if self.kill_after is not None and self.kill_checks > self.kill_after:
            return KillState.KILLED
        return KillState.ACTIVE


async def test_external_authority_can_veto() -> None:
    port = VetoingAuthority()
    h = build([call("write_file", {"path": "out/x", "text": "x"}), reply("ok")], identity=port)
    outcome = await h.run()
    refused = await h.payloads(outcome.run_id, E.ACTION_REFUSED)
    assert refused[0]["reason_code"] == "policy_denied"
    assert refused[0]["message"].startswith("nia:")
    assert h.files.writes == []


async def test_kill() -> None:
    port = VetoingAuthority()
    port.kill_after = 2
    h = build(
        [
            call("read_file", {"path": "docs/a.md"}),
            call("read_file", {"path": "docs/a.md"}),
            reply("never"),
        ],
        identity=port,
    )
    outcome = await h.run()
    assert outcome.status is RunStatus.FAILED
    assert outcome.error_code == "killed"


async def test_repeated_action_is_refused_then_fatal() -> None:
    same = {"path": "docs/a.md"}
    h = build([call("read_file", same, id=f"c{i}") for i in range(6)] + [reply("x")])
    outcome = await h.run()
    assert outcome.status is RunStatus.FAILED
    assert outcome.error_code == "loop_detected"
    refused = await h.payloads(outcome.run_id, E.ACTION_REFUSED)
    assert [r["reason_code"] for r in refused] == ["repeated_action", "repeated_action"]
    assert len(await h.payloads(outcome.run_id, E.TOOL_COMPLETED)) == 2


class Empty(BaseModel):
    pass


def flaky_read(failures: int) -> Any:
    state = {"left": failures}

    @tool(
        effect=EffectClass.READ,
        capabilities=["files.read"],
        resource=lambda a: "docs/x",
        max_attempts=3,
    )
    async def flaky(args: Empty, ctx: ToolContext) -> str:
        """Fails a few times first."""
        if state["left"] > 0:
            state["left"] -= 1
            raise ToolRetryable("backend busy")
        return "finally"

    return flaky


async def test_read_tool_is_retried_with_backoff() -> None:
    h = build([call("flaky", {}), reply("ok")], extra_tools=[flaky_read(2)])
    outcome = await h.run(agent(tools=["flaky"], capabilities=["files.read:docs/**"]))
    assert outcome.status is RunStatus.COMPLETED
    failed = await h.payloads(outcome.run_id, E.TOOL_FAILED)
    assert [f["will_retry"] for f in failed] == [True, True]
    assert h.sleeps.delays == [1.0, 2.0]
    assert len(await h.payloads(outcome.run_id, E.TOOL_STARTED)) == 3


async def test_read_tool_retries_are_bounded() -> None:
    h = build([call("flaky", {}), reply("ok")], extra_tools=[flaky_read(10)])
    outcome = await h.run(agent(tools=["flaky"], capabilities=["files.read:docs/**"]))
    assert outcome.status is RunStatus.COMPLETED
    failed = await h.payloads(outcome.run_id, E.TOOL_FAILED)
    assert [f["will_retry"] for f in failed] == [True, True, False]
    assert failed[-1]["disposition"] == "recoverable"


async def test_write_timeout_in_doubt() -> None:
    attempts = []

    @tool(
        effect=EffectClass.WRITE,
        capabilities=["files.write"],
        resource=lambda a: "out/x",
        timeout_s=0.05,
        max_attempts=3,
    )
    async def slow_write(args: Empty, ctx: ToolContext) -> str:
        """Hangs."""
        attempts.append(1)
        await asyncio.sleep(5)
        return "late"

    h = build([call("slow_write", {}), reply("never")], extra_tools=[slow_write])
    outcome = await h.run(agent(tools=["slow_write"], capabilities=["files.write:out/**"]))
    assert outcome.status is RunStatus.PAUSED
    assert outcome.blocked_call == "call_1"
    assert len(attempts) == 1
    assert len(await h.payloads(outcome.run_id, E.ACTION_IN_DOUBT)) == 1


async def test_tool_exception_is_reported_to_the_model() -> None:
    h = build([call("read_file", {"path": "docs/missing.md"}), reply("recovered")])
    outcome = await h.run()
    assert outcome.status is RunStatus.COMPLETED
    failed = await h.payloads(outcome.run_id, E.TOOL_FAILED)
    assert failed[0]["error_code"] == "tool_failed"
    assert "FileNotFoundError" in failed[0]["message"]
    last = h.provider.requests[-1].messages[-1]
    assert last.parts[0].is_error  # type: ignore[union-attr]


def echo_secret_tool() -> Any:
    @tool(
        effect=EffectClass.READ,
        capabilities=["api.read"],
        resource=lambda a: "x",
        credentials=["api_token"],
    )
    async def use_api(args: Empty, ctx: ToolContext) -> str:
        """Calls an API. Badly written: echoes its credential."""
        return f"called with {ctx.credentials['api_token'].reveal()}"

    return use_api


async def test_secret_not_in_model_or_log(tmp_path: Path) -> None:
    sentinel = "sk-test-SENTINEL-4f8a9b"
    store = SqliteEventStore(tmp_path / "e.db")
    h = build(
        [call("use_api", {}), reply("ok")],
        extra_tools=[echo_secret_tool()],
        store=store,
        credentials=EnvResolver({"API_TOKEN": sentinel}),
        credential_bindings={"api_token": SecretRef.parse("env:API_TOKEN")},
        grantable=("api.read",),
    )
    outcome = await h.run(agent(tools=["use_api"], capabilities=["api.read"]))
    assert outcome.status is RunStatus.COMPLETED
    completed = await h.payloads(outcome.run_id, E.TOOL_COMPLETED)
    assert completed[0]["redactions"] == 1
    assert "[redacted]" in completed[0]["content"]
    for request in h.provider.requests:
        assert sentinel not in request.model_dump_json()
    store.close()
    for path in tmp_path.iterdir():
        assert sentinel.encode() not in path.read_bytes(), path


async def test_missing_credential_binding_fails_closed() -> None:
    h = build(
        [call("use_api", {}), reply("ok")],
        extra_tools=[echo_secret_tool()],
        grantable=("api.read",),
    )
    outcome = await h.run(agent(tools=["use_api"], capabilities=["api.read"]))
    assert outcome.status is RunStatus.FAILED
    assert outcome.error_code == "credential_unavailable"


async def test_large_output_goes_to_an_artifact() -> None:
    @tool(
        effect=EffectClass.READ,
        capabilities=["files.read"],
        resource=lambda a: "docs/big",
        max_output_chars=100,
    )
    async def big(args: Empty, ctx: ToolContext) -> str:
        """Returns a lot."""
        return "x" * 1000

    h = build([call("big", {}), reply("ok")], extra_tools=[big])
    outcome = await h.run(agent(tools=["big"], capabilities=["files.read:docs/**"]))
    [done] = await h.payloads(outcome.run_id, E.TOOL_COMPLETED)
    assert done["truncated"]
    assert h.artifacts.get(done["artifact"]) == b"x" * 1000
    assert len(done["content"]) < 300


async def test_bad_tool_output_is_withheld() -> None:
    @tool(
        effect=EffectClass.WRITE,
        capabilities=["files.write"],
        resource=lambda a: "out/y",
        output_schema={"type": "object", "properties": {"count": {"type": "integer"}}},
    )
    async def counted(args: Empty, ctx: ToolContext) -> ToolResult:
        """Should return a count."""
        return ToolResult(data={"count": "private-value-123"})

    h = build([call("counted", {}), reply("ok")], extra_tools=[counted])
    outcome = await h.run(agent(tools=["counted"], capabilities=["files.write:out/**"]))
    [done] = await h.payloads(outcome.run_id, E.TOOL_COMPLETED)
    assert done["is_error"] is True
    assert "has taken effect" in done["content"]
    assert "private-value-123" not in json.dumps(await h.payloads(outcome.run_id, E.TOOL_COMPLETED))


async def test_parallel_calls_in_one_turn_run_in_order() -> None:
    h = build(
        [
            calls(
                [("read_file", {"path": "docs/a.md"}), ("read_file", {"path": "secret/b.md"})],
                ids=["c1", "c2"],
            ),
            reply("ok"),
        ]
    )
    outcome = await h.run()
    events = await h.events(outcome.run_id)
    order = [
        (e.type, e.payload.get("call_id"))
        for e in events
        if e.type in (E.TOOL_COMPLETED, E.ACTION_REFUSED)
    ]
    assert order == [(E.TOOL_COMPLETED, "c1"), (E.ACTION_REFUSED, "c2")]


async def test_broken_resource_fn_refuses() -> None:
    @tool(effect=EffectClass.READ, capabilities=["files.read"], resource=lambda a: a.nope)
    async def broken(args: Empty, ctx: ToolContext) -> str:
        """Resource function is wrong."""
        return "should not run"

    h = build([call("broken", {}), reply("ok")], extra_tools=[broken])
    outcome = await h.run(agent(tools=["broken"], capabilities=["files.read:**"]))
    assert outcome.status is RunStatus.COMPLETED
    [refused] = await h.payloads(outcome.run_id, E.ACTION_REFUSED)
    assert refused["reason_code"] == "invalid_arguments"
    assert E.TOOL_STARTED not in await h.types(outcome.run_id)


@pytest.mark.parametrize("path", ["docs/a\u0000b", "docs/a\x00.md"])
async def test_unrecordable_resource_is_refused(path: str) -> None:
    h = build([call("read_file", {"path": path}), reply("ok")])
    outcome = await h.run()
    assert outcome.status is RunStatus.COMPLETED
    [refused] = await h.payloads(outcome.run_id, E.ACTION_REFUSED)
    assert refused["reason_code"] == "invalid_arguments"


async def test_nan_in_arguments_is_refused() -> None:
    h = build([call("read_file", {"path": "docs/a.md", "x": float("nan")}), reply("ok")])
    outcome = await h.run()
    assert outcome.status is RunStatus.COMPLETED
    assert (await h.store.verify(outcome.run_id)).ok


def fatal_secret_tool() -> Any:
    from legion.domain.errors import ActionInDoubt

    @tool(
        effect=EffectClass.WRITE,
        capabilities=["api.write"],
        resource=lambda a: "x",
        credentials=["api_token"],
    )
    async def leaky(args: Empty, ctx: ToolContext) -> str:
        """Fails fatally and puts its credential in the message."""
        raise ActionInDoubt(f"gave up with token {ctx.credentials['api_token'].reveal()}")

    return leaky


async def test_secret_redacted_on_fatal_error(tmp_path: Path) -> None:
    sentinel = "sk-test-FATAL-77aa11"
    store = SqliteEventStore(tmp_path / "e.db")
    h = build(
        [call("leaky", {}), reply("never")],
        extra_tools=[fatal_secret_tool()],
        store=store,
        credentials=EnvResolver({"API_TOKEN": sentinel}),
        credential_bindings={"api_token": SecretRef.parse("env:API_TOKEN")},
        grantable=("api.write",),
    )
    outcome = await h.run(agent(tools=["leaky"], capabilities=["api.write"]))
    assert outcome.status is RunStatus.PAUSED
    [doubt] = await h.payloads(outcome.run_id, E.ACTION_IN_DOUBT)
    assert "[redacted]" in doubt["reason"]
    store.close()
    for path in tmp_path.iterdir():
        assert sentinel.encode() not in path.read_bytes(), path


async def test_cancel_records_terminal_events() -> None:
    started = asyncio.Event()

    @tool(
        effect=EffectClass.WRITE,
        capabilities=["files.write"],
        resource=lambda a: "out/x",
        timeout_s=30,
    )
    async def hang(args: Empty, ctx: ToolContext) -> str:
        """Hangs until cancelled."""
        started.set()
        await asyncio.sleep(30)
        return "late"

    h = build([call("hang", {}), reply("never")], extra_tools=[hang])
    running = asyncio.create_task(h.run(agent(tools=["hang"], capabilities=["files.write:out/**"])))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    [run_summary] = await h.store.runs()
    types = await h.types(run_summary.run_id)
    assert types[-1] is E.RUN_CANCELLED
    assert E.ACTION_IN_DOUBT in types
    assert (await h.store.verify(run_summary.run_id)).ok
