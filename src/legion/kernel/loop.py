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
        await self.k.emit(task, EventType.TASK_STARTED, ev.Empty())
        while True:
            await self._step(task)
            response = await self._call_model(task, self._request(task))
            calls = response.message.tool_calls
            if calls:
                # sequential for now; parallel calls come with the Phase 3 scheduler
                for call in calls:
                    await self.pipeline.execute(call, task)
                continue
            if await self._finish(task, response):
                return

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
            try:
                response = await task.model.provider.generate(request)
                try:
                    canonical_json(response.message.model_dump(mode="json"))
                except ValueError as exc:
                    raise MalformedModelResponse(f"response cannot be recorded: {exc}") from exc
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
            cost = binding.pricing.cost(response.usage) if binding.pricing else None
            await self.k.emit(
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
            await self._charge_after(task, Dimension.TOKENS, Decimal(response.usage.total))
            if cost is not None:
                await self._charge_after(task, Dimension.COST_USD, cost)
            return response

    async def _finish(self, task: TaskRuntime, response: ModelResponse) -> bool:
        text = response.message.text
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
                truncated=response.stop_reason is StopReason.MAX_TOKENS,
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

    async def _charge_after(self, task: TaskRuntime, dimension: Dimension, amount: Decimal) -> None:
        record, over = self.k.ledger(task).charge(dimension, amount)
        await self.k.emit(task, EventType.BUDGET_CONSUMED, record)
        if over:
            await self._exceeded(
                task,
                BudgetExceeded(dimension.value, task.grant.budget.limit(dimension), record.total),
            )

    async def _exceeded(self, task: TaskRuntime, exc: BudgetExceeded) -> NoReturn:
        await self.k.emit(task, EventType.BUDGET_EXCEEDED, exceeded_payload(task, exc))
        raise exc


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()
