"""The only code path from a model's tool call to an effect.

Order is fixed and there are no hooks between steps:

     1 kill check           6 budget
     2 lookup + offered     7 credentials
     3 schema + resource    8 execute (timeout, bounded retry by effect class)
     4 repeat detection     9 output check, redaction, size limit
     5 grant, policy, external authority          10 events at every step

Refusals before execution are recoverable: they are recorded and the model is told. Anything
fatal is raised to the loop after being recorded.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import jsonschema

from legion.access.secrets import Secret
from legion.authority.policy import PolicyContext, Verdict
from legion.domain.action import Action
from legion.domain.budget import Dimension
from legion.domain.capability import Capability
from legion.domain.errors import (
    ActionInDoubt,
    ActionRefused,
    ApprovalUnavailable,
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
from legion.kernel.services import Kernel, TaskRuntime
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

        refusal = await self._authorize(call, action, task)
        if refusal is not None:
            return await self._refuse(task, call, refusal, action)

        secrets = await self._credentials(tool)
        await self._run(tool, call, action, task, secrets)
        return None

    def _action(self, tool: Tool, call: ToolCallPart, task: TaskRuntime) -> Action:
        spec = tool.spec
        try:
            jsonschema.validate(call.arguments, spec.input_schema)
        except jsonschema.ValidationError as exc:
            where = "/".join(str(p) for p in exc.absolute_path) or "arguments"
            raise InvalidArguments(f"{where}: {exc.message}") from exc
        try:
            resource = tool.resource_of(call.arguments)
        except ActionRefused:
            raise
        except Exception as exc:
            # If we cannot tell what the call touches, we cannot check it. Refuse, do not crash.
            raise InvalidArguments(
                f"cannot determine what this call acts on ({type(exc).__name__})"
            ) from exc
        return Action(
            tool=spec.name,
            arguments=call.arguments,
            resource=resource,
            required=tuple(Capability(name=n, resource=resource) for n in spec.capabilities),
            effect=spec.effect,
            grant_id=task.grant.id,
            task_id=task.task_id,
        )

    async def _authorize(
        self, call: ToolCallPart, action: Action, task: TaskRuntime
    ) -> ActionRefused | None:
        if task.grant.expired(self.k.now()):
            raise GrantExpired(f"grant {task.grant.id} has expired")
        missing = [str(c) for c in action.required if not task.grant.covers(c)]
        if missing:
            return CapabilityDenied(f"the task's grant does not cover {', '.join(missing)}")

        decision = await self.k.policy.evaluate(
            action, PolicyContext(grant=task.grant, agent=task.agent.name, task_id=task.task_id)
        )
        if decision.verdict is Verdict.DENY:
            return PolicyDenied("; ".join(decision.reasons) or "denied by policy")
        if decision.verdict is Verdict.REQUIRE_APPROVAL:
            return ApprovalUnavailable("this action needs human approval, which is not available")

        external = await self.k.identity.authorize(action, task.identity)
        if not external.allowed:
            return PolicyDenied(f"{external.source}: {external.reason or 'denied'}")

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
        return None

    async def _credentials(self, tool: Tool) -> dict[str, Secret]:
        out = {}
        for name in tool.spec.credentials:
            ref = self.k.credential_bindings.get(name)
            if ref is None:
                raise CredentialUnavailable(
                    f"tool {tool.spec.name} needs credential {name!r}, which is not configured"
                )
            out[name] = await self.k.credentials.resolve(ref)
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
            try:
                result = await asyncio.wait_for(
                    tool.invoke(dict(call.arguments), context), spec.timeout_s
                )
            except TimeoutError:
                error = ToolTimeout(f"{spec.name} did not finish within {spec.timeout_s}s")
            except ActionInDoubt as exc:
                error = exc
            except LegionError as exc:
                error = exc
            except Exception as exc:
                error = ToolFailed(f"{type(exc).__name__}: {exc}")

            if error is not None:
                message = _redact(error.message, secrets)[0]
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
                jsonschema.validate(result.data, output_schema)
            except jsonschema.ValidationError as exc:
                bad = InvalidToolOutput(
                    f"{action.tool} returned output that fails its schema: {exc.message}"
                )
                await self.k.emit(
                    task,
                    EventType.TOOL_FAILED,
                    ev.ToolFailed(
                        call_id=call.id,
                        action_hash=action.hash,
                        attempt=attempt,
                        error_code=bad.code,
                        message=bad.message,
                        disposition=bad.disposition.value,
                        will_retry=False,
                    ),
                    correlation,
                )
                return

        content, redactions = _redact(result.for_model(), secrets)
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


def _redact(text: str, secrets: dict[str, Secret]) -> tuple[str, int]:
    count = 0
    for secret in secrets.values():
        value = secret.reveal()
        if len(value) >= 4 and value in text:
            count += text.count(value)
            text = text.replace(value, "[redacted]")
    return text, count
