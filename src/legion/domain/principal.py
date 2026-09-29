from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PrincipalKind(StrEnum):
    HUMAN = "human"
    SERVICE = "service"
    AGENT = "agent"


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: PrincipalKind
    id: str = Field(min_length=1)

    def __str__(self) -> str:
        return f"{self.kind.value}:{self.id}"


class IdentityContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: Principal
    agent_ref: str
    # outermost first: the human, then each delegating agent
    on_behalf_of: tuple[str, ...] = ()
