from __future__ import annotations

from typing import Any

import httpx

from legion.access.base import AccessProvider, AuthScheme
from legion.domain.agent import ModelFeature
from legion.domain.errors import MalformedModelResponse
from legion.domain.messages import (
    Message,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from legion.events.types import Usage
from legion.models.base import ModelRequest, ModelResponse, StopReason, merge_options
from legion.models.http import post_json

API_VERSION = "2023-06-01"

_STOP = {
    "end_turn": StopReason.END,
    "stop_sequence": StopReason.END,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
}


class AnthropicProvider:
    kind = "anthropic"
    supported = frozenset({ModelFeature.TOOLS, ModelFeature.REASONING, ModelFeature.PROMPT_CACHING})

    def __init__(
        self,
        *,
        access: AccessProvider,
        base_url: str = "https://api.anthropic.com",
        timeout: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access = access
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._reserved = frozenset({"model", "messages", "system", "tools", "max_tokens"})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "messages": to_wire(request.messages),
        }
        if request.system:
            body["system"] = request.system
        if request.tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in request.tools
            ]
        body = merge_options(body, request.provider_options.get(self.kind, {}), self._reserved)
        headers = await self.access.headers(AuthScheme.X_API_KEY)
        headers["anthropic-version"] = API_VERSION
        data = await post_json(self._client, f"{self.base_url}/v1/messages", headers, body)
        return from_wire(data)


def to_wire(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    for message in messages:
        role = "assistant" if message.role == "assistant" else "user"
        blocks = [b for b in (_block(p) for p in message.parts) if b is not None]
        if not blocks:
            continue
        # the API wants alternating roles, so merge consecutive user/tool messages
        if wire and wire[-1]["role"] == role:
            wire[-1]["content"].extend(blocks)
        else:
            wire.append({"role": role, "content": blocks})
    return wire


def _block(part: Any) -> dict[str, Any] | None:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text} if part.text else None
    if isinstance(part, ToolCallPart):
        return {"type": "tool_use", "id": part.id, "name": part.name, "input": part.arguments}
    if isinstance(part, ToolResultPart):
        return {
            "type": "tool_result",
            "tool_use_id": part.call_id,
            "content": part.content,
            "is_error": part.is_error,
        }
    if isinstance(part, ReasoningPart) and part.provider == AnthropicProvider.kind:
        return dict(part.data)
    return None


def from_wire(data: dict[str, Any]) -> ModelResponse:
    content = data.get("content")
    if not isinstance(content, list):
        raise MalformedModelResponse("response has no content list")
    parts: list[TextPart | ToolCallPart | ReasoningPart] = []
    for block in content:
        if not isinstance(block, dict):
            raise MalformedModelResponse("content block is not an object")
        kind = block.get("type")
        if kind == "text":
            parts.append(TextPart(text=str(block.get("text", ""))))
        elif kind == "tool_use":
            if not isinstance(block.get("id"), str) or not isinstance(block.get("name"), str):
                raise MalformedModelResponse("tool_use block without id or name")
            arguments = block.get("input")
            if not isinstance(arguments, dict):
                raise MalformedModelResponse(f"input for {block['name']} is not an object")
            parts.append(ToolCallPart(id=block["id"], name=block["name"], arguments=arguments))
        else:
            parts.append(ReasoningPart(provider=AnthropicProvider.kind, data=block))

    stop_raw = data.get("stop_reason")
    stop = _STOP.get(stop_raw, StopReason.OTHER) if isinstance(stop_raw, str) else StopReason.OTHER
    raw_usage = data.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    return ModelResponse(
        message=Message(role="assistant", parts=tuple(parts)),
        stop_reason=stop,
        usage=Usage(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        ),
    )
