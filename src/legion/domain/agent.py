from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from legion.canonical import digest
from legion.domain.budget import BudgetLimits
from legion.domain.capability import Capability
from legion.domain.grant import DelegationLimits

_AGENT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}\Z")
_PROFILE = re.compile(r"^[a-z0-9_-]+(/[a-z0-9_-]+)*\Z")


class ModelFeature(StrEnum):
    TOOLS = "tools"
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"
    REASONING = "reasoning"
    IMAGES = "images"
    PROMPT_CACHING = "prompt_caching"
    CONTEXT_MANAGEMENT = "context_management"


class ModelRequirement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str = "general/default"
    needs: frozenset[ModelFeature] = frozenset({ModelFeature.TOOLS})

    @field_validator("profile")
    @classmethod
    def _profile(cls, value: str) -> str:
        if not _PROFILE.match(value):
            raise ValueError(f"invalid model profile: {value!r}")
        return value


class AgentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    instructions: str = Field(min_length=1)
    model: ModelRequirement = ModelRequirement()
    tools: tuple[str, ...] = ()
    capabilities: tuple[Capability, ...] = ()
    budget: BudgetLimits = BudgetLimits()
    output_schema: dict[str, Any] | None = None
    # how much this agent may delegate when it's the root of a run; as a child it gets at most
    # what its parent allows
    delegation: DelegationLimits = DelegationLimits()
    max_output_tokens: int = Field(default=4096, gt=0)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _AGENT_NAME.match(value):
            raise ValueError(f"invalid agent name: {value!r}")
        return value

    @field_validator("capabilities", mode="before")
    @classmethod
    def _parse_caps(cls, value: Any) -> Any:
        if isinstance(value, list | tuple):
            return tuple(Capability.parse(v) if isinstance(v, str) else v for v in value)
        return value

    @field_serializer("capabilities")
    def _caps_as_text(self, caps: tuple[Capability, ...]) -> list[str]:
        return [str(c) for c in caps]

    @model_validator(mode="after")
    def _unique_tools(self) -> Self:
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("agent lists a tool more than once")
        return self

    @property
    def spec_hash(self) -> str:
        return digest(self.model_dump(mode="json"))
