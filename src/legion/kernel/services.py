from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from legion.access.secrets import CredentialResolver
from legion.artifacts import ArtifactStore
from legion.authority.ledger import Ledger
from legion.authority.policy import PolicyDecisionPoint
from legion.domain.agent import AgentSpec
from legion.domain.errors import Killed
from legion.domain.grant import Grant
from legion.events.projections import RunState
from legion.events.store import EventStore
from legion.events.types import Event, EventDraft, EventType, _Payload, draft
from legion.kernel.credentials import CredentialBroker
from legion.models.resolver import Resolved
from legion.ports.identity import AgentIdentity, IdentityPort, KillState
from legion.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from legion.kernel.delegation import Delegator

Sleep = Callable[[float], Awaitable[None]]
# Called at named points ("before:tool.started", "tool:after_invoke", ...). Tests use it to stop
# a run exactly there, the way a crash would. Does nothing in normal use.
Faults = Callable[[str], None]


def no_faults(point: str) -> None:
    return None


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0

    def delay(self, attempt: int, hint: float | None = None) -> float:
        if hint is not None:
            return min(hint, self.max_delay)
        return float(min(self.max_delay, self.base_delay * 2 ** (attempt - 1)))


@dataclass
class Recorder:
    # All state changes go through emit(). Payloads are scrubbed of resolved secrets first.

    store: EventStore
    state: RunState
    secrets: set[str] = field(default_factory=set)
    faults: Faults = no_faults

    def remember(self, value: str) -> None:
        if len(value) >= MIN_SECRET_LENGTH:
            self.secrets.add(value)

    def redact(self, text: str) -> str:
        return redact_text(text, self.secrets)[0]

    def draft(
        self,
        kind: EventType,
        payload: _Payload,
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        parent_task_id: str | None = None,
        correlation: dict[str, str] | None = None,
    ) -> EventDraft:
        new = draft(
            self.state.run_id,
            kind,
            payload,
            task_id=task_id,
            agent_id=agent_id,
            parent_task_id=parent_task_id,
            correlation=correlation,
        )
        if self.secrets:
            new = new.model_copy(update={"payload": _redact_tree(new.payload, self.secrets)})
        return new

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
        new = self.draft(
            kind,
            payload,
            task_id=task_id,
            agent_id=agent_id,
            parent_task_id=parent_task_id,
            correlation=correlation,
        )
        [event] = await self.append([new])
        return event

    async def append(self, drafts: list[EventDraft]) -> list[Event]:
        """Write drafts as one atomic batch: either all of them are in the log or none are."""
        for d in drafts:
            self.faults(f"before:{d.type.value}")
        # If we get cancelled mid-append the events may still be committed, and the state has to
        # know about them or every later append fails on expected_seq.
        pending = asyncio.ensure_future(self.store.append(drafts, expected_seq=self.state.last_seq))
        try:
            events = await asyncio.shield(pending)
        except asyncio.CancelledError:
            for event in await pending:
                self.state.apply(event)
            raise
        for event in events:
            self.state.apply(event)
        for event in events:
            self.faults(f"after:{event.type.value}")
        return events


MIN_SECRET_LENGTH = 4


def redact_text(text: str, secrets: set[str] | list[str]) -> tuple[str, int]:
    # also catches the JSON-escaped form of each value
    count = 0
    # longest first, so a secret that contains another isn't left half replaced
    for value in sorted(secrets, key=len, reverse=True):
        for form in {value, json.dumps(value)[1:-1]}:
            if form and form in text:
                count += text.count(form)
                text = text.replace(form, "[redacted]")
    return text, count


def _redact_tree(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)[0]
    if isinstance(value, dict):
        return {_redact_tree(k, secrets): _redact_tree(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_tree(v, secrets) for v in value]
    return value


@dataclass
class TaskRuntime:
    run_id: str
    task_id: str
    parent_task_id: str | None
    agent: AgentSpec
    grant: Grant
    identity: AgentIdentity
    model: Resolved
    deadline: datetime | None = None
    # identities of every ancestor task, outermost first; empty for the root
    lineage: tuple[AgentIdentity, ...] = ()

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
    broker: CredentialBroker
    artifacts: ArtifactStore
    settings: Mapping[str, str]
    retry: RetryPolicy
    sleep: Sleep
    approval_ttl: timedelta = timedelta(hours=1)
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    delegator: Delegator | None = None
    # monotonic start of the current active stretch, for wall time that isn't recorded yet
    stretch_started: float | None = None

    def stretch_elapsed(self) -> float:
        return 0.0 if self.stretch_started is None else time.monotonic() - self.stretch_started

    @property
    def faults(self) -> Faults:
        return self.recorder.faults

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
        [event] = await self.recorder.append([self.draft(task, kind, payload, correlation)])
        return event

    def draft(
        self,
        task: TaskRuntime,
        kind: EventType,
        payload: _Payload,
        correlation: dict[str, str] | None = None,
    ) -> EventDraft:
        return self.recorder.draft(
            kind,
            payload,
            task_id=task.task_id,
            agent_id=task.agent.name,
            parent_task_id=task.parent_task_id,
            correlation=correlation,
        )

    async def check_kill(self, task: TaskRuntime) -> None:
        # a killed ancestor stops its whole subtree
        for identity in (*task.lineage, task.identity):
            if await self.identity.kill_state(identity) is KillState.KILLED:
                raise Killed(f"{identity.source} reports agent {identity.agent_ref} killed")
