from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from legion.authority.ledger import Ledger
from legion.authority.policy import Rule, RuleTablePolicy, Verdict, default_rules
from legion.domain.action import Action, EffectClass
from legion.domain.budget import BudgetLimits, Dimension
from legion.domain.capability import Capability
from legion.domain.errors import BudgetExceeded
from legion.domain.grant import AttenuationError, Grant
from legion.domain.principal import IdentityContext
from legion.events.projections import RunState
from tests.support import PRINCIPAL

C = Capability.parse
IDENTITY = IdentityContext(principal=PRINCIPAL, agent_ref="a")


def grant(*caps: str, budget: BudgetLimits | None = None, expires: datetime | None = None) -> Grant:
    return Grant(
        id="g1",
        capabilities=frozenset(C(c) for c in caps),
        budget=budget or BudgetLimits(),
        identity=IDENTITY,
        issuer="test",
        expires_at=expires,
    )


class TestCapability:
    def test_round_trip(self) -> None:
        assert str(C("files.read:notes/**")) == "files.read:notes/**"
        assert C("files.read").resource is None

    @pytest.mark.parametrize("bad", ["", "Files.read", "files..read", "files.read.*.x", "1files"])
    def test_invalid_names(self, bad: str) -> None:
        with pytest.raises(ValueError):
            C(bad)

    @pytest.mark.parametrize(
        ("granted", "required", "covered"),
        [
            ("files.read", "files.read:any/thing", True),
            ("files.read:notes/**", "files.read:notes/a/b.md", True),
            ("files.read:notes/*", "files.read:notes/a.md", True),
            ("files.read:notes/*", "files.read:notes/a/b.md", False),
            ("files.read:notes/**", "files.read:other/a.md", False),
            ("files.read:notes/**", "files.read", False),
            ("files.read:notes/**", "files.read:notes/../secret.md", False),
            ("files.*", "files.read:x", True),
            ("files.*", "filesystem.read:x", False),
            ("files.read", "files.write:x", False),
            ("files.*", "files.*", False),
        ],
    )
    def test_covers(self, granted: str, required: str, covered: bool) -> None:
        assert C(granted).covers(C(required)) is covered

    def test_is_within(self) -> None:
        parent = frozenset({C("files.read:notes/**"), C("net.*")})
        assert C("files.read:notes/**").is_within(parent)
        assert C("files.read:notes/a.md").is_within(parent)
        assert C("net.http").is_within(parent)
        assert not C("files.read").is_within(parent)
        assert C("files.read:notes/*").is_within(parent)
        assert C("files.read:notes/sub/**").is_within(parent)
        assert not C("files.read:*/notes").is_within(parent)
        # Containment between two arbitrary globs is not decided, so it is refused even when true.
        assert not C("files.read:a?c").is_within(frozenset({C("files.read:a*c")}))


class TestGrant:
    def test_expiry(self) -> None:
        now = datetime.now(UTC)
        assert grant("a.b", expires=now).expired(now)
        assert not grant("a.b").expired(now)

    def test_attenuate_narrower(self) -> None:
        parent = grant("files.read:notes/**", "files.write:out/**")
        child = parent.attenuate(
            id="g2",
            capabilities=frozenset({C("files.read:notes/a.md")}),
            budget=BudgetLimits(steps=5, model_calls=5, tool_calls=5, tokens=100, wall_seconds=10),
            identity=IDENTITY,
            issuer="test",
        )
        assert child.parent_id == "g1"
        assert child.covers(C("files.read:notes/a.md"))
        assert not child.covers(C("files.write:out/x"))

    def test_attenuate_refuses_wider_capability(self) -> None:
        with pytest.raises(AttenuationError):
            grant("files.read:notes/**").attenuate(
                id="g2",
                capabilities=frozenset({C("files.read")}),
                budget=BudgetLimits(),
                identity=IDENTITY,
                issuer="test",
            )

    @pytest.mark.parametrize("budget", [BudgetLimits(steps=21), BudgetLimits(tokens=None)])
    def test_attenuate_refuses_wider_budget(self, budget: BudgetLimits) -> None:
        with pytest.raises(AttenuationError):
            grant("a.b").attenuate(
                id="g2", capabilities=frozenset(), budget=budget, identity=IDENTITY, issuer="t"
            )

    def test_attenuate_expiry(self) -> None:
        now = datetime.now(UTC)
        parent = grant("a.b", expires=now)
        inherited = parent.attenuate(
            id="g2", capabilities=frozenset(), budget=BudgetLimits(), identity=IDENTITY, issuer="t"
        )
        assert inherited.expires_at == now
        with pytest.raises(AttenuationError):
            parent.attenuate(
                id="g3",
                capabilities=frozenset(),
                budget=BudgetLimits(),
                identity=IDENTITY,
                issuer="t",
                expires_at=now + timedelta(seconds=1),
            )


class TestLedger:
    def test_precheck_and_charge(self) -> None:
        state = RunState("r")
        ledger = Ledger(grant("a.b", budget=BudgetLimits(tool_calls=1)), state)
        ledger.precheck(Dimension.TOOL_CALLS)
        record, over = ledger.charge(Dimension.TOOL_CALLS, 1)
        assert (record.total, over) == (Decimal(1), False)
        state.consumed[("g1", "tool_calls")] = record.total
        with pytest.raises(BudgetExceeded):
            ledger.precheck(Dimension.TOOL_CALLS)

    def test_overshoot_is_reported(self) -> None:
        ledger = Ledger(grant("a.b", budget=BudgetLimits(tokens=10)), RunState("r"))
        _, over = ledger.charge(Dimension.TOKENS, 11)
        assert over

    def test_unlimited(self) -> None:
        ledger = Ledger(grant("a.b", budget=BudgetLimits(tokens=None)), RunState("r"))
        assert ledger.remaining(Dimension.TOKENS) is None
        ledger.precheck(Dimension.TOKENS, 10**9)


def action(
    tool: str = "t", effect: EffectClass = EffectClass.READ, cap: str = "files.read"
) -> Action:
    return Action(
        tool=tool,
        arguments={},
        resource=None,
        required=(C(cap),),
        effect=effect,
        grant_id="g",
        task_id="t",
    )


class TestPolicy:
    async def test_deny_beats_allow(self) -> None:
        policy = RuleTablePolicy(
            [Rule(decision=Verdict.ALLOW, tool="*"), Rule(decision=Verdict.DENY, tool="danger_*")]
        )
        assert (await policy.evaluate(action("danger_x"), None)).verdict is Verdict.DENY  # type: ignore[arg-type]
        assert (await policy.evaluate(action("safe"), None)).verdict is Verdict.ALLOW  # type: ignore[arg-type]

    async def test_default_and_conditions(self) -> None:
        policy = RuleTablePolicy(
            [Rule(decision=Verdict.ALLOW, capability="files.*", effect=EffectClass.READ)],
            default=Verdict.DENY,
        )
        assert (await policy.evaluate(action(), None)).verdict is Verdict.ALLOW  # type: ignore[arg-type]
        denied = await policy.evaluate(action(effect=EffectClass.WRITE), None)  # type: ignore[arg-type]
        assert denied.verdict is Verdict.DENY
        assert denied.reasons == ("default deny",)

    async def test_default_rules_ask_before_irreversible(self) -> None:
        policy = RuleTablePolicy(default_rules())
        decision = await policy.evaluate(action(effect=EffectClass.EXTERNAL_IRREVERSIBLE), None)  # type: ignore[arg-type]
        assert decision.verdict is Verdict.REQUIRE_APPROVAL

    async def test_deny_beats_approval(self) -> None:
        policy = RuleTablePolicy(
            [Rule(decision=Verdict.REQUIRE_APPROVAL), Rule(decision=Verdict.DENY, tool="t")]
        )
        assert (await policy.evaluate(action(), None)).verdict is Verdict.DENY  # type: ignore[arg-type]


def test_grant_round_trips_through_json() -> None:
    original = grant("files.read:notes/**", "net.*")
    assert Grant.model_validate(original.model_dump(mode="json")) == original
    assert Grant.model_validate_json(original.model_dump_json()) == original
