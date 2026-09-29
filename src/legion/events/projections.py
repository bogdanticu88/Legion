# Run state rebuilt from events. The runtime uses the same code on the live run.

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from legion.canonical import digest
from legion.domain.messages import Message, ToolResultPart, user_text
from legion.domain.states import RunStatus, TaskStatus, transition_run, transition_task
from legion.events.types import (
    ActionProposed,
    ActionReconciled,
    ActionRefused,
    ApprovalConsumed,
    ApprovalRequested,
    BudgetReserved,
    BudgetSettled,
    Event,
    EventType,
    Failure,
    ModelResponded,
    OutputRejected,
    Paused,
    RunCompleted,
    RunCreated,
    TaskCompleted,
    TaskCreated,
    ToolCompleted,
    ToolFailed,
)


def initial_prompt(task: dict[str, Any]) -> str:
    parts = [str(task["objective"])]
    if task.get("context"):
        parts.append("Context:\n" + json.dumps(task["context"], indent=2, sort_keys=True))
    if task.get("constraints"):
        parts.append("Constraints:\n" + "\n".join(f"- {c}" for c in task["constraints"]))
    return "\n\n".join(parts)


def rejection_prompt(reason: str) -> str:
    return (
        f"Your final answer was rejected: {reason}\n"
        "Reply with a final answer that satisfies the required output format."
    )


@dataclass
class TaskView:
    id: str
    agent: str
    grant_id: str
    spec: dict[str, Any]
    parent_id: str | None
    status: TaskStatus = TaskStatus.PENDING
    transcript: list[Message] = field(default_factory=list)
    output: str | None = None
    structured: dict[str, Any] | None = None
    error: Failure | None = None
    repeats: Counter[str] = field(default_factory=Counter)
    rejections: int = 0
    # call_id -> (action_hash, effect); in_flight = started but not finished
    proposed: dict[str, tuple[str, str]] = field(default_factory=dict)
    in_flight: dict[str, tuple[str, str]] = field(default_factory=dict)
    # in doubt and not yet reconciled by an operator
    in_doubt: dict[str, tuple[str, str]] = field(default_factory=dict)
    started: set[str] = field(default_factory=set)
    ended: set[str] = field(default_factory=set)
    approval_for_call: dict[str, str] = field(default_factory=dict)
    # the last model turn, so the loop (and resume) knows what is still open
    last_message: Message | None = None
    last_stop: str | None = None
    awaiting_finish: bool = False
    # delegation: set on child tasks, and the parent's call -> child mapping
    grant: dict[str, Any] | None = None
    agent_spec: dict[str, Any] | None = None
    provider: str | None = None
    model: str | None = None
    delegated_by: str | None = None
    children: dict[str, str] = field(default_factory=dict)
    settled: bool = False

    def open_calls(self) -> list[str]:
        if self.last_message is None:
            return []
        return [c.id for c in self.last_message.tool_calls if c.id not in self.ended]


@dataclass
class ApprovalView:
    id: str
    task_id: str
    call_id: str
    action_hash: str
    binding_hash: str
    subject: dict[str, Any]
    requested_at: datetime
    expires_at: datetime
    status: str = "requested"
    decided_by: str | None = None
    decided_at: datetime | None = None
    note: str = ""
    consumed_by: str | None = None

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


# which approval states each event may move from; anything else means a broken or forged log
_APPROVAL_MOVES = {
    EventType.APPROVAL_GRANTED: ({"requested"}, "granted"),
    EventType.APPROVAL_DENIED: ({"requested"}, "denied"),
    EventType.APPROVAL_EXPIRED: ({"requested", "granted"}, "expired"),
    EventType.APPROVAL_CONSUMED: ({"granted"}, "consumed"),
    EventType.APPROVAL_INVALIDATED: ({"granted"}, "invalidated"),
}


@dataclass
class RunState:
    run_id: str
    status: RunStatus = RunStatus.CREATED
    agent: str | None = None
    provider: str | None = None
    model: str | None = None
    root_task_id: str | None = None
    agent_spec: dict[str, Any] | None = None
    profile: str | None = None
    config_hash: str | None = None
    grant: dict[str, Any] | None = None
    output: str | None = None
    error: Failure | None = None
    tasks: dict[str, TaskView] = field(default_factory=dict)
    consumed: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    # held back for children still running, and what finished children actually used
    reserved: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    child_used: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    # (parent grant, child task, dimension) -> amount still held for that child
    reservations: dict[tuple[str, str, str], Decimal] = field(default_factory=dict)
    approvals: dict[str, ApprovalView] = field(default_factory=dict)
    paused: Paused | None = None
    # start of the current active stretch, for wall-clock accounting across pauses and crashes
    active_since: datetime | None = None
    last_ts: datetime | None = None
    last_seq: int = 0

    @classmethod
    def from_events(cls, run_id: str, events: list[Event]) -> RunState:
        state = cls(run_id)
        for event in events:
            state.apply(event)
        return state

    def used(self, grant_id: str, dimension: str) -> Decimal:
        # what this grant spent itself; see committed() for what its children hold
        return self.consumed.get((grant_id, dimension), Decimal(0))

    def committed(self, grant_id: str, dimension: str) -> Decimal:
        key = (grant_id, dimension)
        return self.reserved.get(key, Decimal(0)) + self.child_used.get(key, Decimal(0))

    def apply(self, event: Event) -> None:
        if event.run_id != self.run_id:
            raise ValueError("event belongs to another run")
        if event.seq != self.last_seq + 1:
            raise ValueError(f"expected seq {self.last_seq + 1}, got {event.seq}")
        self.last_seq = event.seq
        self.last_ts = event.ts
        handler = _HANDLERS.get(event.type)
        if handler is not None:
            handler(self, event)

    def task(self, event: Event) -> TaskView:
        if event.task_id is None or event.task_id not in self.tasks:
            raise ValueError(f"{event.type} refers to unknown task {event.task_id}")
        return self.tasks[event.task_id]


def _run_created(state: RunState, event: Event) -> None:
    p = RunCreated.model_validate(event.payload)
    state.agent, state.provider, state.model = p.agent, p.provider, p.model
    state.root_task_id, state.grant = p.root_task_id, p.grant
    state.agent_spec, state.profile, state.config_hash = p.agent_spec, p.profile, p.config_hash


def _run_status(target: RunStatus) -> Any:
    def handler(state: RunState, event: Event) -> None:
        state.status = transition_run(state.status, target)
        if target is RunStatus.RUNNING:
            state.active_since = event.ts
        elif target is RunStatus.PAUSED:
            state.paused = Paused.model_validate(event.payload)
            state.active_since = None
        if target is RunStatus.COMPLETED:
            state.output = RunCompleted.model_validate(event.payload).output
        elif target is RunStatus.FAILED:
            state.error = Failure.model_validate(event.payload)

    return handler


def _task_created(state: RunState, event: Event) -> None:
    p = TaskCreated.model_validate(event.payload)
    if event.task_id is None or event.task_id in state.tasks:
        raise ValueError("task.created needs a new task id")
    view = TaskView(event.task_id, p.agent, p.grant_id, p.task, event.parent_task_id)
    view.transcript.append(user_text(initial_prompt(p.task)))
    view.grant, view.agent_spec, view.delegated_by = p.grant, p.agent_spec, p.delegated_by
    view.provider, view.model = p.provider, p.model
    if event.parent_task_id is not None:
        parent = state.tasks.get(event.parent_task_id)
        if parent is None or p.delegated_by is None or p.grant is None:
            raise ValueError(f"child task {event.task_id} has no valid parent or grant")
        if p.delegated_by in parent.children:
            raise ValueError(f"call {p.delegated_by} already created a child")
        parent.children[p.delegated_by] = event.task_id
    state.tasks[event.task_id] = view


def _task_status(target: TaskStatus) -> Any:
    def handler(state: RunState, event: Event) -> None:
        view = state.task(event)
        view.status = transition_task(view.status, target)
        if target is TaskStatus.COMPLETED:
            p = TaskCompleted.model_validate(event.payload)
            view.output, view.structured = p.output, p.structured
            view.awaiting_finish = False
        elif target is TaskStatus.FAILED:
            view.error = Failure.model_validate(event.payload)

    return handler


def _run_resumed(state: RunState, event: Event) -> None:
    # a crashed run is still `running` in the log, so resuming it is not a status change
    if state.status is not RunStatus.RUNNING:
        state.status = transition_run(state.status, RunStatus.RUNNING)
    state.paused = None
    state.active_since = event.ts


def _model_responded(state: RunState, event: Event) -> None:
    p = ModelResponded.model_validate(event.payload)
    view = state.task(event)
    view.transcript.append(p.message)
    view.last_message = p.message
    view.last_stop = p.stop_reason
    view.awaiting_finish = not p.message.tool_calls


def _tool_result(state: RunState, event: Event, call_id: str, content: str, error: bool) -> None:
    part = ToolResultPart(call_id=call_id, content=content, is_error=error)
    state.task(event).transcript.append(Message(role="tool", parts=(part,)))


def _tool_completed(state: RunState, event: Event) -> None:
    p = ToolCompleted.model_validate(event.payload)
    _finished(state, event)
    state.task(event).ended.add(p.call_id)
    _tool_result(state, event, p.call_id, p.content, p.is_error)


def _tool_failed(state: RunState, event: Event) -> None:
    p = ToolFailed.model_validate(event.payload)
    _finished(state, event)
    if not p.will_retry:
        state.task(event).ended.add(p.call_id)
        _tool_result(state, event, p.call_id, f"Tool failed: {p.message}", True)


def _action_refused(state: RunState, event: Event) -> None:
    p = ActionRefused.model_validate(event.payload)
    state.task(event).ended.add(p.call_id)
    _tool_result(state, event, p.call_id, f"Refused ({p.reason_code}): {p.message}", True)


def _action_proposed(state: RunState, event: Event) -> None:
    p = ActionProposed.model_validate(event.payload)
    view = state.task(event)
    # a call proposed again after a resume is still one call
    if p.call_id not in view.proposed:
        view.repeats[digest({"tool": p.tool, "arguments": p.arguments})] += 1
    view.proposed[p.call_id] = (p.action_hash, p.effect)


def _tool_started(state: RunState, event: Event) -> None:
    view = state.task(event)
    call_id = str(event.payload["call_id"])
    if call_id not in view.proposed:
        raise ValueError(f"tool.started for unproposed call {call_id}")
    view.in_flight[call_id] = view.proposed[call_id]
    view.started.add(call_id)


def _finished(state: RunState, event: Event) -> None:
    state.task(event).in_flight.pop(str(event.payload["call_id"]), None)


def _in_doubt(state: RunState, event: Event) -> None:
    view = state.task(event)
    call_id = str(event.payload["call_id"])
    view.in_flight.pop(call_id, None)
    view.in_doubt[call_id] = (str(event.payload["action_hash"]), str(event.payload["effect"]))


def _reconciled(state: RunState, event: Event) -> None:
    p = ActionReconciled.model_validate(event.payload)
    view = state.task(event)
    if p.call_id not in view.in_doubt:
        raise ValueError(f"reconciliation for call {p.call_id}, which is not in doubt")
    del view.in_doubt[p.call_id]
    view.ended.add(p.call_id)
    note = f" Operator note: {p.note}" if p.note else ""
    if p.outcome == "applied":
        _tool_result(
            state, event, p.call_id, f"An operator confirmed this action took effect.{note}", False
        )
    else:
        _tool_result(
            state,
            event,
            p.call_id,
            f"An operator confirmed this action did not take effect.{note}",
            True,
        )


def _approval_requested(state: RunState, event: Event) -> None:
    p = ApprovalRequested.model_validate(event.payload)
    view = state.task(event)
    if p.approval_id in state.approvals or p.call_id not in view.proposed:
        raise ValueError(f"bad approval request {p.approval_id}")
    state.approvals[p.approval_id] = ApprovalView(
        id=p.approval_id,
        task_id=view.id,
        call_id=p.call_id,
        action_hash=p.action_hash,
        binding_hash=p.binding_hash,
        subject=p.subject,
        requested_at=event.ts,
        expires_at=p.expires_at,
    )
    view.approval_for_call[p.call_id] = p.approval_id


def _approval_moved(state: RunState, event: Event) -> None:
    approval_id = str(event.payload["approval_id"])
    approval = state.approvals.get(approval_id)
    allowed, target = _APPROVAL_MOVES[event.type]
    if approval is None or approval.status not in allowed:
        raise ValueError(f"{event.type} not allowed for approval {approval_id}")
    approval.status = target
    if event.type in (EventType.APPROVAL_GRANTED, EventType.APPROVAL_DENIED):
        approval.decided_by = str(event.payload["by"])
        approval.decided_at = event.ts
        approval.note = str(event.payload.get("note", ""))
    elif event.type is EventType.APPROVAL_CONSUMED:
        p = ApprovalConsumed.model_validate(event.payload)
        if p.call_id != approval.call_id:
            raise ValueError(f"approval {approval_id} consumed by the wrong call")
        approval.consumed_by = p.call_id


def _output_rejected(state: RunState, event: Event) -> None:
    p = OutputRejected.model_validate(event.payload)
    view = state.task(event)
    view.rejections += 1
    view.awaiting_finish = False
    view.transcript.append(user_text(rejection_prompt(p.reason)))


def _budget_reserved(state: RunState, event: Event) -> None:
    p = BudgetReserved.model_validate(event.payload)
    key = (p.grant_id, p.dimension)
    state.reserved[key] = state.reserved.get(key, Decimal(0)) + p.amount
    child_key = (p.grant_id, p.child_task_id, p.dimension)
    if child_key in state.reservations:
        raise ValueError(f"{p.dimension} already reserved for {p.child_task_id}")
    state.reservations[child_key] = p.amount


def _budget_settled(state: RunState, event: Event) -> None:
    p = BudgetSettled.model_validate(event.payload)
    key = (p.grant_id, p.dimension)
    held = state.reservations.pop((p.grant_id, p.child_task_id, p.dimension), None)
    if held is None or held != p.reserved:
        raise ValueError(f"settlement for {p.child_task_id} doesn't match its reservation")
    state.reserved[key] = state.reserved.get(key, Decimal(0)) - p.reserved
    state.child_used[key] = state.child_used.get(key, Decimal(0)) + p.used
    child = state.tasks.get(p.child_task_id)
    if child is None:
        raise ValueError(f"settlement for unknown child {p.child_task_id}")
    child.settled = True


def _budget_consumed(state: RunState, event: Event) -> None:
    grant_id = str(event.payload["grant_id"])
    dimension = str(event.payload["dimension"])
    state.consumed[(grant_id, dimension)] = Decimal(str(event.payload["total"]))


_HANDLERS: dict[EventType, Any] = {
    EventType.RUN_CREATED: _run_created,
    EventType.RUN_STARTED: _run_status(RunStatus.RUNNING),
    EventType.RUN_COMPLETED: _run_status(RunStatus.COMPLETED),
    EventType.RUN_FAILED: _run_status(RunStatus.FAILED),
    EventType.RUN_CANCELLED: _run_status(RunStatus.CANCELLED),
    EventType.RUN_PAUSED: _run_status(RunStatus.PAUSED),
    EventType.RUN_RESUMED: _run_resumed,
    EventType.TASK_CREATED: _task_created,
    EventType.TASK_STARTED: _task_status(TaskStatus.RUNNING),
    EventType.TASK_COMPLETED: _task_status(TaskStatus.COMPLETED),
    EventType.TASK_FAILED: _task_status(TaskStatus.FAILED),
    EventType.TASK_CANCELLED: _task_status(TaskStatus.CANCELLED),
    EventType.TASK_AWAITING_APPROVAL: _task_status(TaskStatus.AWAITING_APPROVAL),
    EventType.TASK_BLOCKED: _task_status(TaskStatus.BLOCKED),
    EventType.TASK_RESUMED: _task_status(TaskStatus.RUNNING),
    EventType.TASK_WAITING: _task_status(TaskStatus.WAITING_CHILDREN),
    EventType.BUDGET_RESERVED: _budget_reserved,
    EventType.BUDGET_SETTLED: _budget_settled,
    EventType.MODEL_RESPONDED: _model_responded,
    EventType.ACTION_PROPOSED: _action_proposed,
    EventType.ACTION_REFUSED: _action_refused,
    EventType.TOOL_STARTED: _tool_started,
    EventType.ACTION_IN_DOUBT: _in_doubt,
    EventType.ACTION_INTERRUPTED: _finished,
    EventType.ACTION_RECONCILED: _reconciled,
    EventType.APPROVAL_REQUESTED: _approval_requested,
    EventType.APPROVAL_GRANTED: _approval_moved,
    EventType.APPROVAL_DENIED: _approval_moved,
    EventType.APPROVAL_EXPIRED: _approval_moved,
    EventType.APPROVAL_CONSUMED: _approval_moved,
    EventType.APPROVAL_INVALIDATED: _approval_moved,
    EventType.TOOL_COMPLETED: _tool_completed,
    EventType.TOOL_FAILED: _tool_failed,
    EventType.OUTPUT_REJECTED: _output_rejected,
    EventType.BUDGET_CONSUMED: _budget_consumed,
}
