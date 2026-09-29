from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from legion.domain.budget import BudgetLimits
from legion.domain.capability import Capability
from legion.domain.errors import ConfigError
from legion.domain.principal import IdentityContext


class AttenuationError(ConfigError):
    code = "attenuation_error"


class Grant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    capabilities: frozenset[Capability]
    budget: BudgetLimits
    identity: IdentityContext
    issuer: str
    expires_at: datetime | None = None
    parent_id: str | None = None

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
        identity: IdentityContext,
        issuer: str,
        expires_at: datetime | None = None,
    ) -> Grant:
        wider = [str(cap) for cap in capabilities if not cap.is_within(self.capabilities)]
        if wider:
            raise AttenuationError(f"child capabilities exceed parent: {sorted(wider)}")
        if not budget.is_within(self.budget):
            raise AttenuationError("child budget exceeds parent budget")
        if self.expires_at is not None:
            if expires_at is None:
                expires_at = self.expires_at
            elif expires_at > self.expires_at:
                raise AttenuationError("child grant outlives parent grant")
        return Grant(
            id=id,
            capabilities=capabilities,
            budget=budget,
            identity=identity,
            issuer=issuer,
            expires_at=expires_at,
            parent_id=self.id,
        )
