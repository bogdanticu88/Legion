from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from legion.domain.principal import Principal


class TaskSpec(BaseModel):
    """What a task asks for. Its status lives in the event log, not on this object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    objective: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    priority: int = 0
    parent_id: str | None = None
    created_by: Principal
    deadline: datetime | None = None
