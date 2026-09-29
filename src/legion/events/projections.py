"""Run state as a pure function of the event log.

The runtime applies every event it appends to a `RunState` and reads its transcript and budget
from there, so a run rebuilt from storage and the live run are produced by the same code.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from legion.canonical import digest
from legion.domain.messages import Message, ToolResultPart, user_text
from legion.domain.states import RunStatus, TaskStatus, transition_run, transition_task
from legion.events.types import (
    ActionProposed,
    ActionRefused,
    Event,
    EventType,
    Failure,
    ModelResponded,
    OutputRejected,
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
    # call_id -> (action_hash, effect). An entry in `in_flight` has started and not finished.
    proposed: dict[str, tuple[str, str]] = field(default_factory=dict)
    in_flight: dict[str, tuple[str, str]] = field(default_factory=dict)


@dataclass
class RunState:
    run_id: str
    status: RunStatus = RunStatus.CREATED
    agent: str | None = None
    provider: str | None = None
    model: str | None = None
    root_task_id: str | None = None
    grant: dict[str, Any] | None = None
    output: str | None = None
    error: Failure | None = None
    tasks: dict[str, TaskView] = field(default_factory=dict)
    consumed: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    last_seq: int = 0

    @classmethod
    def from_events(cls, run_id: str, events: list[Event]) -> RunState:
        state = cls(run_id)
        for event in events:
            state.apply(event)
        return state

    def used(self, grant_id: str, dimension: str) -> Decimal:
        return self.consumed.get((grant_id, dimension), Decimal(0))

    def apply(self, event: Event) -> None:
        if event.run_id != self.run_id:
            raise ValueError("event belongs to another run")
        if event.seq != self.last_seq + 1:
            raise ValueError(f"expected seq {self.last_seq + 1}, got {event.seq}")
        self.last_seq = event.seq
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


def _run_status(target: RunStatus) -> Any:
    def handler(state: RunState, event: Event) -> None:
        state.status = transition_run(state.status, target)
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
    state.tasks[event.task_id] = view


def _task_status(target: TaskStatus) -> Any:
    def handler(state: RunState, event: Event) -> None:
        view = state.task(event)
        view.status = transition_task(view.status, target)
        if target is TaskStatus.COMPLETED:
            p = TaskCompleted.model_validate(event.payload)
            view.output, view.structured = p.output, p.structured
        elif target is TaskStatus.FAILED:
            view.error = Failure.model_validate(event.payload)

    return handler


def _model_responded(state: RunState, event: Event) -> None:
    p = ModelResponded.model_validate(event.payload)
    state.task(event).transcript.append(p.message)


def _tool_result(state: RunState, event: Event, call_id: str, content: str, error: bool) -> None:
    part = ToolResultPart(call_id=call_id, content=content, is_error=error)
    state.task(event).transcript.append(Message(role="tool", parts=(part,)))


def _tool_completed(state: RunState, event: Event) -> None:
    p = ToolCompleted.model_validate(event.payload)
    _finished(state, event)
    _tool_result(state, event, p.call_id, p.content, p.is_error)


def _tool_failed(state: RunState, event: Event) -> None:
    p = ToolFailed.model_validate(event.payload)
    _finished(state, event)
    if not p.will_retry:
        _tool_result(state, event, p.call_id, f"Tool failed: {p.message}", True)


def _action_refused(state: RunState, event: Event) -> None:
    p = ActionRefused.model_validate(event.payload)
    _tool_result(state, event, p.call_id, f"Refused ({p.reason_code}): {p.message}", True)


def _action_proposed(state: RunState, event: Event) -> None:
    p = ActionProposed.model_validate(event.payload)
    view = state.task(event)
    view.repeats[digest({"tool": p.tool, "arguments": p.arguments})] += 1
    view.proposed[p.call_id] = (p.action_hash, p.effect)


def _tool_started(state: RunState, event: Event) -> None:
    view = state.task(event)
    call_id = str(event.payload["call_id"])
    if call_id not in view.proposed:
        raise ValueError(f"tool.started for unproposed call {call_id}")
    view.in_flight[call_id] = view.proposed[call_id]


def _finished(state: RunState, event: Event) -> None:
    state.task(event).in_flight.pop(str(event.payload["call_id"]), None)


def _output_rejected(state: RunState, event: Event) -> None:
    p = OutputRejected.model_validate(event.payload)
    view = state.task(event)
    view.rejections += 1
    view.transcript.append(user_text(rejection_prompt(p.reason)))


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
    EventType.TASK_CREATED: _task_created,
    EventType.TASK_STARTED: _task_status(TaskStatus.RUNNING),
    EventType.TASK_COMPLETED: _task_status(TaskStatus.COMPLETED),
    EventType.TASK_FAILED: _task_status(TaskStatus.FAILED),
    EventType.TASK_CANCELLED: _task_status(TaskStatus.CANCELLED),
    EventType.MODEL_RESPONDED: _model_responded,
    EventType.ACTION_PROPOSED: _action_proposed,
    EventType.ACTION_REFUSED: _action_refused,
    EventType.TOOL_STARTED: _tool_started,
    EventType.ACTION_IN_DOUBT: _finished,
    EventType.TOOL_COMPLETED: _tool_completed,
    EventType.TOOL_FAILED: _tool_failed,
    EventType.OUTPUT_REJECTED: _output_rejected,
    EventType.BUDGET_CONSUMED: _budget_consumed,
}
