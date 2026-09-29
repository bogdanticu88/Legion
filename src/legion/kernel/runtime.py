from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from legion.access.secrets import CredentialResolver, EnvResolver, SecretRef
from legion.artifacts import ArtifactStore, MemoryArtifactStore
from legion.authority.policy import PolicyDecisionPoint
from legion.domain.action import EffectClass
from legion.domain.agent import AgentSpec, ModelFeature
from legion.domain.budget import Dimension
from legion.domain.capability import Capability
from legion.domain.errors import (
    ActionInDoubt,
    ApprovalRequired,
    BudgetExceeded,
    ConfigError,
    DeadlineExceeded,
    Disposition,
    LegionError,
    ResumeRefused,
)
from legion.domain.grant import Grant
from legion.domain.principal import IdentityContext, Principal
from legion.domain.states import RunStatus, TaskStatus, is_terminal_run, is_terminal_task
from legion.domain.task import TaskSpec
from legion.events import types as ev
from legion.events.projections import RunState, TaskView
from legion.events.sqlite_store import SqliteEventStore
from legion.events.store import EventStore
from legion.events.types import EventDraft, EventType
from legion.kernel.delegation import DELEGATE, DelegateTool, Delegator
from legion.kernel.locks import FileRunLocks, InProcessRunLocks, RunLocks
from legion.kernel.loop import AgentLoop
from legion.kernel.pipeline import exceeded_payload
from legion.kernel.services import (
    Faults,
    Kernel,
    Recorder,
    RetryPolicy,
    Sleep,
    TaskRuntime,
    no_faults,
)
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
    # set when the run is paused
    approval_id: str | None = None
    blocked_call: str | None = None
    # calls whose outcome nobody knows yet; a cancelled or failed run can still have these
    in_doubt: tuple[str, ...] = ()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


async def load_state(store: EventStore, run_id: str) -> RunState:
    """Rebuild a run from its log, refusing anything that doesn't verify or doesn't parse."""
    result = await store.verify(run_id)
    if result.checked == 0 and result.ok:
        raise ResumeRefused(f"no run {run_id}")
    if not result.ok:
        raise ResumeRefused(
            f"event log for {run_id} fails verification at seq {result.bad_seq}: {result.reason}"
        )
    try:
        return RunState.from_events(run_id, await store.read(run_id))
    except Exception as exc:
        # A log can verify and still not make sense (someone recomputed the chain after editing
        # it). Whatever goes wrong while replaying it, refuse; never act on half a state.
        raise ResumeRefused(f"event log for {run_id} can't be replayed: {exc}") from exc


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
        locks: RunLocks | None = None,
        approval_ttl: timedelta = timedelta(hours=1),
        faults: Faults = no_faults,
        agents: Mapping[str, AgentSpec] | None = None,
        max_delegation_depth: int = 2,
        max_tasks: int = 16,
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
        self.locks = locks or _default_locks(store)
        self.approval_ttl = approval_ttl
        self.faults = faults
        self.agents = dict(agents or {})
        self.max_delegation_depth = max_delegation_depth
        self.max_tasks = max_tasks
        existing = self.tools.get(DELEGATE)
        if existing is None:
            self.tools.register(DelegateTool(self.agents))
        elif not isinstance(existing, DelegateTool):
            raise ConfigError(f"the tool name {DELEGATE!r} is reserved")

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
                warnings.append(f"tool {name} is external_irreversible; expect approval requests")
        if agent.budget.cost_usd is not None and resolved and resolved.binding.pricing is None:
            errors.append("agent has a cost budget but the model binding has no pricing")
        if agent.delegation.max_depth > self.max_delegation_depth:
            errors.append(
                f"agent asks for delegation depth {agent.delegation.max_depth}, "
                f"this configuration allows {self.max_delegation_depth}"
            )
        if DELEGATE in agent.tools and agent.delegation.max_depth < 1:
            errors.append("agent lists delegate but its delegation.max_depth is 0")
        return errors, warnings

    def _kernel(self, state: RunState) -> Kernel:
        kernel = Kernel(
            recorder=Recorder(self.store, state, faults=self.faults),
            tools=self.tools,
            policy=self.policy,
            identity=self.identity,
            credentials=self.credentials,
            credential_bindings=self.credential_bindings,
            artifacts=self.artifacts,
            settings=self.settings,
            retry=self.retry,
            sleep=self.sleep,
            approval_ttl=self.approval_ttl,
            now=self.now,
        )
        kernel.delegator = Delegator(
            kernel,
            agents=self.agents,
            resolver=self.resolver,
            max_tasks=self.max_tasks,
            run_task=lambda child: AgentLoop(kernel).run(child),
        )
        return kernel

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
            delegation=agent.delegation,
        )
        spec = TaskSpec(
            id=task_id,
            objective=objective,
            context=context or {},
            constraints=tuple(constraints),
            created_by=principal,
            deadline=deadline,
        )
        with self.locks.hold(run_id):
            kernel = self._kernel(RunState(run_id))
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
                ev.TaskCreated(
                    task=spec.model_dump(mode="json"), grant_id=grant.id, agent=agent.name
                ),
            )
            await kernel.recorder.emit(EventType.RUN_STARTED, ev.Empty())
            await self._execute(kernel, task)
            return _outcome(kernel.state)

    async def resume(self, run_id: str, *, principal: Principal) -> RunOutcome:
        """Continue a paused or crashed run from its event log.

        What happened, what was charged and what is still open all come from the log, not from
        memory or the model, and every call that runs again goes through the full pipeline.
        """
        with self.locks.hold(run_id):
            state = await load_state(self.store, run_id)
            if is_terminal_run(state.status):
                raise ResumeRefused(f"run {run_id} already {state.status.value}")
            kernel = self._kernel(state)
            task = await self._restore(state)
            view = state.tasks[task.task_id]
            now = self.now()

            waiting = [
                a
                for a in state.approvals.values()
                if a.status == "requested"
                and a.call_id not in state.tasks[a.task_id].ended
                and not a.expired(now)
            ]
            if waiting:
                # still up to a human; nothing to write
                return _outcome(state, approval_id=waiting[0].id)

            drafts: list[EventDraft] = []
            if state.status is RunStatus.RUNNING and state.active_since and state.last_ts:
                # The process died mid-run. Charge the time up to its last recorded event so a
                # crash can't hand the run a fresh wall-clock budget.
                elapsed = (state.last_ts - state.active_since).total_seconds()
                drafts.append(self._wall_draft(kernel, task, elapsed))
            drafts.append(
                kernel.draft(
                    task,
                    EventType.RUN_RESUMED,
                    ev.Resumed(
                        by=str(principal),
                        previous_status=state.status.value,
                        config_hash=self.config_hash,
                    ),
                )
            )
            for other in state.tasks.values():
                if other.status in (TaskStatus.AWAITING_APPROVAL, TaskStatus.BLOCKED):
                    drafts.append(_task_draft(kernel, other, EventType.TASK_RESUMED, ev.Empty()))
            await kernel.recorder.append(drafts)

            # Calls that were running when the process stopped, in any task. Safe ones (and
            # delegations, which find their existing child) get another go through the pipeline;
            # anything that may have changed the world waits for a person.
            for other in list(state.tasks.values()):
                for call_id, (action_hash, effect) in list(other.in_flight.items()):
                    ref = {"action_hash": action_hash}
                    if EffectClass(effect).safe_to_repeat:
                        payload: ev.ActionInterrupted | ev.ActionInDoubt = ev.ActionInterrupted(
                            call_id=call_id, action_hash=action_hash, effect=effect
                        )
                        kind = EventType.ACTION_INTERRUPTED
                    else:
                        payload = ev.ActionInDoubt(
                            call_id=call_id,
                            action_hash=action_hash,
                            effect=effect,
                            reason="the process stopped while it was running and no result "
                            "was recorded",
                        )
                        kind = EventType.ACTION_IN_DOUBT
                    await kernel.recorder.append([_task_draft(kernel, other, kind, payload, ref)])
            doubtful = _in_doubt(state)
            if doubtful:
                other, call_id = doubtful[0]
                await self._block(kernel, task, other, call_id, 0.0)
                return _outcome(state)

            if view.status is TaskStatus.COMPLETED:
                # crashed between finishing the task and closing the run
                await kernel.recorder.emit(
                    EventType.RUN_COMPLETED, ev.RunCompleted(output=view.output or "")
                )
                return _outcome(state)

            await self._execute(kernel, task)
            return _outcome(state)

    async def _restore(self, state: RunState) -> TaskRuntime:
        # Rebuild the task from what the log recorded when the run started, then check it
        # against today's configuration. Authority can shrink between runs, never grow.
        if state.agent_spec is None or state.grant is None or state.root_task_id is None:
            raise ResumeRefused(f"run {state.run_id} has no run.created event")
        try:
            agent = AgentSpec.model_validate(state.agent_spec)
            grant = Grant.model_validate(state.grant)
        except ValidationError as exc:
            raise ResumeRefused(f"recorded agent or grant is invalid: {exc}") from exc

        errors, _ = self.check(agent)
        wider = [str(c) for c in grant.capabilities if not c.is_within(self.grantable)]
        if wider:
            errors.append(f"configuration no longer grants {', '.join(sorted(wider))}")
        if errors:
            raise ResumeRefused("; ".join(errors))

        resolved = self.resolver.resolve(agent.model)
        if (resolved.binding.provider, resolved.binding.model) != (state.provider, state.model):
            raise ResumeRefused(
                f"run used {state.provider}/{state.model}, configuration now gives "
                f"{resolved.binding.provider}/{resolved.binding.model}"
            )

        view = state.tasks.get(state.root_task_id)
        if view is None:
            raise ResumeRefused(f"run {state.run_id} stopped before its task was created")
        deadline = view.spec.get("deadline")
        return TaskRuntime(
            run_id=state.run_id,
            task_id=view.id,
            parent_task_id=view.parent_id,
            agent=agent,
            grant=grant,
            identity=await self.identity.agent_identity(agent.name),
            model=resolved,
            deadline=datetime.fromisoformat(deadline) if deadline else None,
        )

    async def _execute(self, kernel: Kernel, task: TaskRuntime) -> None:
        limit = task.grant.budget.wall_seconds
        used = kernel.ledger(task).used(Dimension.WALL_SECONDS)
        timeout = None if limit is None else float(limit) - float(used)
        if task.deadline is not None:
            until_deadline = (task.deadline - self.now()).total_seconds()
            timeout = until_deadline if timeout is None else min(timeout, until_deadline)
        started = time.monotonic()
        kernel.stretch_started = started

        def elapsed() -> float:
            return time.monotonic() - started

        try:
            try:
                if timeout is not None and timeout <= 0:
                    raise TimeoutError
                async with asyncio.timeout(timeout):
                    await AgentLoop(kernel).run(task)
            except TimeoutError:
                await _mark_in_doubt(kernel, "run stopped by its time limit")
                deadline_hit = task.deadline is not None and self.now() >= task.deadline
                if limit is None or deadline_hit:
                    raise DeadlineExceeded(f"task {task.task_id} passed its deadline") from None
                exceeded = BudgetExceeded(
                    Dimension.WALL_SECONDS.value, limit, round(float(used) + elapsed(), 3)
                )
                await kernel.emit(task, EventType.BUDGET_EXCEEDED, exceeded_payload(task, exceeded))
                raise exceeded from None
        except LegionError as exc:
            if exc.disposition is Disposition.ESCALATE:
                await self._pause(kernel, task, exc, elapsed())
                return
            await _mark_in_doubt(kernel, exc.message)
            failure = ev.Failure(
                error_code=exc.code, message=exc.message, disposition=exc.disposition.value
            )
            await kernel.recorder.append(
                [
                    self._wall_draft(kernel, task, elapsed()),
                    *_close_children(kernel, EventType.TASK_FAILED, failure),
                    kernel.draft(task, EventType.TASK_FAILED, failure),
                    kernel.draft(task, EventType.RUN_FAILED, failure),
                ]
            )
            return
        except asyncio.CancelledError:
            await _mark_in_doubt(kernel, "run cancelled")
            cancelled = ev.Cancelled(reason="cancelled")
            await kernel.recorder.append(
                [
                    self._wall_draft(kernel, task, elapsed()),
                    *_close_children(kernel, EventType.TASK_CANCELLED, cancelled),
                    kernel.draft(task, EventType.TASK_CANCELLED, cancelled),
                    kernel.draft(task, EventType.RUN_CANCELLED, cancelled),
                ]
            )
            raise
        except Exception as exc:
            with contextlib.suppress(Exception):
                await _mark_in_doubt(kernel, f"internal error: {type(exc).__name__}")
            failure = ev.Failure(
                error_code="internal_error",
                message=f"{type(exc).__name__}: {exc}",
                disposition="fatal",
            )
            await kernel.recorder.append(
                [
                    *_close_children(kernel, EventType.TASK_FAILED, failure),
                    kernel.draft(task, EventType.TASK_FAILED, failure),
                    kernel.draft(task, EventType.RUN_FAILED, failure),
                ]
            )
            raise
        view = kernel.state.tasks[task.task_id]
        await kernel.recorder.append(
            [
                self._wall_draft(kernel, task, elapsed()),
                kernel.draft(
                    task, EventType.RUN_COMPLETED, ev.RunCompleted(output=view.output or "")
                ),
            ]
        )

    async def _pause(
        self, kernel: Kernel, task: TaskRuntime, exc: LegionError, spent: float
    ) -> None:
        # The task that needs a person may be a child; everything above it is already waiting.
        state = kernel.state
        if isinstance(exc, ApprovalRequired):
            owner = state.tasks[state.approvals[exc.approval_id].task_id]
            await kernel.recorder.append(
                [
                    self._wall_draft(kernel, task, spent),
                    _task_draft(
                        kernel,
                        owner,
                        EventType.TASK_AWAITING_APPROVAL,
                        ev.AwaitingApproval(approval_id=exc.approval_id),
                    ),
                    kernel.draft(
                        task,
                        EventType.RUN_PAUSED,
                        ev.Paused(reason="approval", approval_id=exc.approval_id),
                    ),
                ]
            )
            return
        if isinstance(exc, ActionInDoubt):
            doubtful = _in_doubt(state)
            if not doubtful:
                raise RuntimeError("in-doubt pause without an in-doubt action")
            owner, call_id = doubtful[-1]
            await self._block(kernel, task, owner, call_id, spent)
            return
        raise RuntimeError(f"no pause handling for {exc.code}")

    async def _block(
        self, kernel: Kernel, task: TaskRuntime, owner: TaskView, call_id: str, spent: float
    ) -> None:
        reason = "action may or may not have taken effect; an operator has to reconcile it"
        await kernel.recorder.append(
            [
                self._wall_draft(kernel, task, spent),
                _task_draft(
                    kernel,
                    owner,
                    EventType.TASK_BLOCKED,
                    ev.Blocked(call_id=call_id, reason=reason),
                ),
                kernel.draft(
                    task,
                    EventType.RUN_PAUSED,
                    ev.Paused(reason="reconciliation", call_id=call_id),
                ),
            ]
        )

    def _wall_draft(self, kernel: Kernel, task: TaskRuntime, seconds: float) -> EventDraft:
        amount = Decimal(str(round(max(seconds, 0.0), 3)))
        record, _ = kernel.ledger(task).charge(Dimension.WALL_SECONDS, amount)
        return kernel.draft(task, EventType.BUDGET_CONSUMED, record)


def _task_draft(
    kernel: Kernel,
    view: TaskView,
    kind: EventType,
    payload: Any,
    correlation: dict[str, str] | None = None,
) -> EventDraft:
    return kernel.recorder.draft(
        kind,
        payload,
        task_id=view.id,
        agent_id=view.agent,
        parent_task_id=view.parent_id,
        correlation=correlation,
    )


def _in_doubt(state: RunState) -> list[tuple[TaskView, str]]:
    return [
        (view, call_id)
        for view in state.tasks.values()
        if not is_terminal_task(view.status)
        for call_id in view.in_doubt
    ]


def _close_children(kernel: Kernel, kind: EventType, payload: Any) -> list[EventDraft]:
    # When the run ends, every child still open ends with it, deepest first.
    root = kernel.state.root_task_id
    return [
        _task_draft(kernel, view, kind, payload)
        for view in reversed(list(kernel.state.tasks.values()))
        if view.id != root and not is_terminal_task(view.status)
    ]


def _default_locks(store: EventStore) -> RunLocks:
    # A SQLite store can be shared by several processes, so it needs OS-level locks. In-process
    # locks are only enough for the in-memory store.
    if isinstance(store, SqliteEventStore) and str(store.path) != ":memory:":
        return FileRunLocks(store.path.parent / "locks")
    return InProcessRunLocks()


async def _mark_in_doubt(kernel: Kernel, reason: str) -> None:
    # Anything started and not finished, in any task, is in doubt. Delegations aren't: the child
    # is recorded, and its own calls are covered here.
    for view in list(kernel.state.tasks.values()):
        for call_id, (action_hash, effect) in list(view.in_flight.items()):
            if call_id in view.children:
                continue
            await kernel.recorder.append(
                [
                    _task_draft(
                        kernel,
                        view,
                        EventType.ACTION_IN_DOUBT,
                        ev.ActionInDoubt(
                            call_id=call_id, action_hash=action_hash, effect=effect, reason=reason
                        ),
                        {"action_hash": action_hash},
                    )
                ]
            )


def _outcome(state: RunState, approval_id: str | None = None) -> RunOutcome:
    assert state.root_task_id is not None
    view = state.tasks[state.root_task_id]
    paused = state.paused
    if approval_id is None and paused is not None:
        approval_id = paused.approval_id
    return RunOutcome(
        run_id=state.run_id,
        status=state.status,
        output=state.output,
        structured=view.structured,
        error_code=state.error.error_code if state.error else None,
        error_message=state.error.message if state.error else None,
        approval_id=approval_id,
        blocked_call=paused.call_id if paused is not None else None,
        in_doubt=tuple(call_id for v in state.tasks.values() for call_id in v.in_doubt),
    )
