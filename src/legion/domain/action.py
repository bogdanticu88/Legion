from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field

from legion.canonical import digest
from legion.domain.capability import Capability


class EffectClass(StrEnum):
    PURE = "pure"
    READ = "read"
    WRITE_IDEMPOTENT = "write_idempotent"
    WRITE = "write"
    EXTERNAL_IRREVERSIBLE = "external_irreversible"

    @property
    def safe_to_repeat(self) -> bool:
        return self in (EffectClass.PURE, EffectClass.READ, EffectClass.WRITE_IDEMPOTENT)


class Action(BaseModel):
    """One proposed tool call in canonical form. Policy judges it and approvals bind to its hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    arguments: dict[str, Any]
    resource: str | None
    required: tuple[Capability, ...]
    effect: EffectClass
    grant_id: str
    task_id: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hash(self) -> str:
        return digest(
            {
                "tool": self.tool,
                "arguments": self.arguments,
                "resource": self.resource,
                "grant_id": self.grant_id,
                "task_id": self.task_id,
            }
        )

    @property
    def repeat_key(self) -> str:
        """What makes two actions 'the same' for loop detection: tool and arguments only."""
        return digest({"tool": self.tool, "arguments": self.arguments})
