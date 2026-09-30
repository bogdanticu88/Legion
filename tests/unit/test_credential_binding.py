# Legion's own comparison of evidence against the request, one binding at a time, with no
# authority in the way: each check has to refuse on its own, not only because some other layer
# (an adapter's mapping, say) happened to catch the same thing first.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from legion.kernel.credentials import assess
from legion.ports.credentials import Assurance, CredentialRequest

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


def request(**kw: Any) -> CredentialRequest:
    base: dict[str, Any] = {
        "authority": "auth",
        "provider": "gw",
        "permissions": ("repo.read",),
        "resource": "repo-A",
        "capabilities": ("repo.read:repo-A",),
        "principal": "human:tester",
        "subject": "tester",
        "on_behalf_of": (),
        "run_id": "run",
        "task_id": "task",
        "call_id": "lc-1",
        "action_hash": "a" * 64,
        "grant_id": "g",
        "grant_fingerprint": "b" * 64,
        "tool": "read_repo",
        "max_lifetime_s": 60,
        "external_principal": "ext:tester",
    }
    base.update(kw)
    return CredentialRequest(**base)


def evidence(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "authority": "auth",
        "credential_ref": "ref-1",
        "provider": "gw",
        "principal": "human:tester",
        "subject": "tester",
        "external_principal": "ext:tester",
        "permissions": ["repo.read"],
        "resource": "repo-A",
        "issued_at": NOW,
        "expires_at": NOW + timedelta(seconds=60),
        "action_hash": "a" * 64,
        "call_id": "lc-1",
        "grant_fingerprint": "b" * 64,
    }
    base.update(kw)
    return base


def test_matching_evidence_is_bound() -> None:
    result = assess(request(), evidence(), trusted=True, now=NOW)
    assert result.assurance is Assurance.BOUND, result.problems


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"external_principal": "ext:someone"}, "different external principal"),
        ({"subject": "helper"}, "different principal or agent"),
        ({"principal": "human:mallory"}, "different principal or agent"),
        ({"call_id": "lc-2"}, "different call"),
        ({"action_hash": "c" * 64}, "different action"),
        ({"grant_fingerprint": "d" * 64}, "different grant"),
        ({"resource": "repo-B"}, "wider than"),
        ({"resource": None}, "wider than"),
        ({"permissions": ["repo.read", "repo.write"]}, "permissions wider"),
        ({"provider": "other"}, "for provider"),
        ({"authority": "other"}, "issued by"),
        ({"expires_at": NOW + timedelta(hours=2)}, "lives longer"),
        ({"expires_at": NOW}, "already expired"),
    ],
)
def test_each_binding_refuses_on_its_own(change: dict[str, Any], problem: str) -> None:
    result = assess(request(), evidence(**change), trusted=True, now=NOW)
    assert result.assurance is None
    assert any(problem in p for p in result.problems), result.problems


def test_authority_that_names_no_external_principal_is_judged_on_the_rest() -> None:
    result = assess(request(), evidence(external_principal=None), trusted=True, now=NOW)
    assert result.assurance is Assurance.BOUND


def test_request_without_external_principal_accepts_any_stated() -> None:
    result = assess(request(external_principal=None), evidence(), trusted=True, now=NOW)
    assert result.assurance is Assurance.BOUND


def test_verified_flag_never_raises_assurance() -> None:
    result = assess(request(), evidence(verified=True), trusted=False, now=NOW)
    assert result.assurance is Assurance.DECLARED
