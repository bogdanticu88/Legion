from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from legion.domain.principal import Principal


class TaskSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    objective: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    priority: int = 0
    parent_id: str | None = None
    created_by: Principal
    deadline: datetime | None = None

    @field_validator("deadline")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("deadline must include a timezone")
        return value
