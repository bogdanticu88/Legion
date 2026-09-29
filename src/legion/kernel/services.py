from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

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

    def remember(self, value: str) -> None:
        if len(value) >= MIN_SECRET_LENGTH:
            self.secrets.add(value)

    def redact(self, text: str) -> str:
        return redact_text(text, self.secrets)[0]

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
        if self.secrets:
            new = new.model_copy(update={"payload": _redact_tree(new.payload, self.secrets)})
        # If we get cancelled mid-append the event may still be committed, and the state has to
        # know about it or every later append fails on expected_seq.
        pending = asyncio.ensure_future(self.store.append([new], expected_seq=self.state.last_seq))
        try:
            [event] = await asyncio.shield(pending)
        except asyncio.CancelledError:
            [event] = await pending
            self.state.apply(event)
            raise
        self.state.apply(event)
        return event


MIN_SECRET_LENGTH = 4


def redact_text(text: str, secrets: set[str] | list[str]) -> tuple[str, int]:
    # also catches the JSON-escaped form of each value
    count = 0
    for value in secrets:
        for form in {value, json.dumps(value)[1:-1]}:
            if form and form in text:
                count += text.count(form)
                text = text.replace(form, "[redacted]")
    return text, count


def _redact_tree(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)[0]
    if isinstance(value, dict):
        return {k: _redact_tree(v, secrets) for k, v in value.items()}
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
