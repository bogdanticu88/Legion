from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from legion.domain.agent import ModelFeature, ModelRequirement
from legion.domain.errors import NoModelBinding
from legion.events.types import Usage
from legion.models.base import ModelProvider

_MILLION = Decimal(1_000_000)


class Pricing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_mtok: Decimal = Field(ge=0)
    output_per_mtok: Decimal = Field(ge=0)
    cache_read_per_mtok: Decimal | None = Field(default=None, ge=0)
    cache_write_per_mtok: Decimal | None = Field(default=None, ge=0)

    def cost(self, usage: Usage) -> Decimal:
        read = self.input_per_mtok if self.cache_read_per_mtok is None else self.cache_read_per_mtok
        write = (
            self.input_per_mtok if self.cache_write_per_mtok is None else self.cache_write_per_mtok
        )
        total = (
            usage.input_tokens * self.input_per_mtok
            + usage.output_tokens * self.output_per_mtok
            + usage.cache_read_tokens * read
            + usage.cache_write_tokens * write
        )
        return total / _MILLION


class ModelBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str
    provider: str
    model: str
    features: frozenset[ModelFeature] = frozenset({ModelFeature.TOOLS})
    max_context: int | None = None
    pricing: Pricing | None = None
    # e.g. {"anthropic": {"thinking": {...}}}
    options: dict[str, dict[str, Any]] = Field(default_factory=dict)


@dataclass(frozen=True)
class Resolved:
    binding: ModelBinding
    provider: ModelProvider

    @property
    def features(self) -> frozenset[ModelFeature]:
        # declared by the operator, limited by what the adapter implements
        return self.binding.features & self.provider.supported


class ModelResolver:
    def __init__(self, bindings: list[ModelBinding], providers: dict[str, ModelProvider]) -> None:
        unknown = {b.provider for b in bindings} - providers.keys()
        if unknown:
            raise NoModelBinding(f"bindings refer to unknown providers: {sorted(unknown)}")
        self.bindings = bindings
        self.providers = providers

    def resolve(self, requirement: ModelRequirement) -> Resolved:
        candidates = [b for b in self.bindings if b.profile == requirement.profile]
        if not candidates:
            raise NoModelBinding(f"no model is configured for profile {requirement.profile!r}")
        for binding in candidates:
            resolved = Resolved(binding, self.providers[binding.provider])
            if requirement.needs <= resolved.features:
                return resolved
        first = Resolved(candidates[0], self.providers[candidates[0].provider])
        missing = sorted(f.value for f in requirement.needs - first.features)
        raise NoModelBinding(
            f"no binding for {requirement.profile!r} supports {missing}; refusing to start"
        )
