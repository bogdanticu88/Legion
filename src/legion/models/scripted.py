# Replays a fixed script. For tests and the examples, so nothing needs a model or a key.

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from legion.domain.agent import ModelFeature
from legion.domain.errors import ConfigError, ModelRequestRejected
from legion.domain.messages import Message, TextPart, ToolCallPart
from legion.events.types import Usage
from legion.models.base import ModelRequest, ModelResponse, StopReason

Step = ModelResponse | BaseException | Callable[[ModelRequest], ModelResponse]


def reply(text: str, *, usage: Usage | None = None) -> ModelResponse:
    return ModelResponse(
        message=Message(role="assistant", parts=(TextPart(text=text),)),
        stop_reason=StopReason.END,
        usage=usage or Usage(input_tokens=10, output_tokens=max(1, len(text) // 4)),
    )


def call(
    name: str, arguments: dict[str, Any] | None = None, *, id: str = "call_1", text: str = ""
) -> ModelResponse:
    return calls([(name, arguments or {})], ids=[id], text=text)


def calls(
    items: Sequence[tuple[str, dict[str, Any]]],
    *,
    ids: Sequence[str] | None = None,
    text: str = "",
) -> ModelResponse:
    ids = list(ids) if ids is not None else [f"call_{i + 1}" for i in range(len(items))]
    parts: list[TextPart | ToolCallPart] = [TextPart(text=text)] if text else []
    parts += [ToolCallPart(id=i, name=n, arguments=a) for i, (n, a) in zip(ids, items, strict=True)]
    return ModelResponse(
        message=Message(role="assistant", parts=tuple(parts)),
        stop_reason=StopReason.TOOL_USE,
        usage=Usage(input_tokens=10, output_tokens=5 * len(items)),
    )


class ScriptedProvider:
    kind = "scripted"
    supported = frozenset(ModelFeature) - {ModelFeature.STREAMING}

    def __init__(self, steps: Sequence[Step], *, by_turn: bool = False) -> None:
        # by_turn: pick the step from how many assistant turns the conversation already has,
        # instead of counting calls. Then a resumed run, in a new process, carries on where the
        # script left off rather than starting over.
        self._steps = list(steps)
        self._next = 0
        self.by_turn = by_turn
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.by_turn:
            self._next = sum(1 for m in request.messages if m.role == "assistant")
        if self._next >= len(self._steps):
            raise ModelRequestRejected("scripted provider has no more turns")
        step = self._steps[self._next]
        self._next += 1
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, ModelResponse):
            return step
        return step(request)

    async def aclose(self) -> None:
        return None

    @classmethod
    def from_yaml(cls, path: Path) -> ScriptedProvider:
        try:
            turns = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"cannot read script {path}: {exc}") from exc
        if not isinstance(turns, list):
            raise ConfigError(f"script {path} must be a list of turns")
        return cls([_turn(i, t) for i, t in enumerate(turns, start=1)], by_turn=True)


def _turn(index: int, turn: Any) -> ModelResponse:
    if not isinstance(turn, dict) or not ({"text", "tool_calls"} & turn.keys()):
        raise ConfigError(f"script turn {index} needs `text` or `tool_calls`")
    parts: list[TextPart | ToolCallPart] = []
    if turn.get("text"):
        parts.append(TextPart(text=str(turn["text"])))
    for n, tool_call in enumerate(turn.get("tool_calls") or [], start=1):
        parts.append(
            ToolCallPart(
                id=f"call_{index}_{n}",
                name=str(tool_call["name"]),
                arguments=dict(tool_call.get("arguments") or {}),
            )
        )
    usage = Usage.model_validate(turn.get("usage") or {"input_tokens": 50, "output_tokens": 20})
    has_calls = any(isinstance(p, ToolCallPart) for p in parts)
    return ModelResponse(
        message=Message(role="assistant", parts=tuple(parts)),
        stop_reason=StopReason.TOOL_USE if has_calls else StopReason.END,
        usage=usage,
    )
