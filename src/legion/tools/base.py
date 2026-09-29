from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, Self

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from legion.access.secrets import Secret
from legion.domain.action import EffectClass
from legion.domain.capability import Capability
from legion.domain.errors import InvalidArguments

TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = Field(min_length=1)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    effect: EffectClass
    capabilities: tuple[str, ...] = ()
    resource_arg: str | None = None
    timeout_s: float = Field(default=30.0, gt=0, le=3600)
    max_attempts: int = Field(default=2, ge=1, le=5)
    credentials: tuple[str, ...] = ()
    max_output_chars: int = Field(default=20_000, gt=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not TOOL_NAME.match(self.name):
            raise ValueError(f"tool name must match {TOOL_NAME.pattern}: {self.name!r}")
        # anything with an effect has to say what it needs
        if self.effect is not EffectClass.PURE and not self.capabilities:
            raise ValueError(f"tool {self.name} has effect {self.effect} but no capabilities")
        for name in self.capabilities:
            cap = Capability(name=name)
            if cap.name.endswith(".*"):
                raise ValueError(f"tool {self.name} must require concrete capabilities")
        if self.input_schema.get("type") != "object":
            raise ValueError(f"tool {self.name} input schema must describe an object")
        for schema in (self.input_schema, self.output_schema):
            if schema is not None:
                try:
                    Draft202012Validator.check_schema(schema)
                except SchemaError as exc:
                    raise ValueError(
                        f"tool {self.name} has an invalid schema: {exc.message}"
                    ) from exc
        return self


@dataclass(frozen=True)
class ToolContext:
    task_id: str
    agent: str
    call_id: str
    credentials: Mapping[str, Secret] = field(default_factory=dict)
    settings: Mapping[str, str] = field(default_factory=dict)


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str | None = None
    data: Any = None
    is_error: bool = False

    def for_model(self) -> str:
        if self.content is not None:
            return self.content
        if self.data is None:
            return ""
        return json.dumps(self.data, sort_keys=True, default=str)


class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    def resource_of(self, arguments: dict[str, Any]) -> str | None: ...

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult: ...


def resource_from_argument(spec: ToolSpec, arguments: dict[str, Any]) -> str | None:
    if spec.resource_arg is None:
        return None
    value = arguments.get(spec.resource_arg)
    if not isinstance(value, str) or not value:
        raise InvalidArguments(f"{spec.resource_arg} must be a non-empty string")
    return value
