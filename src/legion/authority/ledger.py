from __future__ import annotations

from decimal import Decimal

from legion.domain.budget import Dimension
from legion.domain.errors import BudgetExceeded
from legion.domain.grant import Grant
from legion.events.projections import RunState
from legion.events.types import BudgetConsumed


# Counts (steps, calls) are checked before they happen. Tokens and cost are only known
# afterwards, so they get recorded even when they go over, and then the run stops.
class Ledger:
    def __init__(self, grant: Grant, state: RunState) -> None:
        self.grant = grant
        self.state = state

    def used(self, dimension: Dimension) -> Decimal:
        return self.state.used(self.grant.id, dimension.value)

    def remaining(self, dimension: Dimension) -> Decimal | None:
        limit = self.grant.budget.limit(dimension)
        if limit is None:
            return None
        return Decimal(limit) - self.used(dimension)

    def precheck(self, dimension: Dimension, amount: Decimal | int = 1) -> None:
        limit = self.grant.budget.limit(dimension)
        attempted = self.used(dimension) + Decimal(amount)
        if limit is not None and attempted > Decimal(limit):
            raise BudgetExceeded(dimension.value, limit, attempted)

    def charge(self, dimension: Dimension, amount: Decimal | int) -> tuple[BudgetConsumed, bool]:
        total = self.used(dimension) + Decimal(amount)
        limit = self.grant.budget.limit(dimension)
        record = BudgetConsumed(
            grant_id=self.grant.id, dimension=dimension.value, amount=Decimal(amount), total=total
        )
        return record, limit is not None and total > Decimal(limit)
