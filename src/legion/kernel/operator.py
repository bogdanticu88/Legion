from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from legion.domain.errors import LegionError
from legion.domain.principal import Principal
from legion.domain.states import is_terminal_run
from legion.events import types as ev
from legion.events.projections import ApprovalView, RunState
from legion.events.store import EventStore
from legion.events.types import EventType
from legion.kernel.locks import RunLocks
from legion.kernel.runtime import load_state
from legion.kernel.services import Recorder


class OperatorError(LegionError):
    code = "operator_error"


@dataclass(frozen=True)
class Pending:
    run_id: str
    approval: ApprovalView
    expired: bool


def _now() -> datetime:
    return datetime.now(UTC)


async def approvals(store: EventStore, *, now: Callable[[], datetime] = _now) -> list[Pending]:
    """Approvals still waiting for a decision, across all runs whose logs verify."""
    out = []
    for summary in await store.runs():
        try:
            state = await load_state(store, summary.run_id)
        except LegionError:
            continue
        for approval in state.approvals.values():
            if approval.status == "requested":
                out.append(Pending(state.run_id, approval, approval.expired(now())))
    return out


async def find(store: EventStore, approval_id: str) -> tuple[RunState, ApprovalView]:
    for summary in await store.runs():
        try:
            state = await load_state(store, summary.run_id)
        except LegionError:
            continue
        if approval_id in state.approvals:
            return state, state.approvals[approval_id]
    raise OperatorError(f"no approval {approval_id}")


async def decide(
    store: EventStore,
    locks: RunLocks,
    approval_id: str,
    *,
    approve: bool,
    by: Principal,
    note: str = "",
    now: Callable[[], datetime] = _now,
) -> ApprovalView:
    state, _ = await find(store, approval_id)
    # Take the run's lock and re-read, so nothing can run while the decision is written.
    with locks.hold(state.run_id):
        state = await load_state(store, state.run_id)
        approval = state.approvals[approval_id]
        if is_terminal_run(state.status):
            raise OperatorError(f"run {state.run_id} already {state.status.value}")
        if approval.status != "requested":
            raise OperatorError(f"approval {approval_id} is {approval.status}, not waiting")
        recorder = Recorder(store, state)
        ref = {"action_hash": approval.action_hash, "approval_id": approval_id}
        if approval.expired(now()):
            await recorder.emit(
                EventType.APPROVAL_EXPIRED,
                ev.ApprovalRef(approval_id=approval_id),
                task_id=approval.task_id,
                agent_id=state.agent,
                correlation=ref,
            )
            raise OperatorError(f"approval {approval_id} expired at {approval.expires_at}")
        await recorder.emit(
            EventType.APPROVAL_GRANTED if approve else EventType.APPROVAL_DENIED,
            ev.ApprovalDecided(approval_id=approval_id, by=str(by), note=note),
            task_id=approval.task_id,
            agent_id=state.agent,
            correlation=ref,
        )
        return state.approvals[approval_id]


async def reconcile(
    store: EventStore,
    locks: RunLocks,
    run_id: str,
    call_id: str,
    *,
    outcome: Literal["applied", "not_applied", "abandon"],
    by: Principal,
    note: str = "",
) -> RunState:
    """Record what actually happened to an in-doubt action. Legion can't find this out itself."""
    with locks.hold(run_id):
        state = await load_state(store, run_id)
        if is_terminal_run(state.status):
            raise OperatorError(f"run {run_id} already {state.status.value}")
        assert state.root_task_id is not None
        view = state.tasks[state.root_task_id]
        if call_id not in view.in_doubt:
            raise OperatorError(f"call {call_id} is not in doubt in run {run_id}")
        action_hash, _ = view.in_doubt[call_id]
        recorder = Recorder(store, state)
        if outcome == "abandon":
            failure = ev.Failure(
                error_code="abandoned",
                message=f"{by} abandoned the run instead of reconciling call {call_id}",
                disposition="fatal",
            )
            await recorder.append(
                [
                    recorder.draft(
                        EventType.TASK_FAILED, failure, task_id=view.id, agent_id=state.agent
                    ),
                    recorder.draft(EventType.RUN_FAILED, failure),
                ]
            )
            return state
        await recorder.emit(
            EventType.ACTION_RECONCILED,
            ev.ActionReconciled(
                call_id=call_id, action_hash=action_hash, outcome=outcome, by=str(by), note=note
            ),
            task_id=view.id,
            agent_id=state.agent,
            correlation={"action_hash": action_hash},
        )
        return state
