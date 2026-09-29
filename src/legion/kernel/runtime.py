from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from legion.access.secrets import CredentialResolver, EnvResolver, SecretRef
from legion.artifacts import ArtifactStore, MemoryArtifactStore
from legion.authority.policy import PolicyDecisionPoint
from legion.domain.action import EffectClass
from legion.domain.agent import AgentSpec, ModelFeature
from legion.domain.budget import Dimension
from legion.domain.capability import Capability
from legion.domain.errors import BudgetExceeded, ConfigError, DeadlineExceeded, LegionError
from legion.domain.grant import Grant
from legion.domain.principal import IdentityContext, Principal
from legion.domain.states import RunStatus
from legion.domain.task import TaskSpec
from legion.events import types as ev
from legion.events.projections import RunState
from legion.events.store import EventStore
from legion.events.types import EventType
from legion.kernel.loop import AgentLoop
from legion.kernel.pipeline import exceeded_payload
from legion.kernel.services import Kernel, Recorder, RetryPolicy, Sleep, TaskRuntime
from legion.models.resolver import ModelResolver
from legion.ports.identity import IdentityPort, NullIdentityPort
from legion.tools.registry import ToolRegistry


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    status: RunStatus
    output: str | None
    structured: dict[str, Any] | None
    error_code: str | None
    error_message: str | None


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Legion:
    def __init__(
        self,
        *,
        resolver: ModelResolver,
        tools: ToolRegistry,
        store: EventStore,
        policy: PolicyDecisionPoint,
        grantable: Sequence[Capability],
        identity: IdentityPort | None = None,
        credentials: CredentialResolver | None = None,
        credential_bindings: Mapping[str, SecretRef] | None = None,
        artifacts: ArtifactStore | None = None,
        settings: Mapping[str, str] | None = None,
        retry: RetryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        config_hash: str = "",
    ) -> None:
        self.resolver = resolver
        self.tools = tools
        self.store = store
        self.policy = policy
        self.grantable = frozenset(grantable)
        self.identity = identity or NullIdentityPort()
        self.credentials = credentials or EnvResolver()
        self.credential_bindings = dict(credential_bindings or {})
        self.artifacts = artifacts or MemoryArtifactStore()
        self.settings = dict(settings or {})
        self.retry = retry or RetryPolicy()
        self.sleep = sleep
        self.now = now
        self.config_hash = config_hash

    def check(self, agent: AgentSpec) -> tuple[list[str], list[str]]:
        """Return (errors, warnings). Any error means the agent can't start."""
        errors: list[str] = []
        warnings: list[str] = []
        unknown = [t for t in agent.tools if self.tools.get(t) is None]
        if unknown:
            errors.append(f"unknown tools: {', '.join(unknown)}")
        resolved = None
        try:
            resolved = self.resolver.resolve(agent.model)
        except LegionError as exc:
            errors.append(exc.message)
        if agent.tools and ModelFeature.TOOLS not in agent.model.needs:
            errors.append("agent lists tools but its model requirement does not need tools")
        wider = [str(c) for c in agent.capabilities if not c.is_within(self.grantable)]
        if wider:
            errors.append(f"capabilities not grantable by this configuration: {', '.join(wider)}")
        requested = [Capability(name=c.name) for c in agent.capabilities]
        for name in agent.tools:
            tool = self.tools.get(name)
            if tool is None:
                continue
            for needed in tool.spec.capabilities:
                if not any(c.covers(Capability(name=needed)) for c in requested):
                    errors.append(f"tool {name} needs {needed}, which the agent does not request")
            if tool.spec.effect is EffectClass.EXTERNAL_IRREVERSIBLE:
                warnings.append(
                    f"tool {name} is external_irreversible; the default policy denies it"
                )
        if agent.budget.cost_usd is not None and resolved and resolved.binding.pricing is None:
            errors.append("agent has a cost budget but the model binding has no pricing")
        return errors, warnings

    async def run(
        self,
        agent: AgentSpec,
        objective: str,
        *,
        principal: Principal,
        context: dict[str, Any] | None = None,
        constraints: Sequence[str] = (),
        deadline: datetime | None = None,
    ) -> RunOutcome:
        errors, _ = self.check(agent)
        if errors:
            raise ConfigError("; ".join(errors))
        resolved = self.resolver.resolve(agent.model)

        run_id, task_id = _id("run"), _id("task")
        grant = Grant(
            id=_id("grant"),
            capabilities=frozenset(agent.capabilities),
            budget=agent.budget,
            identity=IdentityContext(
                principal=principal, agent_ref=agent.name, on_behalf_of=(str(principal),)
            ),
            issuer="operator",
        )
        spec = TaskSpec(
            id=task_id,
            objective=objective,
            context=context or {},
            constraints=tuple(constraints),
            created_by=principal,
            deadline=deadline,
        )
        state = RunState(run_id)
        kernel = Kernel(
            recorder=Recorder(self.store, state),
            tools=self.tools,
            policy=self.policy,
            identity=self.identity,
            credentials=self.credentials,
            credential_bindings=self.credential_bindings,
            artifacts=self.artifacts,
            settings=self.settings,
            retry=self.retry,
            sleep=self.sleep,
            now=self.now,
        )
        task = TaskRuntime(
            run_id=run_id,
            task_id=task_id,
            parent_task_id=None,
            agent=agent,
            grant=grant,
            identity=await self.identity.agent_identity(agent.name),
            model=resolved,
            deadline=deadline,
        )

        await kernel.recorder.emit(
            EventType.RUN_CREATED,
            ev.RunCreated(
                agent=agent.name,
                agent_spec=agent.model_dump(mode="json"),
                agent_spec_hash=agent.spec_hash,
                provider=resolved.binding.provider,
                model=resolved.binding.model,
                profile=resolved.binding.profile,
                grant=grant.model_dump(mode="json"),
                root_task_id=task_id,
                config_hash=self.config_hash,
            ),
        )
        await kernel.emit(
            task,
            EventType.TASK_CREATED,
            ev.TaskCreated(task=spec.model_dump(mode="json"), grant_id=grant.id, agent=agent.name),
        )
        await kernel.recorder.emit(EventType.RUN_STARTED, ev.Empty())

        await self._execute(kernel, task)
        return _outcome(state)

    async def _execute(self, kernel: Kernel, task: TaskRuntime) -> None:
        limit = task.grant.budget.wall_seconds
        timeout = None if limit is None else float(limit)
        if task.deadline is not None:
            until_deadline = (task.deadline - self.now()).total_seconds()
            timeout = until_deadline if timeout is None else min(timeout, until_deadline)
        started = time.monotonic()
        try:
            try:
                async with asyncio.timeout(timeout):
                    await AgentLoop(kernel).run(task)
            except TimeoutError:
                await _mark_in_doubt(kernel, task, "run stopped by its time limit")
                deadline_hit = task.deadline is not None and self.now() >= task.deadline
                if limit is None or deadline_hit:
                    raise DeadlineExceeded(f"task {task.task_id} passed its deadline") from None
                elapsed = round(time.monotonic() - started, 3)
                exceeded = BudgetExceeded(Dimension.WALL_SECONDS.value, limit, elapsed)
                await kernel.emit(task, EventType.BUDGET_EXCEEDED, exceeded_payload(task, exceeded))
                raise exceeded from None
        except LegionError as exc:
            await _mark_in_doubt(kernel, task, exc.message)
            failure = ev.Failure(
                error_code=exc.code, message=exc.message, disposition=exc.disposition.value
            )
            await kernel.emit(task, EventType.TASK_FAILED, failure)
            await kernel.recorder.emit(EventType.RUN_FAILED, failure)
            return
        except asyncio.CancelledError:
            await _mark_in_doubt(kernel, task, "run cancelled")
            await kernel.emit(task, EventType.TASK_CANCELLED, ev.Cancelled(reason="cancelled"))
            await kernel.recorder.emit(EventType.RUN_CANCELLED, ev.Cancelled(reason="cancelled"))
            raise
        except Exception as exc:
            with contextlib.suppress(Exception):
                await _mark_in_doubt(kernel, task, f"internal error: {type(exc).__name__}")
            failure = ev.Failure(
                error_code="internal_error",
                message=f"{type(exc).__name__}: {exc}",
                disposition="fatal",
            )
            await kernel.emit(task, EventType.TASK_FAILED, failure)
            await kernel.recorder.emit(EventType.RUN_FAILED, failure)
            raise
        view = kernel.state.tasks[task.task_id]
        await kernel.recorder.emit(
            EventType.RUN_COMPLETED, ev.RunCompleted(output=view.output or "")
        )


async def _mark_in_doubt(kernel: Kernel, task: TaskRuntime, reason: str) -> None:
    view = kernel.state.tasks[task.task_id]
    for call_id, (action_hash, effect) in list(view.in_flight.items()):
        await kernel.emit(
            task,
            EventType.ACTION_IN_DOUBT,
            ev.ActionInDoubt(
                call_id=call_id, action_hash=action_hash, effect=effect, reason=reason
            ),
            {"action_hash": action_hash},
        )


def _outcome(state: RunState) -> RunOutcome:
    assert state.root_task_id is not None
    view = state.tasks[state.root_task_id]
    return RunOutcome(
        run_id=state.run_id,
        status=state.status,
        output=state.output,
        structured=view.structured,
        error_code=state.error.error_code if state.error else None,
        error_message=state.error.message if state.error else None,
    )
