# What any CredentialAuthority has to do for Legion, run against every implementation there is:
# the deterministic lab authority, the example in examples/authorities.py, and NIA's scoped
# credentials through the NIA adapter (against the NIA stand-in, so no NIA process is needed). A Vault or cloud IAM authority would be added
# to AUTHORITIES and held to the same tests.

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from legion.access.secrets import Secret
from legion.kernel.credentials import assess
from legion.ports.credentials import (
    REF_PATTERN,
    Assurance,
    CredentialRequest,
    CredentialStatus,
    UnknownCredential,
)
from tests.credential_lab import LabAuthority


def request(authority: str, provider: str, **kw: Any) -> CredentialRequest:
    base: dict[str, Any] = {
        "authority": authority,
        "provider": provider,
        "permissions": ("repo.read",),
        "resource": "repo-A",
        "capabilities": ("repo.read:repo-A",),
        "principal": "human:tester",
        "subject": "tester",
        "on_behalf_of": (),
        "run_id": "run-1",
        "task_id": "task-1",
        "call_id": "lc-0123456789abcdef0123456789abcdef",
        "action_hash": "a" * 64,
        "grant_id": "grant-1",
        "grant_fingerprint": "b" * 64,
        "tool": "read_repo",
        "max_lifetime_s": 60,
    }
    base.update(kw)
    return CredentialRequest(**base)


@asynccontextmanager
async def lab() -> AsyncIterator[tuple[Any, str]]:
    yield LabAuthority(), "github"


@asynccontextmanager
async def nia() -> AsyncIterator[tuple[Any, str]]:
    from tests.nia_cred_lab import serve
    from tests.nia_cred_support import authority, fake

    stand_in = fake()
    with serve(stand_in) as url:
        auth = authority(url)
        try:
            yield auth, "nia-gateway"
        finally:
            await auth.aclose()


@asynccontextmanager
async def example() -> AsyncIterator[tuple[Any, str]]:
    # examples/authorities.py, loaded the way legion.yaml loads it
    from pathlib import Path

    from legion.config.loader import load_authority_module

    path = Path(__file__).resolve().parents[2] / "examples" / "authorities.py"
    yield load_authority_module(path, "local"), "github"


AUTHORITIES: dict[str, Callable[[], Any]] = {"lab": lab, "example": example, "nia": nia}


@pytest.fixture(params=sorted(AUTHORITIES))
def make(request: Any) -> Callable[[], Any]:
    return AUTHORITIES[request.param]


async def test_issues_a_bound_credential_for_exactly_the_request(make: Any) -> None:
    async with make() as (authority, provider):
        req = request(authority.name, provider)
        issued = await authority.issue(req)
        assert isinstance(issued.secret, Secret) and issued.secret.reveal()
        assert issued.credential_ref is not None
        assert re.match(REF_PATTERN, issued.credential_ref)
        result = assess(req, issued.evidence, trusted=True, now=datetime.now(UTC))
        assert result.assurance is Assurance.BOUND, result.problems
        assert result.evidence is not None
        assert result.evidence.credential_ref == issued.credential_ref
        assert set(result.evidence.permissions) <= set(req.permissions)
        assert result.evidence.call_id == req.call_id
        assert result.evidence.action_hash == req.action_hash
        assert result.evidence.grant_fingerprint == req.grant_fingerprint


async def test_status_then_revoke(make: Any) -> None:
    async with make() as (authority, provider):
        issued = await authority.issue(request(authority.name, provider))
        assert await authority.status(issued.credential_ref) is CredentialStatus.ACTIVE
        await authority.revoke(issued.credential_ref)
        assert await authority.status(issued.credential_ref) is CredentialStatus.REVOKED
        # revoking again is harmless, and revoked stays revoked
        await authority.revoke(issued.credential_ref)
        assert await authority.status(issued.credential_ref) is CredentialStatus.REVOKED


async def test_unknown_reference_is_never_active(make: Any) -> None:
    async with make() as (authority, _):
        with pytest.raises(UnknownCredential):
            await authority.status("never-issued-0000")


async def test_two_calls_get_two_references(make: Any) -> None:
    async with make() as (authority, provider):
        a = await authority.issue(request(authority.name, provider, call_id="lc-" + "1" * 32))
        b = await authority.issue(request(authority.name, provider, call_id="lc-" + "2" * 32))
        assert a.credential_ref != b.credential_ref
        assert a.secret.reveal() != b.secret.reveal()


async def test_secret_is_not_in_the_evidence(make: Any) -> None:
    async with make() as (authority, provider):
        issued = await authority.issue(request(authority.name, provider))
        assert issued.secret.reveal() not in repr(issued.evidence)
        assert issued.secret.reveal() not in repr(issued)
