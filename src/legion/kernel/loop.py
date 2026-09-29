from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import NoReturn

import jsonschema

from legion.canonical import canonical_json
from legion.domain.agent import ModelFeature
from legion.domain.budget import Dimension
from legion.domain.errors import (
    BudgetExceeded,
    DeadlineExceeded,
    Disposition,
    FinalOutputInvalid,
    GrantExpired,
    LegionError,
    MalformedModelResponse,
)
from legion.domain.messages import Message, Part, ToolCallPart
from legion.domain.states import TaskStatus
from legion.events import types as ev
from legion.events.types import EventType
from legion.kernel.pipeline import ActionPipeline, exceeded_payload
from legion.kernel.services import Kernel, TaskRuntime
from legion.models.base import ModelRequest, ModelResponse, StopReason

MAX_OUTPUT_REJECTIONS = 2


class AgentLoop:
    def __init__(self, kernel: Kernel) -> None:
        self.k = kernel
        self.pipeline = ActionPipeline(kernel)

    async def run(self, task: TaskRuntime) -> None:
        if self.k.state.tasks[task.task_id].status is TaskStatus.PENDING:
            await self.k.emit(task, EventType.TASK_STARTED, ev.Empty())
        while True:
            if await self._settle(task):
                return
            await self._step(task)
            await self._call_model(task, self._request(task))

    async def _settle(self, task: TaskRuntime) -> bool:
        # Finish whatever the last model turn left open. After a fresh response that's all of it;
        # after a resume it's whatever the log says hasn't ended yet. Returns True when the task
        # is complete.
        view = self.k.state.tasks[task.task_id]
        message = view.last_message
        if message is None:
            return False
        if view.awaiting_finish:
            return await self._finish(task, message.text, view.last_stop)
        # sequential for now; parallel calls come with the Phase 3 scheduler
        for call in message.tool_calls:
            if call.id in view.ended:
                continue
            await self.pipeline.execute(call, task)
            if call.id not in view.ended:
                raise RuntimeError(f"pipeline returned without ending call {call.id}")
        return False

    async def _step(self, task: TaskRuntime) -> None:
        now = self.k.now()
        if task.deadline is not None and now >= task.deadline:
            raise DeadlineExceeded(f"task {task.task_id} passed its deadline")
        if task.grant.expired(now):
            raise GrantExpired(f"grant {task.grant.id} has expired")
        await self._charge_before(task, Dimension.STEPS)

    def _request(self, task: TaskRuntime) -> ModelRequest:
        agent = task.agent
        view = self.k.state.tasks[task.task_id]
        system = agent.instructions
        schema = agent.output_schema
        if schema is not None:
            system += (
                "\n\nWhen you give your final answer, reply with only a JSON object that matches "
                "this schema:\n" + json.dumps(schema, sort_keys=True)
            )
        max_tokens = agent.max_output_tokens
        remaining = self.k.ledger(task).remaining(Dimension.TOKENS)
        if remaining is not None:
            # don't let one answer blow through the rest of the token budget
            max_tokens = max(1, min(max_tokens, int(remaining)))
        features = task.model.features
        return ModelRequest(
            model=task.model.binding.model,
            system=system,
            messages=tuple(view.transcript),
            tools=self.k.tools.definitions(agent.tools),
            max_output_tokens=max_tokens,
            response_schema=schema if ModelFeature.STRUCTURED_OUTPUT in features else None,
            provider_options=task.model.binding.options,
        )

    async def _call_model(self, task: TaskRuntime, request: ModelRequest) -> ModelResponse:
        binding = task.model.binding
        attempt = 0
        while True:
            attempt += 1
            for dimension in (Dimension.TOKENS, Dimension.COST_USD):
                remaining = self.k.ledger(task).remaining(dimension)
                if remaining is not None and remaining <= 0:
                    limit = task.grant.budget.limit(dimension)
                    await self._exceeded(
                        task,
                        BudgetExceeded(dimension.value, limit, self.k.ledger(task).used(dimension)),
                    )
            await self._charge_before(task, Dimension.MODEL_CALLS)
            await self.k.check_kill(task)
            await self.k.emit(
                task,
                EventType.MODEL_REQUESTED,
                ev.ModelRequested(
                    attempt=attempt,
                    provider=binding.provider,
                    model=binding.model,
                    message_count=len(request.messages),
                    tool_names=[t.name for t in request.tools],
                    request_hash=request.request_hash,
                    max_output_tokens=request.max_output_tokens,
                ),
            )
            started = time.monotonic()
            self.k.faults("model:before_call")
            try:
                response = await task.model.provider.generate(request)
                try:
                    canonical_json(response.message.model_dump(mode="json"))
                except ValueError as exc:
                    raise MalformedModelResponse(f"response cannot be recorded: {exc}") from exc
                if not response.message.text.strip() and not response.message.tool_calls:
                    # seen with small local models; an empty turn isn't an answer
                    raise MalformedModelResponse("model returned an empty response")
            except LegionError as exc:
                retry = exc.disposition is Disposition.RETRYABLE and (
                    attempt < self.k.retry.max_attempts
                )
                delay = self.k.retry.delay(attempt, getattr(exc, "retry_after", None))
                await self.k.emit(
                    task,
                    EventType.MODEL_FAILED,
                    ev.ModelFailed(
                        attempt=attempt,
                        error_code=exc.code,
                        message=exc.message,
                        disposition=exc.disposition.value,
                        will_retry=retry,
                        retry_in_ms=int(delay * 1000) if retry else None,
                    ),
                )
                if retry:
                    await self.k.sleep(delay)
                    continue
                raise
            self.k.faults("model:after_response")
            response = _unique_call_ids(response, self.k.state.tasks[task.task_id].transcript)
            cost = binding.pricing.cost(response.usage) if binding.pricing else None
            # The response and what it cost go into the log together, so a crash can't leave a
            # recorded response whose tokens were never charged.
            ledger = self.k.ledger(task)
            charges = [(Dimension.TOKENS, Decimal(response.usage.total))]
            if cost is not None:
                charges.append((Dimension.COST_USD, cost))
            drafts = [
                self.k.draft(
                    task,
                    EventType.MODEL_RESPONDED,
                    ev.ModelResponded(
                        attempt=attempt,
                        message=response.message,
                        stop_reason=response.stop_reason.value,
                        usage=response.usage,
                        cost_usd=cost,
                        latency_ms=int((time.monotonic() - started) * 1000),
                    ),
                )
            ]
            over = []
            for dimension, amount in charges:
                record, exceeded = ledger.charge(dimension, amount)
                drafts.append(self.k.draft(task, EventType.BUDGET_CONSUMED, record))
                if exceeded:
                    over.append((dimension, record.total))
            await self.k.recorder.append(drafts)
            for dimension, total in over:
                limit = task.grant.budget.limit(dimension)
                await self._exceeded(task, BudgetExceeded(dimension.value, limit, total))
            return response

    async def _finish(self, task: TaskRuntime, text: str, stop_reason: str | None) -> bool:
        schema = task.agent.output_schema
        structured = None
        if schema is not None:
            try:
                structured = json.loads(_strip_fence(text))
                if not isinstance(structured, dict):
                    raise ValueError("final answer is not a JSON object")
                jsonschema.validate(structured, schema)
            except (ValueError, jsonschema.ValidationError) as exc:
                reason = exc.message if isinstance(exc, jsonschema.ValidationError) else str(exc)
                view = self.k.state.tasks[task.task_id]
                if view.rejections >= MAX_OUTPUT_REJECTIONS:
                    raise FinalOutputInvalid(f"final answer still invalid: {reason}") from exc
                await self.k.emit(
                    task,
                    EventType.OUTPUT_REJECTED,
                    ev.OutputRejected(attempt=view.rejections + 1, reason=reason),
                )
                return False
        await self.k.emit(
            task,
            EventType.TASK_COMPLETED,
            ev.TaskCompleted(
                output=text,
                structured=structured,
                truncated=stop_reason == StopReason.MAX_TOKENS.value,
            ),
        )
        return True

    async def _charge_before(self, task: TaskRuntime, dimension: Dimension) -> None:
        ledger = self.k.ledger(task)
        try:
            ledger.precheck(dimension)
        except BudgetExceeded as exc:
            await self._exceeded(task, exc)
        record, _ = ledger.charge(dimension, 1)
        await self.k.emit(task, EventType.BUDGET_CONSUMED, record)

    async def _exceeded(self, task: TaskRuntime, exc: BudgetExceeded) -> NoReturn:
        await self.k.emit(task, EventType.BUDGET_EXCEEDED, exceeded_payload(task, exc))
        raise exc


def _unique_call_ids(response: ModelResponse, transcript: list[Message]) -> ModelResponse:
    # Everything about a call (approval, in-flight, in doubt) is keyed by its id, and some servers
    # reuse ids across turns. Rename clashes before the response is recorded; the model only ever
    # sees the recorded version, so the rename is consistent from then on.
    seen = {c.id for m in transcript for c in m.tool_calls}
    parts: list[Part] = []
    changed = False
    for part in response.message.parts:
        if isinstance(part, ToolCallPart):
            new_id, n = part.id, 1
            while new_id in seen:
                n += 1
                new_id = f"{part.id}_{n}"
            seen.add(new_id)
            if new_id != part.id:
                part = part.model_copy(update={"id": new_id})
                changed = True
        parts.append(part)
    if not changed:
        return response
    message = response.message.model_copy(update={"parts": tuple(parts)})
    return response.model_copy(update={"message": message})


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()
