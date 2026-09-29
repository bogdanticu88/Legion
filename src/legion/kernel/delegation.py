from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from legion.authority.ledger import Ledger
from legion.canonical import canonical_json, digest
from legion.domain.action import EffectClass
from legion.domain.agent import AgentSpec
from legion.domain.budget import BudgetLimits, Dimension
from legion.domain.capability import Capability
from legion.domain.errors import (
    ActionInDoubt,
    ActionRefused,
    BudgetExceeded,
    Disposition,
    Killed,
    LegionError,
    ResumeRefused,
)
from legion.domain.grant import AttenuationError, DelegationLimits, Grant
from legion.domain.messages import ToolCallPart
from legion.domain.states import TaskStatus, is_terminal_task
from legion.domain.task import TaskSpec
from legion.events import types as ev
from legion.events.types import EventDraft, EventType
from legion.kernel.services import Kernel, TaskRuntime
from legion.models.resolver import ModelResolver, Resolved
from legion.tools.base import ToolContext, ToolResult, ToolSpec

DELEGATE = "delegate"
CONTEXT_LIMIT = 16_000
RESULT_OUTPUT_LIMIT = 4_000
# Dimensions that add up across tasks, so a child's share is reserved from the parent. Wall time
# isn't one of them: a child runs inside its parent's time.
ADDITIVE = (
    Dimension.STEPS,
    Dimension.MODEL_CALLS,
    Dimension.TOOL_CALLS,
    Dimension.TOKENS,
    Dimension.COST_USD,
)


class DelegationRefused(ActionRefused):
    code = "delegation_refused"


class DelegateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str = Field(description="Name of the agent to hand the task to.")
    objective: str = Field(min_length=1, max_length=4000, description="What the agent should do.")
    context: dict[str, Any] = Field(
        default_factory=dict, description="Only the facts the agent needs. Nothing else is shared."
    )
    capabilities: list[str] | None = Field(
        default=None, description="Narrow the agent's capabilities further. Optional."
    )
    budget: dict[str, float] | None = Field(
        default=None, description="Limits for the agent, e.g. {'tool_calls': 5}. Optional."
    )


class DelegateTool:
    """The built-in `delegate` tool. The pipeline runs it; it has no code of its own to invoke."""

    def __init__(self, agents: Mapping[str, AgentSpec]) -> None:
        listing = "; ".join(
            f"{name}: {spec.description or 'no description'}"
            for name, spec in sorted(agents.items())
        )
        self._spec = ToolSpec(
            name=DELEGATE,
            description=(
                "Hand a sub-task to another agent and wait for its result. It starts with only "
                f"the objective and context you give it. Agents: {listing or 'none configured'}"
            ),
            input_schema=DelegateArgs.model_json_schema(),
            effect=EffectClass.WRITE_IDEMPOTENT,
            capabilities=("agent.delegate",),
            timeout_s=3600,
            max_attempts=1,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def resource_of(self, arguments: dict[str, Any]) -> str | None:
        agent = arguments.get("agent")
        return agent if isinstance(agent, str) and agent else None

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        raise RuntimeError("delegate is run by the pipeline, not invoked")


def child_ids(parent_task_id: str, call_id: str) -> tuple[str, str]:
    # Derived from the parent's call, so a delegation that is run again after a crash finds the
    # child it already made instead of making a second one.
    key = digest({"parent": parent_task_id, "call": call_id})
    return f"task_{key[:16]}", f"grant_{key[16:32]}"


@dataclass(frozen=True)
class ChildPlan:
    task_id: str
    grant: Grant
    spec: AgentSpec
    task_spec: TaskSpec
    model: Resolved
    reservations: tuple[tuple[Dimension, Decimal], ...]
    existing: bool


class Delegator:
    def __init__(
        self,
        kernel: Kernel,
        *,
        agents: Mapping[str, AgentSpec],
        resolver: ModelResolver,
        max_tasks: int,
        run_task: Callable[[TaskRuntime], Awaitable[None]],
    ) -> None:
        self.k = kernel
        self.agents = agents
        self.resolver = resolver
        self.max_tasks = max_tasks
        self.run_task = run_task

    def plan(self, call: ToolCallPart, parent: TaskRuntime) -> ChildPlan | ActionRefused:
        """Work out the child a delegate call would create, or why it can't be created.

        Nothing is written here; the pipeline calls this before approval, so a refused
        delegation never costs an approval.
        """
        state = self.k.state
        view = state.tasks[parent.task_id]
        existing = view.children.get(call.id)
        if existing is not None:
            return self._existing(existing)

        try:
            args = DelegateArgs.model_validate(call.arguments)
        except ValidationError as exc:
            return DelegationRefused(f"bad delegate arguments: {exc.error_count()} problems")
        spec = self.agents.get(args.agent)
        if spec is None:
            return DelegationRefused(f"there is no agent named {args.agent!r}")
        if parent.grant.delegation.max_depth < 1:
            return DelegationRefused("this agent is not allowed to delegate")
        if len(view.children) >= parent.grant.delegation.max_children:
            return DelegationRefused(
                f"this task already has {len(view.children)} children, the most it may have"
            )
        if len(state.tasks) >= self.max_tasks:
            return DelegationRefused(f"this run already has {len(state.tasks)} tasks, the limit")
        if len(canonical_json(args.context)) > CONTEXT_LIMIT:
            return DelegationRefused(f"context is larger than {CONTEXT_LIMIT} characters")

        try:
            requested = (
                [Capability.parse(c) for c in args.capabilities]
                if args.capabilities is not None
                else list(spec.capabilities)
            )
        except ValueError:
            return DelegationRefused("capabilities must look like name[:resource]")
        outside_parent = [str(c) for c in requested if not c.is_within(parent.grant.capabilities)]
        if outside_parent:
            return DelegationRefused(
                f"this task's grant doesn't cover {', '.join(sorted(outside_parent))}"
            )
        outside_spec = [str(c) for c in requested if not c.is_within(frozenset(spec.capabilities))]
        if outside_spec:
            return DelegationRefused(
                f"{spec.name} is not defined to use {', '.join(sorted(outside_spec))}"
            )
        for tool_name in spec.tools:
            tool = self.k.tools.get(tool_name)
            if tool is None:
                return DelegationRefused(f"{spec.name} needs tool {tool_name}, which doesn't exist")
            for needed in tool.spec.capabilities:
                if not any(
                    Capability(name=c.name).covers(Capability(name=needed)) for c in requested
                ):
                    return DelegationRefused(
                        f"{spec.name} needs {needed} for {tool_name}, which it isn't being given"
                    )

        budget = self._budget(spec, args, parent)
        if isinstance(budget, DelegationRefused):
            return budget
        try:
            model = self.resolver.resolve(spec.model)
        except LegionError as exc:
            return DelegationRefused(exc.message)

        task_id, grant_id = child_ids(parent.task_id, call.id)
        try:
            grant = parent.grant.attenuate(
                id=grant_id,
                capabilities=frozenset(requested),
                budget=budget,
                agent_ref=spec.name,
                delegation=DelegationLimits(
                    max_depth=min(spec.delegation.max_depth, parent.grant.delegation.max_depth - 1),
                    max_children=min(
                        spec.delegation.max_children, parent.grant.delegation.max_children
                    ),
                ),
            )
        except AttenuationError as exc:
            return DelegationRefused(exc.message)

        parent_limits = parent.grant.budget
        reservations = tuple(
            (dim, Decimal(child_limit))
            for dim in ADDITIVE
            if (child_limit := budget.limit(dim)) is not None
            and parent_limits.limit(dim) is not None
        )
        return ChildPlan(
            task_id=task_id,
            grant=grant,
            spec=spec,
            task_spec=TaskSpec(
                id=task_id,
                objective=args.objective,
                context=args.context,
                created_by=parent.grant.identity.principal,
                parent_id=parent.task_id,
                deadline=parent.deadline,
            ),
            model=model,
            reservations=reservations,
            existing=False,
        )

    def _budget(
        self, spec: AgentSpec, args: DelegateArgs, parent: TaskRuntime
    ) -> BudgetLimits | DelegationRefused:
        # Each limit is the smallest of: the child agent's default, what the call asked for, and
        # what the parent has left. Asking for more than the parent has is refused outright.
        requested = args.budget or {}
        unknown = set(requested) - {d.value for d in Dimension}
        if unknown:
            return DelegationRefused(f"unknown budget dimensions: {', '.join(sorted(unknown))}")
        ledger = self.k.ledger(parent)
        values: dict[str, int | Decimal | None] = {}
        for dim in Dimension:
            remaining = ledger.remaining(dim)
            if dim is Dimension.WALL_SECONDS and remaining is not None:
                remaining -= Decimal(str(self.k.stretch_elapsed()))
            if dim is Dimension.TOOL_CALLS and remaining is not None:
                # the delegate call itself is charged one tool call before the child is made
                remaining -= 1
            asked = requested.get(dim.value)
            if asked is not None and asked < 0:
                return DelegationRefused(f"negative {dim.value} budget")
            if asked is not None and remaining is not None and Decimal(str(asked)) > remaining:
                return DelegationRefused(
                    f"asked for {asked} {dim.value}, but only {remaining} is left to give"
                )
            options = [
                Decimal(str(x)) for x in (spec.budget.limit(dim), asked, remaining) if x is not None
            ]
            limit = min(options) if options else None
            if limit is not None and dim is not Dimension.COST_USD:
                limit = Decimal(int(limit))
            if limit is not None and limit <= 0 and dim is not Dimension.COST_USD:
                return DelegationRefused(f"no {dim.value} budget left to give a child")
            values[dim.value] = limit if dim is Dimension.COST_USD or limit is None else int(limit)
        return BudgetLimits.model_validate(values)

    def _existing(self, task_id: str) -> ChildPlan:
        # A delegation being run again (after a pause or crash): everything comes from what was
        # recorded when the child was made, not from today's catalog.
        view = self.k.state.tasks[task_id]
        if view.agent_spec is None or view.grant is None:
            raise ResumeRefused(f"child task {task_id} has no recorded spec or grant")
        spec = AgentSpec.model_validate(view.agent_spec)
        model = self.resolver.resolve(spec.model)
        if (model.binding.provider, model.binding.model) != (view.provider, view.model):
            raise ResumeRefused(
                f"child {task_id} used {view.provider}/{view.model}, configuration now gives "
                f"{model.binding.provider}/{model.binding.model}"
            )
        return ChildPlan(
            task_id=task_id,
            grant=Grant.model_validate(view.grant),
            spec=spec,
            task_spec=TaskSpec.model_validate(view.spec),
            model=model,
            reservations=(),
            existing=True,
        )

    async def run(self, call: ToolCallPart, parent: TaskRuntime, plan: ChildPlan) -> ToolResult:
        k = self.k
        if not plan.existing:
            # The plan was made a moment ago against the same state, but a reservation that
            # doesn't fit would mean a bug that creates budget, so check again and fail loudly.
            ledger = k.ledger(parent)
            for dim, amount in plan.reservations:
                ledger.precheck(dim, amount)
            await k.recorder.append(self._creation(call, parent, plan))

        view = k.state.tasks[plan.task_id]
        child = TaskRuntime(
            run_id=parent.run_id,
            task_id=plan.task_id,
            parent_task_id=parent.task_id,
            agent=plan.spec,
            grant=plan.grant,
            identity=await k.identity.agent_identity(plan.spec.name),
            model=plan.model,
            deadline=plan.task_spec.deadline,
            lineage=(*parent.lineage, parent.identity),
        )
        started = time.monotonic()
        ran = False
        if not is_terminal_task(view.status):
            ran = True
            await self._run_child(child)
        if not view.settled:
            elapsed = time.monotonic() - started if ran else 0.0
            await k.recorder.append(self._settlement(parent, child, elapsed))
        return self._result(child)

    def _creation(
        self, call: ToolCallPart, parent: TaskRuntime, plan: ChildPlan
    ) -> list[EventDraft]:
        k = self.k
        created = k.recorder.draft(
            EventType.TASK_CREATED,
            ev.TaskCreated(
                task=plan.task_spec.model_dump(mode="json"),
                grant_id=plan.grant.id,
                agent=plan.spec.name,
                grant=plan.grant.model_dump(mode="json"),
                agent_spec=plan.spec.model_dump(mode="json"),
                provider=plan.model.binding.provider,
                model=plan.model.binding.model,
                delegated_by=call.id,
            ),
            task_id=plan.task_id,
            agent_id=plan.spec.name,
            parent_task_id=parent.task_id,
        )
        reserved = [
            k.draft(
                parent,
                EventType.BUDGET_RESERVED,
                ev.BudgetReserved(
                    grant_id=parent.grant.id,
                    child_grant_id=plan.grant.id,
                    child_task_id=plan.task_id,
                    dimension=dim.value,
                    amount=amount,
                ),
            )
            for dim, amount in plan.reservations
        ]
        waiting = k.draft(parent, EventType.TASK_WAITING, ev.Waiting(child_task_id=plan.task_id))
        # one append: the child, its reservations and the parent waiting all exist, or none do
        return [created, *reserved, waiting]

    async def _run_child(self, child: TaskRuntime) -> None:
        k = self.k
        limit = child.grant.budget.wall_seconds
        timeout = None
        if limit is not None:
            timeout = float(limit) - float(k.ledger(child).used(Dimension.WALL_SECONDS))
        failure: LegionError | None = None
        try:
            if timeout is not None and timeout <= 0:
                raise TimeoutError
            async with asyncio.timeout(timeout):
                await self.run_task(child)
        except TimeoutError:
            failure = BudgetExceeded(Dimension.WALL_SECONDS.value, limit, limit)
            await k.emit(child, EventType.BUDGET_EXCEEDED, _exceeded(child, failure))
        except LegionError as exc:
            # A pause (approval, in doubt) or a kill is about the whole run, so it goes up.
            # Anything else ends this child and the parent is told.
            if exc.disposition is Disposition.ESCALATE or isinstance(exc, Killed):
                raise
            failure = exc
        if failure is None:
            return
        view = k.state.tasks[child.task_id]
        unsafe = False
        for call_id, (action_hash, effect) in list(view.in_flight.items()):
            if call_id in view.children:
                continue
            unsafe = unsafe or not EffectClass(effect).safe_to_repeat
            await k.emit(
                child,
                EventType.ACTION_IN_DOUBT,
                ev.ActionInDoubt(
                    call_id=call_id, action_hash=action_hash, effect=effect, reason=failure.message
                ),
                {"action_hash": action_hash},
            )
        if unsafe:
            # Failing the child here would let the parent carry on as if the write hadn't
            # happened. Stop and ask instead.
            raise ActionInDoubt(f"child {child.task_id} stopped with an action in doubt")
        await k.emit(
            child,
            EventType.TASK_FAILED,
            ev.Failure(
                error_code=failure.code,
                message=failure.message,
                disposition=failure.disposition.value,
            ),
        )

    def _settlement(
        self, parent: TaskRuntime, child: TaskRuntime, elapsed: float
    ) -> list[EventDraft]:
        k = self.k
        drafts: list[EventDraft] = []
        if elapsed > 0:
            record, _ = k.ledger(child).charge(
                Dimension.WALL_SECONDS, Decimal(str(round(elapsed, 3)))
            )
            drafts.append(k.draft(child, EventType.BUDGET_CONSUMED, record))
        # Give back what was held for the child and charge the parent what the child really
        # used, including anything its own children used. Not clamped to the reservation: a
        # child can overshoot on tokens by one call's prompt, and that was really spent.
        ledger = k.ledger(child)
        for (grant_id, task_id, dimension), reserved in sorted(k.state.reservations.items()):
            if grant_id != parent.grant.id or task_id != child.task_id:
                continue
            drafts.append(
                k.draft(
                    parent,
                    EventType.BUDGET_SETTLED,
                    ev.BudgetSettled(
                        grant_id=parent.grant.id,
                        child_grant_id=child.grant.id,
                        child_task_id=child.task_id,
                        dimension=dimension,
                        reserved=reserved,
                        used=ledger.used(Dimension(dimension)),
                    ),
                )
            )
        if k.state.tasks[parent.task_id].status is TaskStatus.WAITING_CHILDREN:
            drafts.append(k.draft(parent, EventType.TASK_RESUMED, ev.Empty()))
        return drafts

    def _result(self, child: TaskRuntime) -> ToolResult:
        view = self.k.state.tasks[child.task_id]
        ledger = Ledger(child.grant, self.k.state)
        output = view.output or ""
        result = {
            "task_id": child.task_id,
            "agent": child.agent.name,
            "status": view.status.value,
            "output": output[:RESULT_OUTPUT_LIMIT],
            "output_truncated": len(output) > RESULT_OUTPUT_LIMIT,
            "structured": view.structured,
            "error_code": view.error.error_code if view.error else None,
            "error": view.error.message if view.error else None,
            "used": {
                d.value: str(ledger.used(d))
                for d in Dimension
                if ledger.used(d) or child.grant.budget.limit(d) is not None
            },
        }
        return ToolResult(
            content=json.dumps(result, sort_keys=True),
            data=result,
            is_error=view.status is not TaskStatus.COMPLETED,
        )


def _exceeded(task: TaskRuntime, exc: BudgetExceeded) -> ev.BudgetExceeded:
    return ev.BudgetExceeded(
        grant_id=task.grant.id,
        dimension=exc.dimension,
        limit=Decimal(str(exc.limit)),
        attempted=Decimal(str(exc.attempted)),
    )
