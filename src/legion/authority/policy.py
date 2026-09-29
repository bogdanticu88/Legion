from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from legion.domain.action import Action, EffectClass
from legion.domain.grant import Grant


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyContext:
    grant: Grant
    agent: str
    task_id: str


class PolicyDecisionPoint(Protocol):
    async def evaluate(self, action: Action, context: PolicyContext) -> Decision: ...


# all set fields must match; tool and capability are globs
class Rule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: Verdict
    tool: str | None = None
    capability: str | None = None
    effect: EffectClass | None = None
    reason: str | None = None

    def matches(self, action: Action) -> bool:
        if self.tool is not None and not fnmatchcase(action.tool, self.tool):
            return False
        if self.capability is not None and not any(
            fnmatchcase(cap.name, self.capability) for cap in action.required
        ):
            return False
        return self.effect is None or self.effect is action.effect

    def describe(self) -> str:
        if self.reason:
            return self.reason
        conditions = [
            f"{k}={v}"
            for k, v in (
                ("tool", self.tool),
                ("capability", self.capability),
                ("effect", self.effect),
            )
            if v is not None
        ]
        return f"{self.decision.value} rule ({', '.join(conditions) or 'any'})"


_PRECEDENCE = (Verdict.DENY, Verdict.REQUIRE_APPROVAL, Verdict.ALLOW)


# deny > require_approval > allow, then `default` if nothing matched
class RuleTablePolicy:
    def __init__(self, rules: list[Rule], default: Verdict = Verdict.ALLOW) -> None:
        self.rules = tuple(rules)
        self.default = default

    async def evaluate(self, action: Action, context: PolicyContext) -> Decision:
        matched = [r for r in self.rules if r.matches(action)]
        for verdict in _PRECEDENCE:
            hits = [r for r in matched if r.decision is verdict]
            if hits:
                return Decision(verdict, tuple(r.describe() for r in hits))
        return Decision(self.default, (f"default {self.default.value}",))


def default_rules() -> list[Rule]:
    return [
        Rule(
            decision=Verdict.REQUIRE_APPROVAL,
            effect=EffectClass.EXTERNAL_IRREVERSIBLE,
            reason="irreversible actions need a human to approve them",
        )
    ]
