import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel

from legion.domain.action import EffectClass
from legion.domain.budget import BudgetLimits
from legion.domain.errors import ConfigError, MalformedModelResponse, ModelAuthError, RateLimited
from legion.domain.states import RunStatus
from legion.events.projections import RunState
from legion.events.types import EventType, Usage
from legion.models.base import ModelResponse, StopReason
from legion.models.resolver import Pricing
from legion.models.scripted import call, reply
from legion.tools.base import ToolContext
from legion.tools.native import tool
from tests.support import agent, build

E = EventType


def usage(n: int) -> Usage:
    return Usage(input_tokens=n, output_tokens=0)


async def test_step_budget_stops_the_run() -> None:
    h = build([call("read_file", {"path": "docs/a.md"}, id=f"c{i}") for i in range(10)])
    outcome = await h.run(agent(budget=BudgetLimits(steps=2)))
    assert outcome.status is RunStatus.FAILED
    assert outcome.error_code == "budget_exceeded"
    [exceeded] = await h.payloads(outcome.run_id, E.BUDGET_EXCEEDED)
    assert exceeded["dimension"] == "steps"
    assert len(h.provider.requests) == 2


async def test_tool_call_budget() -> None:
    h = build(
        [
            call("read_file", {"path": "docs/a.md"}, id="c1"),
            call("read_file", {"path": "docs/a.md"}, id="c2"),
            reply("x"),
        ]
    )
    outcome = await h.run(agent(budget=BudgetLimits(tool_calls=1)))
    assert outcome.error_code == "budget_exceeded"
    [exceeded] = await h.payloads(outcome.run_id, E.BUDGET_EXCEEDED)
    assert exceeded["dimension"] == "tool_calls"


async def test_token_overshoot_is_recorded_then_stops() -> None:
    h = build([reply("big", usage=usage(500))])
    outcome = await h.run(agent(budget=BudgetLimits(tokens=100)))
    assert outcome.status is RunStatus.FAILED
    consumed = await h.payloads(outcome.run_id, E.BUDGET_CONSUMED)
    assert any(c["dimension"] == "tokens" and Decimal(c["total"]) == 500 for c in consumed)


async def test_output_cap_follows_remaining_tokens() -> None:
    h = build([call("read_file", {"path": "docs/a.md"}), reply("ok")])
    await h.run(agent(budget=BudgetLimits(tokens=1000), max_output_tokens=4096))
    assert h.provider.requests[0].max_output_tokens == 1000
    assert h.provider.requests[1].max_output_tokens < 1000


async def test_cost_budget_needs_pricing() -> None:
    h = build([reply("x")])
    with pytest.raises(ConfigError, match="pricing"):
        await h.run(agent(budget=BudgetLimits(cost_usd=Decimal("1"))))
    assert await h.store.runs() == []


async def test_cost_is_charged() -> None:
    pricing = Pricing(input_per_mtok=Decimal("3"), output_per_mtok=Decimal("15"))
    h = build([reply("x", usage=Usage(input_tokens=1_000_000, output_tokens=0))], pricing=pricing)
    outcome = await h.run(agent(budget=BudgetLimits(cost_usd=Decimal("2"), tokens=None)))
    assert outcome.error_code == "budget_exceeded"
    [responded] = await h.payloads(outcome.run_id, E.MODEL_RESPONDED)
    assert Decimal(responded["cost_usd"]) == Decimal("3")


async def test_rate_limit_is_retried_with_the_hint() -> None:
    h = build(
        [RateLimited("slow down", retry_after=4.0), MalformedModelResponse("junk"), reply("ok")]
    )
    outcome = await h.run()
    assert outcome.status is RunStatus.COMPLETED
    failed = await h.payloads(outcome.run_id, E.MODEL_FAILED)
    assert [f["will_retry"] for f in failed] == [True, True]
    assert h.sleeps.delays == [4.0, 2.0]
    calls_charged = [
        c
        for c in await h.payloads(outcome.run_id, E.BUDGET_CONSUMED)
        if c["dimension"] == "model_calls"
    ]
    assert len(calls_charged) == 3


async def test_model_retries_are_bounded() -> None:
    h = build([RateLimited("no")] * 5)
    outcome = await h.run()
    assert outcome.error_code == "rate_limited"
    assert len(h.provider.requests) == 3


async def test_auth_error_is_not_retried() -> None:
    h = build([ModelAuthError("bad key"), reply("never")])
    outcome = await h.run()
    assert outcome.error_code == "model_auth_error"
    assert len(h.provider.requests) == 1


SCHEMA = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string"}}}


async def test_structured_output_is_validated_and_retried() -> None:
    h = build([reply("not json"), reply('```json\n{"verdict": "fine"}\n```')])
    outcome = await h.run(agent(output_schema=SCHEMA))
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.structured == {"verdict": "fine"}
    assert "rejected" in h.provider.requests[-1].messages[-1].text
    assert "JSON object" in h.provider.requests[0].system


async def test_structured_output_gives_up() -> None:
    h = build([reply("{}")] * 3 + [reply("never")])
    outcome = await h.run(agent(output_schema=SCHEMA))
    assert outcome.error_code == "final_output_invalid"
    assert len(await h.payloads(outcome.run_id, E.OUTPUT_REJECTED)) == 2


async def test_truncated_answer_is_flagged() -> None:
    truncated = ModelResponse(
        message=reply("partial").message, stop_reason=StopReason.MAX_TOKENS, usage=usage(1)
    )
    h = build([truncated])
    outcome = await h.run()
    [done] = await h.payloads(outcome.run_id, E.TASK_COMPLETED)
    assert done["truncated"] is True


class Empty(BaseModel):
    pass


async def test_wall_clock_marks_running_write_in_doubt() -> None:
    @tool(
        effect=EffectClass.WRITE,
        capabilities=["files.write"],
        resource=lambda a: "out/x",
        timeout_s=30,
    )
    async def slow(args: Empty, ctx: ToolContext) -> str:
        """Takes too long."""
        await asyncio.sleep(30)
        return "late"

    h = build([call("slow", {}), reply("never")], extra_tools=[slow])
    outcome = await h.run(
        agent(
            tools=["slow"], capabilities=["files.write:out/**"], budget=BudgetLimits(wall_seconds=1)
        )
    )
    assert outcome.error_code == "budget_exceeded"
    types = await h.types(outcome.run_id)
    assert E.ACTION_IN_DOUBT in types
    assert types.index(E.ACTION_IN_DOUBT) < types.index(E.RUN_FAILED)


async def test_deadline_already_passed() -> None:
    h = build([reply("x")])
    outcome = await h.run(deadline=datetime.now(UTC) - timedelta(seconds=1))
    assert outcome.error_code == "deadline_exceeded"


async def test_check_reports_problems_before_any_event() -> None:
    h = build([reply("x")], grantable=("files.read:docs/**",))
    errors, _ = h.legion.check(agent(tools=["read_file", "nope"], capabilities=["files.read:**"]))
    assert any("unknown tools: nope" in e for e in errors)
    assert any("not grantable" in e for e in errors)
    errors, _ = h.legion.check(agent(tools=["write_file"], capabilities=["files.read:docs/**"]))
    assert any("needs files.write" in e for e in errors)


async def test_rebuilt_state_matches_what_the_model_saw() -> None:
    h = build(
        [
            call("read_file", {"path": "docs/a.md"}),
            call("read_file", {"path": "secret/b.md"}),
            reply("done"),
        ]
    )
    outcome = await h.run()
    state = RunState.from_events(outcome.run_id, await h.events(outcome.run_id))
    assert state.status is RunStatus.COMPLETED
    task = state.tasks[state.root_task_id]  # type: ignore[index]
    seen = h.provider.requests[-1].messages
    assert tuple(task.transcript[: len(seen)]) == seen
    assert task.transcript[-1] == reply("done").message
    assert not task.in_flight


async def test_exhausted_tokens_do_not_charge_another_model_call() -> None:
    h = build([call("read_file", {"path": "docs/a.md"}), reply("x")])
    outcome = await h.run(agent(budget=BudgetLimits(tokens=15)))
    assert outcome.error_code == "budget_exceeded"
    consumed = await h.payloads(outcome.run_id, E.BUDGET_CONSUMED)
    assert [c["total"] for c in consumed if c["dimension"] == "model_calls"] == ["1"]
    assert len(h.provider.requests) == 1


def test_naive_deadline_is_rejected() -> None:
    from legion.domain.task import TaskSpec
    from tests.support import PRINCIPAL

    with pytest.raises(ValueError, match="timezone"):
        TaskSpec(id="t", objective="x", created_by=PRINCIPAL, deadline=datetime(2030, 1, 1))


async def test_deadline_without_wall_clock_limit() -> None:
    from legion.domain.budget import BudgetLimits as B

    @tool(effect=EffectClass.READ, capabilities=["files.read"], resource=lambda a: "docs/x")
    async def slow(args: Empty, ctx: ToolContext) -> str:
        """Slow read."""
        await asyncio.sleep(5)
        return "late"

    h = build([call("slow", {}), reply("never")], extra_tools=[slow])
    outcome = await h.run(
        agent(tools=["slow"], capabilities=["files.read:docs/**"], budget=B(wall_seconds=None)),
        deadline=datetime.now(UTC) + timedelta(milliseconds=200),
    )
    assert outcome.error_code == "deadline_exceeded"
