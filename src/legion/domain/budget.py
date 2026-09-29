from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Dimension(StrEnum):
    STEPS = "steps"
    MODEL_CALLS = "model_calls"
    TOOL_CALLS = "tool_calls"
    TOKENS = "tokens"
    COST_USD = "cost_usd"
    WALL_SECONDS = "wall_seconds"


class BudgetLimits(BaseModel):
    """Upper bounds for one grant. `None` means unlimited, which only the operator can choose.

    The defaults are deliberately small so that an agent defined without a budget cannot run away.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: int | None = Field(default=20, ge=0)
    model_calls: int | None = Field(default=30, ge=0)
    tool_calls: int | None = Field(default=50, ge=0)
    tokens: int | None = Field(default=200_000, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    wall_seconds: int | None = Field(default=600, ge=0)

    def limit(self, dimension: Dimension) -> int | Decimal | None:
        value: int | Decimal | None = getattr(self, dimension.value)
        return value

    def is_within(self, other: BudgetLimits) -> bool:
        for dimension in Dimension:
            mine, theirs = self.limit(dimension), other.limit(dimension)
            if theirs is None:
                continue
            if mine is None or mine > theirs:
                return False
        return True
