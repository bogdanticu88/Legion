# Chat Completions format. Ollama, vLLM, llama.cpp and most gateways speak it too.

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import httpx

from legion.access.base import AccessProvider, AuthScheme
from legion.domain.agent import ModelFeature
from legion.domain.errors import MalformedModelResponse
from legion.domain.messages import Message, TextPart, ToolCallPart, ToolResultPart
from legion.events.types import Usage
from legion.models.base import ModelRequest, ModelResponse, StopReason, merge_options
from legion.models.http import loads_strict, post_json

_STOP = {
    "stop": StopReason.END,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSAL,
}


class OpenAICompatProvider:
    kind = "openai_compat"
    supported = frozenset({ModelFeature.TOOLS, ModelFeature.STRUCTURED_OUTPUT})

    def __init__(
        self,
        *,
        base_url: str,
        access: AccessProvider,
        timeout: float = 120.0,
        output_limit_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access = access
        self.output_limit_field = output_limit_field
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._reserved = frozenset(
            {"model", "messages", "tools", "response_format", "max_tokens", "max_completion_tokens"}
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": to_wire(request),
            self.output_limit_field: request.max_output_tokens,
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in request.tools
            ]
        if request.response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "final_output", "schema": request.response_schema},
            }
        body = merge_options(body, request.provider_options.get(self.kind, {}), self._reserved)
        headers = await self.access.headers(AuthScheme.BEARER)
        data = await post_json(self._client, f"{self.base_url}/chat/completions", headers, body)
        return from_wire(data)


def to_wire(request: ModelRequest) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    if request.system:
        wire.append({"role": "system", "content": request.system})
    for message in request.messages:
        if message.role == "user":
            wire.append({"role": "user", "content": message.text})
        elif message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.text or None}
            calls = message.tool_calls
            if calls:
                entry["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in calls
                ]
            elif entry["content"] is None:
                entry["content"] = ""
            wire.append(entry)
        else:
            for part in message.parts:
                if isinstance(part, ToolResultPart):
                    wire.append(
                        {"role": "tool", "tool_call_id": part.call_id, "content": part.content}
                    )
    return wire


def from_wire(data: dict[str, Any]) -> ModelResponse:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise MalformedModelResponse("response has no choices")
    raw = choices[0].get("message")
    if not isinstance(raw, dict):
        raise MalformedModelResponse("choice has no message")

    parts: list[TextPart | ToolCallPart] = []
    content = raw.get("content")
    if isinstance(content, str) and content:
        parts.append(TextPart(text=content))
    for index, call in enumerate(raw.get("tool_calls") or [], start=1):
        parts.append(_tool_call(index, call))

    finish = choices[0].get("finish_reason")
    stop = _STOP.get(finish, StopReason.OTHER) if isinstance(finish, str) else StopReason.OTHER
    if any(isinstance(p, ToolCallPart) for p in parts):
        stop = StopReason.TOOL_USE
    return ModelResponse(
        message=Message(role="assistant", parts=tuple(parts)),
        stop_reason=stop,
        usage=_usage(data.get("usage")),
    )


def _tool_call(index: int, call: Any) -> ToolCallPart:
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        raise MalformedModelResponse("tool call without a function name")
    arguments = function.get("arguments") or "{}"
    if isinstance(arguments, str):
        try:
            arguments = loads_strict(arguments)
        except json.JSONDecodeError as exc:
            raise MalformedModelResponse(
                f"arguments for {function['name']} are not valid JSON"
            ) from exc
    if not isinstance(arguments, dict):
        raise MalformedModelResponse(f"arguments for {function['name']} are not an object")
    call_id = call.get("id") if isinstance(call.get("id"), str) and call.get("id") else None
    fallback = f"call_{uuid.uuid4().hex[:12]}_{index}"
    return ToolCallPart(id=call_id or fallback, name=function["name"], arguments=arguments)


def _usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    prompt = int(raw.get("prompt_tokens") or 0)
    details = raw.get("prompt_tokens_details")
    cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
    return Usage(
        input_tokens=max(0, prompt - cached),
        output_tokens=int(raw.get("completion_tokens") or 0),
        cache_read_tokens=cached,
    )
