from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from legion.domain.budget import BudgetLimits
from legion.domain.capability import Capability
from legion.domain.errors import ConfigError
from legion.domain.principal import IdentityContext


class AttenuationError(ConfigError):
    code = "attenuation_error"


class DelegationLimits(BaseModel):
    # max_depth: how many levels of children may exist below this grant. 0 means it can't
    # delegate at all. max_children: direct children per task.
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_depth: int = Field(default=0, ge=0, le=8)
    max_children: int = Field(default=0, ge=0, le=32)


class Grant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    capabilities: frozenset[Capability]
    budget: BudgetLimits
    identity: IdentityContext
    issuer: str
    expires_at: datetime | None = None
    parent_id: str | None = None
    depth: int = Field(default=0, ge=0)
    delegation: DelegationLimits = DelegationLimits()

    @field_serializer("capabilities")
    def _sorted(self, caps: frozenset[Capability]) -> list[str]:
        return sorted(str(cap) for cap in caps)

    @field_validator("capabilities", mode="before")
    @classmethod
    def _parse(cls, value: Any) -> Any:
        # capabilities are stored as strings in events
        if isinstance(value, list | tuple | set | frozenset):
            return frozenset(Capability.parse(v) if isinstance(v, str) else v for v in value)
        return value

    def covers(self, required: Capability) -> bool:
        return any(cap.covers(required) for cap in self.capabilities)

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def attenuate(
        self,
        *,
        id: str,
        capabilities: frozenset[Capability],
        budget: BudgetLimits,
        agent_ref: str,
        delegation: DelegationLimits | None = None,
        expires_at: datetime | None = None,
    ) -> Grant:
        """The only way to make a child grant. Every part is at most what this grant holds."""
        delegation = delegation or DelegationLimits()
        if self.delegation.max_depth < 1:
            raise AttenuationError("this grant does not allow delegation")
        wider = [str(cap) for cap in capabilities if not cap.is_within(self.capabilities)]
        if wider:
            raise AttenuationError(f"child capabilities exceed parent: {sorted(wider)}")
        if not budget.is_within(self.budget):
            raise AttenuationError("child budget exceeds parent budget")
        if delegation.max_depth > self.delegation.max_depth - 1:
            raise AttenuationError("child may not delegate deeper than its parent allows")
        if delegation.max_children > self.delegation.max_children:
            raise AttenuationError("child may not have more children than its parent")
        if self.expires_at is not None:
            if expires_at is None:
                expires_at = self.expires_at
            elif expires_at > self.expires_at:
                raise AttenuationError("child grant outlives parent grant")
        # Identity is derived, never passed in: same human at the top, and the chain records
        # every agent in between, so a child can't act under its parent's name.
        identity = IdentityContext(
            principal=self.identity.principal,
            agent_ref=agent_ref,
            on_behalf_of=(*self.identity.on_behalf_of, self.identity.agent_ref),
        )
        return Grant(
            id=id,
            capabilities=capabilities,
            budget=budget,
            identity=identity,
            issuer=f"delegation:{self.id}",
            expires_at=expires_at,
            parent_id=self.id,
            depth=self.depth + 1,
            delegation=delegation,
        )
