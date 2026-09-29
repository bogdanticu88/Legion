from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from legion.access.secrets import CredentialResolver, SecretRef
from legion.artifacts import ArtifactStore
from legion.authority.ledger import Ledger
from legion.authority.policy import PolicyDecisionPoint
from legion.domain.agent import AgentSpec
from legion.domain.errors import Killed
from legion.domain.grant import Grant
from legion.events.projections import RunState
from legion.events.store import EventStore
from legion.events.types import Event, EventType, _Payload, draft
from legion.models.resolver import Resolved
from legion.ports.identity import AgentIdentity, IdentityPort, KillState
from legion.tools.registry import ToolRegistry

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    """Deterministic exponential backoff. Every attempt is also charged to the budget."""

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0

    def delay(self, attempt: int, hint: float | None = None) -> float:
        if hint is not None:
            return min(hint, self.max_delay)
        return float(min(self.max_delay, self.base_delay * 2 ** (attempt - 1)))


@dataclass
class Recorder:
    """The one way to change run state: append an event, then apply it."""

    store: EventStore
    state: RunState

    async def emit(
        self,
        kind: EventType,
        payload: _Payload,
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        parent_task_id: str | None = None,
        correlation: dict[str, str] | None = None,
    ) -> Event:
        new = draft(
            self.state.run_id,
            kind,
            payload,
            task_id=task_id,
            agent_id=agent_id,
            parent_task_id=parent_task_id,
            correlation=correlation,
        )
        [event] = await self.store.append([new], expected_seq=self.state.last_seq)
        self.state.apply(event)
        return event


@dataclass
class TaskRuntime:
    """What the loop and the pipeline need to know about the task being executed."""

    run_id: str
    task_id: str
    parent_task_id: str | None
    agent: AgentSpec
    grant: Grant
    identity: AgentIdentity
    model: Resolved
    deadline: datetime | None = None

    @property
    def offered(self) -> frozenset[str]:
        return frozenset(self.agent.tools)


@dataclass
class Kernel:
    recorder: Recorder
    tools: ToolRegistry
    policy: PolicyDecisionPoint
    identity: IdentityPort
    credentials: CredentialResolver
    credential_bindings: Mapping[str, SecretRef]
    artifacts: ArtifactStore
    settings: Mapping[str, str]
    retry: RetryPolicy
    sleep: Sleep
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @property
    def state(self) -> RunState:
        return self.recorder.state

    def ledger(self, task: TaskRuntime) -> Ledger:
        return Ledger(task.grant, self.state)

    async def emit(
        self,
        task: TaskRuntime,
        kind: EventType,
        payload: _Payload,
        correlation: dict[str, str] | None = None,
    ) -> Event:
        return await self.recorder.emit(
            kind,
            payload,
            task_id=task.task_id,
            agent_id=task.agent.name,
            parent_task_id=task.parent_task_id,
            correlation=correlation,
        )

    async def check_kill(self, task: TaskRuntime) -> None:
        if await self.identity.kill_state(task.identity) is KillState.KILLED:
            raise Killed(f"{task.identity.source} reports agent {task.identity.agent_ref} killed")
