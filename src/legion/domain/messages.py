"""Provider-neutral conversation parts. Adapters translate these to and from each wire format."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TextPart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["text"] = "text"
    text: str


class ToolCallPart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any]


class ToolResultPart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    content: str
    is_error: bool = False


class ReasoningPart(BaseModel):
    """Opaque reasoning data. Only ever sent back to the provider that produced it."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["reasoning"] = "reasoning"
    provider: str
    data: dict[str, Any]


Part = Annotated[
    TextPart | ToolCallPart | ToolResultPart | ReasoningPart, Field(discriminator="type")
]


class Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user", "assistant", "tool"]
    parts: tuple[Part, ...]

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))

    @property
    def tool_calls(self) -> list[ToolCallPart]:
        return [p for p in self.parts if isinstance(p, ToolCallPart)]


def user_text(text: str) -> Message:
    return Message(role="user", parts=(TextPart(text=text),))
