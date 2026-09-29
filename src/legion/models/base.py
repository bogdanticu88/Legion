from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from legion.canonical import digest
from legion.domain.agent import ModelFeature
from legion.domain.errors import ConfigError
from legion.domain.messages import Message
from legion.events.types import Usage


class StopReason(StrEnum):
    END = "end"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    OTHER = "other"


class ToolDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any]


class ModelRequest(BaseModel):
    # provider_options: {kind: {...}}, each adapter only reads its own key
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()
    max_output_tokens: int = Field(gt=0)
    response_schema: dict[str, Any] | None = None
    provider_options: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @property
    def request_hash(self) -> str:
        return digest(self.model_dump(mode="json"))


class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message: Message
    stop_reason: StopReason
    usage: Usage


class ModelProvider(Protocol):
    kind: str
    supported: frozenset[ModelFeature]

    async def generate(self, request: ModelRequest) -> ModelResponse: ...

    async def aclose(self) -> None: ...


def merge_options(
    body: dict[str, Any], options: dict[str, Any], reserved: frozenset[str]
) -> dict[str, Any]:
    clash = reserved & options.keys()
    if clash:
        raise ConfigError(f"provider_options may not set {sorted(clash)}")
    return {**body, **options}
