# Properties of credential assessment over generated requests and generated tampering. Whatever
# the authority sends, assurance never goes up by itself, and nothing wider than the request, or
# for someone or something else, is accepted.

from datetime import UTC, datetime, timedelta
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from legion.domain.action import Action, EffectClass
from legion.domain.budget import BudgetLimits
from legion.domain.capability import Capability
from legion.domain.grant import DelegationLimits, Grant
from legion.domain.principal import IdentityContext, Principal, PrincipalKind
from legion.kernel.credentials import CredentialMapping, assess, derive_request, ref_digest
from legion.ports.credentials import Assurance

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
NAMES = st.sampled_from(["repo.read", "repo.issue.create", "repo.admin"])
RESOURCES = st.sampled_from(["repo-A", "repo-B", "org/repo-C", "repo-\u0410"])
MAPPING = CredentialMapping(
    authority="lab",
    provider="github",
    permissions={
        "repo.read": ("contents:read",),
        "repo.issue.create": ("issues:write",),
        "repo.admin": ("admin",),
    },
    max_lifetime_s=300,
)
# every way evidence can differ from what was asked for, and whether that difference widens or
# redirects the credential (refused) or only lowers what Legion can say about it
TAMPER: dict[str, Any] = {
    "extra_permission": lambda e, r: {"permissions": [*e["permissions"], "admin:org"]},
    "any_resource": lambda e, r: {"resource": None},
    "glob_resource": lambda e, r: {"resource": "*"},
    "other_resource": lambda e, r: {"resource": r.resource + "-other"},
    "principal": lambda e, r: {"principal": "human:mallory"},
    "subject": lambda e, r: {"subject": "someone-else"},
    "action": lambda e, r: {"action_hash": "0" * 64},
    "call": lambda e, r: {"call_id": "call_other"},
    "grant": lambda e, r: {"grant_fingerprint": "f" * 64},
    "expired": lambda e, r: {"expires_at": NOW - timedelta(seconds=1)},
    "too_long": lambda e, r: {"expires_at": NOW + timedelta(days=30)},
    "old_and_long": lambda e, r: {"issued_at": NOW - timedelta(days=30)},
    "future": lambda e, r: {"issued_at": NOW + timedelta(hours=1)},
    "provider": lambda e, r: {"provider": "gitlab"},
    "authority": lambda e, r: {"authority": "other"},
}
DOWNGRADE: dict[str, Any] = {
    "unbound_action": lambda e, r: {"action_hash": None},
    "unbound_call": lambda e, r: {"call_id": None},
    "unbound_grant": lambda e, r: {"grant_fingerprint": None},
}


def grant(caps: frozenset[Capability]) -> Grant:
    return Grant(
        id="g1",
        capabilities=caps,
        budget=BudgetLimits(),
        identity=IdentityContext(
            principal=Principal(kind=PrincipalKind.HUMAN, id="ana"), agent_ref="boss"
        ),
        issuer="test",
        delegation=DelegationLimits(max_depth=2, max_children=2),
    )


def request_for(names: list[str], resource: str) -> Any:
    required = tuple(Capability(name=n, resource=resource) for n in sorted(set(names)))
    g = grant(frozenset(Capability(name=n, resource=resource) for n in names))
    action = Action(
        tool="t",
        arguments={"repo": resource},
        resource=resource,
        required=required,
        effect=EffectClass.READ,
        grant_id=g.id,
        task_id="task",
    )
    request = derive_request(MAPPING, action, g, run_id="run", call_id="call_1")
    assert not isinstance(request, str)
    return request


def exact(request: Any) -> dict[str, Any]:
    return {
        "authority": "lab",
        "credential_ref": "ref-1",
        "provider": request.provider,
        "principal": request.principal,
        "subject": request.subject,
        "permissions": list(request.permissions),
        "resource": request.resource,
        "issued_at": NOW - timedelta(seconds=1),
        "expires_at": NOW + timedelta(seconds=60),
        "action_hash": request.action_hash,
        "call_id": request.call_id,
        "grant_fingerprint": request.grant_fingerprint,
        "verified": True,
    }


REQUESTS = st.builds(request_for, st.lists(NAMES, min_size=1, max_size=3), RESOURCES)


@settings(max_examples=300)
@given(
    request=REQUESTS,
    tamper=st.sets(st.sampled_from(sorted(TAMPER)), min_size=1, max_size=4),
    downgrade=st.sets(st.sampled_from(sorted(DOWNGRADE)), max_size=3),
    trusted=st.booleans(),
)
def test_any_widening_or_substitution_is_refused(
    request: Any, tamper: set[str], downgrade: set[str], trusted: bool
) -> None:
    evidence = exact(request)
    # downgrades first, so they can't undo a tamper (an unbound action hash after a wrong one
    # would just be unbound)
    for name in sorted(downgrade):
        evidence.update(DOWNGRADE[name](evidence, request))
    for name in sorted(tamper):
        evidence.update(TAMPER[name](evidence, request))
    result = assess(request, evidence, trusted=trusted, now=NOW)
    assert result.assurance is None, (tamper, result)


@settings(max_examples=200)
@given(
    request=REQUESTS,
    downgrade=st.sets(st.sampled_from(sorted(DOWNGRADE)), max_size=3),
    trusted=st.booleans(),
)
def test_assurance_is_earned_not_claimed(request: Any, downgrade: set[str], trusted: bool) -> None:
    evidence = exact(request)
    for name in downgrade:
        evidence.update(DOWNGRADE[name](evidence, request))
    result = assess(request, evidence, trusted=trusted, now=NOW)
    assert result.assurance is not None
    if not trusted:
        # an untrusted authority can't get past declared, whatever it says about itself
        assert result.assurance is Assurance.DECLARED
    elif downgrade:
        assert result.assurance is Assurance.VERIFIED
    else:
        assert result.assurance is Assurance.BOUND


@settings(max_examples=200)
@given(
    request=REQUESTS,
    raw=st.one_of(
        st.none(),
        st.just({}),
        st.dictionaries(st.text(max_size=8), st.text(max_size=8), max_size=4),
        st.text(max_size=20),
        st.integers(),
        st.lists(st.text(max_size=5), max_size=3),
    ),
    trusted=st.booleans(),
)
def test_missing_or_malformed_evidence_never_raises_assurance(
    request: Any, raw: Any, trusted: bool
) -> None:
    result = assess(request, raw, trusted=trusted, now=NOW)
    assert result.assurance is Assurance.UNVERIFIED


@settings(max_examples=100)
@given(request=REQUESTS, trusted=st.booleans())
def test_a_reference_already_used_is_refused(request: Any, trusted: bool) -> None:
    seen = {ref_digest(request.run_id, "ref-1")}
    result = assess(request, exact(request), trusted=trusted, now=NOW, seen=seen)
    assert result.assurance is None


@settings(max_examples=200)
@given(
    parent_names=st.sets(NAMES, min_size=1),
    child_names=st.sets(NAMES, min_size=1),
    resource=RESOURCES,
)
def test_child_request_is_within_the_child_and_parent_grants(
    parent_names: set[str], child_names: set[str], resource: str
) -> None:
    parent = grant(frozenset(Capability(name=n, resource=resource) for n in parent_names))
    wanted = frozenset(Capability(name=n, resource=resource) for n in child_names)
    try:
        child = parent.attenuate(
            id="g-child", capabilities=wanted, budget=BudgetLimits(), agent_ref="child"
        )
    except Exception:
        # asking for more than the parent holds never produces a child grant at all
        assert not child_names <= parent_names
        return
    action = Action(
        tool="t",
        arguments={},
        resource=resource,
        required=tuple(sorted(wanted, key=str)),
        effect=EffectClass.READ,
        grant_id=child.id,
        task_id="child-task",
    )
    request = derive_request(MAPPING, action, child, run_id="r", call_id="c")
    assert not isinstance(request, str)
    allowed = {p for n in child_names for p in MAPPING.permissions[n]}
    parent_allowed = {p for n in parent_names for p in MAPPING.permissions[n]}
    assert set(request.permissions) <= allowed <= parent_allowed
    assert request.resource == resource
    assert request.subject == "child" and request.on_behalf_of[-1] == "boss"
