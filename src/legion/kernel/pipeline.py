# Every tool call goes through ActionPipeline.execute, in this order:
#   kill check, lookup, schema + resource, repeat check, grant, policy, external authority,
#   approval (if policy asks for one), credentials, budget, run (timeout, retries), output check,
#   redaction. A resumed run goes through exactly the same steps.

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

from legion import schemas
from legion.access.secrets import Secret
from legion.authority.policy import PolicyContext, Verdict
from legion.canonical import canonical_json
from legion.domain.action import Action
from legion.domain.budget import Dimension
from legion.domain.capability import Capability
from legion.domain.errors import (
    ActionInDoubt,
    ActionRefused,
    ApprovalDenied,
    ApprovalExpired,
    ApprovalMismatch,
    ApprovalRequired,
    ApprovalReused,
    BudgetExceeded,
    CapabilityDenied,
    CredentialUnavailable,
    Disposition,
    GrantExpired,
    InvalidArguments,
    InvalidToolOutput,
    LegionError,
    LoopDetected,
    PolicyDenied,
    RepeatedAction,
    ToolFailed,
    ToolNotOffered,
    ToolTimeout,
    UnknownTool,
)
from legion.domain.messages import ToolCallPart
from legion.events import types as ev
from legion.events.types import EventType
from legion.kernel import approvals
from legion.kernel.delegation import ChildPlan, DelegateTool, DelegationRefused
from legion.kernel.services import Kernel, TaskRuntime, redact_text
from legion.tools.base import Tool, ToolContext, ToolResult

REPEAT_REFUSE_AT = 3
REPEAT_FATAL_AT = 5


class ActionPipeline:
    def __init__(self, kernel: Kernel) -> None:
        self.k = kernel

    async def execute(self, call: ToolCallPart, task: TaskRuntime) -> None:
        await self.k.check_kill(task)

        tool = self.k.tools.get(call.name)
        if tool is None:
            return await self._refuse(
                task, call, UnknownTool(f"there is no tool named {call.name}")
            )
        if call.name not in task.offered:
            return await self._refuse(
                task, call, ToolNotOffered(f"{call.name} is not available to this agent")
            )

        try:
            action = self._action(tool, call, task)
        except ActionRefused as exc:
            return await self._refuse(task, call, exc)

        await self.k.emit(
            task,
            EventType.ACTION_PROPOSED,
            ev.ActionProposed(
                call_id=call.id,
                tool=action.tool,
                arguments=action.arguments,
                action_hash=action.hash,
                effect=action.effect.value,
                resource=action.resource,
                required=[str(c) for c in action.required],
                remote=await self._remote(tool),
            ),
            correlation={"action_hash": action.hash},
        )

        count = self.k.state.tasks[task.task_id].repeats[action.repeat_key]
        if count >= REPEAT_REFUSE_AT:
            await self.k.emit(
                task,
                EventType.ACTION_REPEATED,
                ev.ActionRepeated(
                    call_id=call.id, tool=call.name, repeat_key=action.repeat_key, count=count
                ),
                correlation={"action_hash": action.hash},
            )
            if count >= REPEAT_FATAL_AT:
                raise LoopDetected(f"{call.name} called {count} times with identical arguments")
            return await self._refuse(
                task,
                call,
                RepeatedAction(
                    f"this exact call has been made {count} times; change approach or finish"
                ),
                action,
            )

        refusal, plan = await self._authorize(call, action, task, tool)
        if refusal is not None:
            return await self._refuse(task, call, refusal, action)

        if plan is not None:
            await self._delegate(tool, call, action, task, plan)
            return None
        secrets = await self._credentials(tool)
        await self._run(tool, call, action, task, secrets)
        return None

    async def _remote(self, tool: Tool) -> dict[str, Any] | None:
        # Where a remote tool runs and whose credential it uses there. Recorded for the audit
        # trail; none of it changes what the grant allows.
        origin = tool.spec.origin
        if origin is None:
            return None
        remote: dict[str, Any] = dict(origin)
        evidence = await self.k.identity.credential_evidence(origin.get("server", ""))
        if evidence is not None:
            remote["credential_evidence"] = {
                "source": evidence.source,
                "subject": evidence.subject,
                "scopes": list(evidence.scopes),
                "verified": evidence.verified,
            }
        return remote

    def _action(self, tool: Tool, call: ToolCallPart, task: TaskRuntime) -> Action:
        spec = tool.spec
        try:
            schemas.validate(call.arguments, spec.input_schema)
        except schemas.ValidationError as exc:
            where = "/".join(str(p) for p in exc.absolute_path) or "arguments"
            raise InvalidArguments(f"{where}: {exc.message}") from exc
        try:
            resource = tool.resource_of(call.arguments)
        except ActionRefused:
            raise
        except Exception as exc:
            # a buggy resource function shouldn't kill the run, and we can't check the call
            raise InvalidArguments(
                f"cannot determine what this call acts on ({type(exc).__name__})"
            ) from exc
        try:
            canonical_json(call.arguments)  # NaN etc. can't be hashed or logged
            action = Action(
                tool=spec.name,
                arguments=call.arguments,
                resource=resource,
                required=tuple(Capability(name=n, resource=resource) for n in spec.capabilities),
                effect=spec.effect,
                grant_id=task.grant.id,
                task_id=task.task_id,
            )
        except ValueError as exc:
            raise InvalidArguments(f"arguments cannot be checked: {exc}") from exc
        return action

    async def _authorize(
        self, call: ToolCallPart, action: Action, task: TaskRuntime, tool: Tool
    ) -> tuple[ActionRefused | None, ChildPlan | None]:
        if task.grant.expired(self.k.now()):
            raise GrantExpired(f"grant {task.grant.id} has expired")
        missing = [str(c) for c in action.required if not task.grant.covers(c)]
        if missing:
            return CapabilityDenied(f"the task's grant does not cover {', '.join(missing)}"), None

        decision = await self.k.policy.evaluate(
            action, PolicyContext(grant=task.grant, agent=task.agent.name, task_id=task.task_id)
        )
        if decision.verdict is Verdict.DENY:
            return PolicyDenied("; ".join(decision.reasons) or "denied by policy"), None

        external = await self.k.identity.authorize(action, task.identity)
        if not external.allowed:
            return PolicyDenied(f"{external.source}: {external.reason or 'denied'}"), None

        plan = None
        if isinstance(tool, DelegateTool):
            if self.k.delegator is None:
                return DelegationRefused("delegation isn't set up for this run"), None
            planned = self.k.delegator.plan(call, task)
            if isinstance(planned, ActionRefused):
                return planned, None
            plan = planned

        # Only ask a human once everything else has said yes.
        if decision.verdict is Verdict.REQUIRE_APPROVAL:
            refusal = await self._approval(call, action, task, tool)
            if refusal is not None:
                return refusal, None

        await self.k.emit(
            task,
            EventType.ACTION_AUTHORIZED,
            ev.ActionAuthorized(
                call_id=call.id,
                action_hash=action.hash,
                reasons=[*decision.reasons, f"{external.source}: {external.reason}"],
            ),
            correlation={"action_hash": action.hash},
        )
        return None, plan

    async def _delegate(
        self, tool: Tool, call: ToolCallPart, action: Action, task: TaskRuntime, plan: ChildPlan
    ) -> None:
        # Same bookkeeping as any tool call, but the "tool" is a child task run by the harness.
        # No timeout or retry here: the child has its own budget, and a pause or crash is
        # picked up again through the parent's call.
        assert self.k.delegator is not None
        if not plan.existing:
            # An identity service may veto the child. Ask before anything starts, so a veto is a
            # clean refusal and not a half-started call someone has to reconcile.
            try:
                await self.k.identity.on_delegation(task.grant, plan.grant)
            except LegionError as exc:
                return await self._refuse(task, call, DelegationRefused(exc.message), action)
            # Charged once, when the child is made. Picking an existing child back up after a
            # pause or crash is free; otherwise a child given everything the parent had left
            # could never be resumed.
            ledger = self.k.ledger(task)
            try:
                ledger.precheck(Dimension.TOOL_CALLS)
            except BudgetExceeded as exc:
                await self._exceeded(task, exc)
                raise
            record, _ = ledger.charge(Dimension.TOOL_CALLS, 1)
            await self.k.emit(task, EventType.BUDGET_CONSUMED, record)
        await self.k.check_kill(task)
        await self.k.emit(
            task,
            EventType.TOOL_STARTED,
            ev.ToolStarted(call_id=call.id, action_hash=action.hash, attempt=1),
            {"action_hash": action.hash},
        )
        started = time.monotonic()
        result = await self.k.delegator.run(call, task, plan)
        await self._complete(tool, task, call, action, result, {}, started, 1)

    async def _approval(
        self, call: ToolCallPart, action: Action, task: TaskRuntime, tool: Tool
    ) -> ActionRefused | None:
        view = self.k.state.tasks[task.task_id]
        bound = approvals.binding(
            task=task,
            call=call,
            action=action,
            tool=tool.spec,
            settings=self.k.settings,
            credential_refs={k: str(v) for k, v in self.k.credential_bindings.items()},
        )
        expected = approvals.binding_hash(bound)
        now = self.k.now()
        approval_id = view.approval_for_call.get(call.id)

        if approval_id is None:
            approval_id = approvals.new_approval_id()
            note = view.last_message.text if view.last_message else ""
            await self.k.emit(
                task,
                EventType.APPROVAL_REQUESTED,
                ev.ApprovalRequested(
                    approval_id=approval_id,
                    call_id=call.id,
                    action_hash=action.hash,
                    binding_hash=expected,
                    subject=approvals.subject(
                        task=task,
                        call=call,
                        action=action,
                        tool=tool.spec,
                        objective=str(view.spec.get("objective", "")),
                        model_note=note,
                    ),
                    expires_at=now + self.k.approval_ttl,
                ),
                {"action_hash": action.hash, "approval_id": approval_id},
            )
            raise ApprovalRequired(f"{action.tool} needs approval {approval_id}", approval_id)

        approval = self.k.state.approvals[approval_id]
        ref = {"action_hash": action.hash, "approval_id": approval_id}
        if approval.status in ("requested", "granted") and approval.expired(now):
            await self.k.emit(
                task, EventType.APPROVAL_EXPIRED, ev.ApprovalRef(approval_id=approval_id), ref
            )
            return ApprovalExpired(f"approval {approval_id} expired before it was used")
        if approval.status == "requested":
            raise ApprovalRequired(f"still waiting for approval {approval_id}", approval_id)
        if approval.status == "denied":
            note = f": {approval.note}" if approval.note else ""
            return ApprovalDenied(f"{approval.decided_by} denied this action{note}")
        if approval.status == "expired":
            return ApprovalExpired(f"approval {approval_id} expired before it was used")
        if approval.status == "invalidated":
            return ApprovalMismatch(f"approval {approval_id} no longer matches this action")

        # granted or consumed; either way it has to be for exactly this action
        if approval.binding_hash != expected:
            if approval.status == "granted":
                await self.k.emit(
                    task,
                    EventType.APPROVAL_INVALIDATED,
                    ev.ApprovalInvalidated(
                        approval_id=approval_id, reason="the action changed after approval"
                    ),
                    ref,
                )
            return ApprovalMismatch(f"approval {approval_id} does not match this action")
        if approval.status == "consumed":
            # A crash between consuming the approval and starting the tool must not cost the
            # approval, and a safe-to-repeat call interrupted mid-run may try again. Anything
            # else is a replay.
            same_call = approval.consumed_by == call.id
            if same_call and (call.id not in view.started or action.effect.safe_to_repeat):
                return None
            return ApprovalReused(f"approval {approval_id} has already been used")

        # Consume before running, so a crash after this point can't make the approval usable
        # for a second, different execution.
        await self.k.emit(
            task,
            EventType.APPROVAL_CONSUMED,
            ev.ApprovalConsumed(approval_id=approval_id, call_id=call.id),
            ref,
        )
        return None

    async def _credentials(self, tool: Tool) -> dict[str, Secret]:
        out = {}
        for name in tool.spec.credentials:
            ref = self.k.credential_bindings.get(name)
            if ref is None:
                raise CredentialUnavailable(
                    f"tool {tool.spec.name} needs credential {name!r}, which is not configured"
                )
            secret = await self.k.credentials.resolve(ref)
            self.k.recorder.remember(secret.reveal())
            out[name] = secret
        return out

    async def _run(
        self,
        tool: Tool,
        call: ToolCallPart,
        action: Action,
        task: TaskRuntime,
        secrets: dict[str, Secret],
    ) -> None:
        spec = tool.spec
        ledger = self.k.ledger(task)
        context = ToolContext(
            task_id=task.task_id,
            agent=task.agent.name,
            call_id=call.id,
            credentials=secrets,
            settings=self.k.settings,
            # same value on every retry and after a resume, so an idempotent API can dedupe
            idempotency_key=action.hash,
        )
        correlation = {"action_hash": action.hash}
        attempt = 0
        while True:
            attempt += 1
            try:
                ledger.precheck(Dimension.TOOL_CALLS)
            except BudgetExceeded as exc:
                await self._exceeded(task, exc)
                raise
            record, _ = ledger.charge(Dimension.TOOL_CALLS, 1)
            await self.k.emit(task, EventType.BUDGET_CONSUMED, record)
            await self.k.check_kill(task)
            await self.k.emit(
                task,
                EventType.TOOL_STARTED,
                ev.ToolStarted(call_id=call.id, action_hash=action.hash, attempt=attempt),
                correlation,
            )
            started = time.monotonic()
            error: LegionError | None = None
            result: ToolResult | None = None
            self.k.faults("tool:before_invoke")
            try:
                result = await asyncio.wait_for(
                    tool.invoke(dict(call.arguments), context), spec.timeout_s
                )
                self.k.faults("tool:after_invoke")
            except TimeoutError:
                error = ToolTimeout(f"{spec.name} did not finish within {spec.timeout_s}s")
            except ActionInDoubt as exc:
                error = exc
            except LegionError as exc:
                error = exc
            except Exception as exc:
                error = ToolFailed(f"{type(exc).__name__}: {exc}")

            if error is not None:
                message = self.k.recorder.redact(error.message)
                timed_out_write = isinstance(error, ToolTimeout) and not spec.effect.safe_to_repeat
                if isinstance(error, ActionInDoubt) or timed_out_write:
                    await self.k.emit(
                        task,
                        EventType.ACTION_IN_DOUBT,
                        ev.ActionInDoubt(
                            call_id=call.id,
                            action_hash=action.hash,
                            effect=spec.effect.value,
                            reason=message,
                        ),
                        correlation,
                    )
                    raise ActionInDoubt(
                        f"{spec.name} ({spec.effect.value}) may or may not have taken effect: "
                        f"{message}"
                    )
                retry = (
                    error.disposition is Disposition.RETRYABLE
                    and spec.effect.safe_to_repeat
                    and attempt < spec.max_attempts
                )
                disposition = error.disposition
                if disposition is Disposition.RETRYABLE and not retry:
                    disposition = Disposition.RECOVERABLE
                await self.k.emit(
                    task,
                    EventType.TOOL_FAILED,
                    ev.ToolFailed(
                        call_id=call.id,
                        action_hash=action.hash,
                        attempt=attempt,
                        error_code=error.code,
                        message=message,
                        disposition=disposition.value,
                        will_retry=retry,
                    ),
                    correlation,
                )
                if retry:
                    await self.k.sleep(self.k.retry.delay(attempt))
                    continue
                if disposition is Disposition.FATAL:
                    raise error
                return

            assert result is not None
            await self._complete(tool, task, call, action, result, secrets, started, attempt)
            return

    async def _complete(
        self,
        tool: Tool,
        task: TaskRuntime,
        call: ToolCallPart,
        action: Action,
        result: ToolResult,
        secrets: dict[str, Secret],
        started: float,
        attempt: int,
    ) -> None:
        output_schema = tool.spec.output_schema
        correlation = {"action_hash": action.hash}
        if output_schema is not None and not result.is_error:
            try:
                schemas.validate(result.data, output_schema)
            except schemas.ValidationError as exc:
                # The tool did run, so don't report a failure (the model might retry a write).
                # Leave out exc.message, it quotes the bad value.
                where = "/".join(str(p) for p in exc.absolute_path) or "output"
                result = ToolResult(
                    content=(
                        f"{action.tool} ran, but its output does not match its declared schema "
                        f"(at {where}), so it is withheld. The action itself has taken effect; "
                        f"do not repeat it on this basis. [{InvalidToolOutput.code}]"
                    ),
                    is_error=True,
                )

        content, redactions = redact_text(result.for_model(), self.k.recorder.secrets)
        artifact = None
        truncated = False
        limit = tool.spec.max_output_chars
        if len(content) > limit:
            artifact = self.k.artifacts.put(content.encode("utf-8"))
            content = (
                content[:limit]
                + f"\n[output truncated at {limit} characters; full output is artifact {artifact}]"
            )
            truncated = True
        await self.k.emit(
            task,
            EventType.TOOL_COMPLETED,
            ev.ToolCompleted(
                call_id=call.id,
                action_hash=action.hash,
                content=content,
                is_error=result.is_error,
                artifact=artifact,
                truncated=truncated,
                redactions=redactions,
                latency_ms=int((time.monotonic() - started) * 1000),
            ),
            correlation,
        )

    async def _refuse(
        self,
        task: TaskRuntime,
        call: ToolCallPart,
        error: ActionRefused,
        action: Action | None = None,
    ) -> None:
        await self.k.emit(
            task,
            EventType.ACTION_REFUSED,
            ev.ActionRefused(
                call_id=call.id,
                tool=call.name,
                action_hash=action.hash if action else None,
                reason_code=error.code,
                message=error.message,
            ),
            {"action_hash": action.hash} if action else None,
        )

    async def _exceeded(self, task: TaskRuntime, exc: BudgetExceeded) -> None:
        await self.k.emit(task, EventType.BUDGET_EXCEEDED, exceeded_payload(task, exc))


def exceeded_payload(task: TaskRuntime, exc: BudgetExceeded) -> ev.BudgetExceeded:
    return ev.BudgetExceeded(
        grant_id=task.grant.id,
        dimension=exc.dimension,
        limit=Decimal(str(exc.limit)),
        attempted=Decimal(str(exc.attempted)),
    )
