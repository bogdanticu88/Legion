# Keep docs/events.md in sync with this file.

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from legion.domain.messages import Message

SCHEMA_VERSION = 1


class EventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    MODEL_REQUESTED = "model.requested"
    MODEL_RESPONDED = "model.responded"
    MODEL_FAILED = "model.failed"
    ACTION_PROPOSED = "action.proposed"
    ACTION_REFUSED = "action.refused"
    ACTION_AUTHORIZED = "action.authorized"
    ACTION_REPEATED = "action.repeated"
    ACTION_IN_DOUBT = "action.in_doubt"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    BUDGET_CONSUMED = "budget.consumed"
    BUDGET_EXCEEDED = "budget.exceeded"
    OUTPUT_REJECTED = "output.rejected"


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunCreated(_Payload):
    agent: str
    agent_spec: dict[str, Any]
    agent_spec_hash: str
    provider: str
    model: str
    profile: str
    grant: dict[str, Any]
    root_task_id: str
    config_hash: str


class Empty(_Payload):
    pass


class RunCompleted(_Payload):
    output: str


class Failure(_Payload):
    error_code: str
    message: str
    disposition: str


class Cancelled(_Payload):
    reason: str


class TaskCreated(_Payload):
    task: dict[str, Any]
    grant_id: str
    agent: str


class TaskCompleted(_Payload):
    output: str
    structured: dict[str, Any] | None = None
    truncated: bool = False


class ModelRequested(_Payload):
    attempt: int
    provider: str
    model: str
    message_count: int
    tool_names: list[str]
    request_hash: str
    max_output_tokens: int


class Usage(_Payload):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


class ModelResponded(_Payload):
    attempt: int
    message: Message
    stop_reason: str
    usage: Usage
    cost_usd: Decimal | None
    latency_ms: int


class ModelFailed(_Payload):
    attempt: int
    error_code: str
    message: str
    disposition: str
    will_retry: bool
    retry_in_ms: int | None = None


class ActionProposed(_Payload):
    call_id: str
    tool: str
    arguments: dict[str, Any]
    action_hash: str
    effect: str
    resource: str | None
    required: list[str]


# action_hash is None when the call was refused before it became an Action
class ActionRefused(_Payload):
    call_id: str
    tool: str
    action_hash: str | None
    reason_code: str
    message: str


class ActionAuthorized(_Payload):
    call_id: str
    action_hash: str
    reasons: list[str]


class ActionRepeated(_Payload):
    call_id: str
    tool: str
    repeat_key: str
    count: int


class ActionInDoubt(_Payload):
    call_id: str
    action_hash: str
    effect: str
    reason: str


class ToolStarted(_Payload):
    call_id: str
    action_hash: str
    attempt: int


class ToolCompleted(_Payload):
    call_id: str
    action_hash: str
    content: str
    is_error: bool = False
    artifact: str | None = None
    truncated: bool = False
    redactions: int = 0
    latency_ms: int


class ToolFailed(_Payload):
    call_id: str
    action_hash: str
    attempt: int
    error_code: str
    message: str
    disposition: str
    will_retry: bool


class BudgetConsumed(_Payload):
    grant_id: str
    dimension: str
    amount: Decimal
    total: Decimal


class BudgetExceeded(_Payload):
    grant_id: str
    dimension: str
    limit: Decimal
    attempted: Decimal


class OutputRejected(_Payload):
    attempt: int
    reason: str


PAYLOADS: dict[EventType, type[_Payload]] = {
    EventType.RUN_CREATED: RunCreated,
    EventType.RUN_STARTED: Empty,
    EventType.RUN_COMPLETED: RunCompleted,
    EventType.RUN_FAILED: Failure,
    EventType.RUN_CANCELLED: Cancelled,
    EventType.TASK_CREATED: TaskCreated,
    EventType.TASK_STARTED: Empty,
    EventType.TASK_COMPLETED: TaskCompleted,
    EventType.TASK_FAILED: Failure,
    EventType.TASK_CANCELLED: Cancelled,
    EventType.MODEL_REQUESTED: ModelRequested,
    EventType.MODEL_RESPONDED: ModelResponded,
    EventType.MODEL_FAILED: ModelFailed,
    EventType.ACTION_PROPOSED: ActionProposed,
    EventType.ACTION_REFUSED: ActionRefused,
    EventType.ACTION_AUTHORIZED: ActionAuthorized,
    EventType.ACTION_REPEATED: ActionRepeated,
    EventType.ACTION_IN_DOUBT: ActionInDoubt,
    EventType.TOOL_STARTED: ToolStarted,
    EventType.TOOL_COMPLETED: ToolCompleted,
    EventType.TOOL_FAILED: ToolFailed,
    EventType.BUDGET_CONSUMED: BudgetConsumed,
    EventType.BUDGET_EXCEEDED: BudgetExceeded,
    EventType.OUTPUT_REJECTED: OutputRejected,
}


# an event before the store assigns seq and hashes
class EventDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = SCHEMA_VERSION
    run_id: str
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    type: EventType
    task_id: str | None = None
    agent_id: str | None = None
    parent_task_id: str | None = None
    correlation: dict[str, str] = Field(default_factory=dict)
    payload: dict[str, Any]


class Event(EventDraft):
    seq: int
    prev_hash: str
    hash: str

    def body(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"hash"})

    def typed(self) -> Any:
        return PAYLOADS[self.type].model_validate(self.payload)


def draft(
    run_id: str,
    kind: EventType,
    payload: _Payload,
    *,
    task_id: str | None = None,
    agent_id: str | None = None,
    parent_task_id: str | None = None,
    correlation: dict[str, str] | None = None,
) -> EventDraft:
    expected = PAYLOADS[kind]
    if type(payload) is not expected:
        raise TypeError(f"{kind} needs {expected.__name__}, got {type(payload).__name__}")
    return EventDraft(
        run_id=run_id,
        type=kind,
        task_id=task_id,
        agent_id=agent_id,
        parent_task_id=parent_task_id,
        correlation=correlation or {},
        payload=payload.model_dump(mode="json"),
    )
