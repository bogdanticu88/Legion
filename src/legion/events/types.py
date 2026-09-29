# Keep docs/events.md in sync with this file.

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from legion.domain.messages import Message

SCHEMA_VERSION = 1


class EventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_PAUSED = "run.paused"
    RUN_RESUMED = "run.resumed"
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    TASK_AWAITING_APPROVAL = "task.awaiting_approval"
    TASK_BLOCKED = "task.blocked"
    TASK_RESUMED = "task.resumed"
    TASK_WAITING = "task.waiting"
    MODEL_REQUESTED = "model.requested"
    MODEL_RESPONDED = "model.responded"
    MODEL_FAILED = "model.failed"
    ACTION_PROPOSED = "action.proposed"
    ACTION_REFUSED = "action.refused"
    ACTION_AUTHORIZED = "action.authorized"
    ACTION_REPEATED = "action.repeated"
    ACTION_IN_DOUBT = "action.in_doubt"
    ACTION_INTERRUPTED = "action.interrupted"
    ACTION_RECONCILED = "action.reconciled"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_DENIED = "approval.denied"
    APPROVAL_EXPIRED = "approval.expired"
    APPROVAL_CONSUMED = "approval.consumed"
    APPROVAL_INVALIDATED = "approval.invalidated"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    BUDGET_CONSUMED = "budget.consumed"
    BUDGET_EXCEEDED = "budget.exceeded"
    BUDGET_RESERVED = "budget.reserved"
    BUDGET_SETTLED = "budget.settled"
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
    # set for child tasks; a root task's grant and spec are in run.created
    grant: dict[str, Any] | None = None
    agent_spec: dict[str, Any] | None = None
    provider: str | None = None
    model: str | None = None
    delegated_by: str | None = None


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
    # negative counts would let a provider lower the recorded spend
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)

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


class Paused(_Payload):
    reason: Literal["approval", "reconciliation"]
    approval_id: str | None = None
    call_id: str | None = None


class Resumed(_Payload):
    by: str
    previous_status: str
    config_hash: str


class AwaitingApproval(_Payload):
    approval_id: str


class Blocked(_Payload):
    call_id: str
    reason: str


class ActionInterrupted(_Payload):
    call_id: str
    action_hash: str
    effect: str


class ActionReconciled(_Payload):
    call_id: str
    action_hash: str
    outcome: Literal["applied", "not_applied"]
    by: str
    note: str = ""


class ApprovalRequested(_Payload):
    approval_id: str
    call_id: str
    action_hash: str
    binding_hash: str
    # what the human is shown; the binding hash is what gets enforced
    subject: dict[str, Any]
    expires_at: datetime


class ApprovalDecided(_Payload):
    approval_id: str
    by: str
    note: str = ""


class ApprovalRef(_Payload):
    approval_id: str


class ApprovalConsumed(_Payload):
    approval_id: str
    call_id: str


class ApprovalInvalidated(_Payload):
    approval_id: str
    reason: str


class Waiting(_Payload):
    child_task_id: str


class BudgetReserved(_Payload):
    grant_id: str
    child_grant_id: str
    child_task_id: str
    dimension: str
    amount: Decimal


class BudgetSettled(_Payload):
    grant_id: str
    child_grant_id: str
    child_task_id: str
    dimension: str
    reserved: Decimal
    used: Decimal


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
    EventType.RUN_PAUSED: Paused,
    EventType.RUN_RESUMED: Resumed,
    EventType.TASK_AWAITING_APPROVAL: AwaitingApproval,
    EventType.TASK_BLOCKED: Blocked,
    EventType.TASK_RESUMED: Empty,
    EventType.TASK_WAITING: Waiting,
    EventType.BUDGET_RESERVED: BudgetReserved,
    EventType.BUDGET_SETTLED: BudgetSettled,
    EventType.ACTION_INTERRUPTED: ActionInterrupted,
    EventType.ACTION_RECONCILED: ActionReconciled,
    EventType.APPROVAL_REQUESTED: ApprovalRequested,
    EventType.APPROVAL_GRANTED: ApprovalDecided,
    EventType.APPROVAL_DENIED: ApprovalDecided,
    EventType.APPROVAL_EXPIRED: ApprovalRef,
    EventType.APPROVAL_CONSUMED: ApprovalConsumed,
    EventType.APPROVAL_INVALIDATED: ApprovalInvalidated,
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
