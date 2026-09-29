# Attacks on credential handling (ADR 0019) from the Phase 5A review: long or dirty
# references, secrets in evidence, requests the authority rewrites, malformed but wide evidence,
# odd return values. Each one found a bug that's since fixed; these keep it fixed.

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from legion.access.secrets import Secret
from legion.cli import describe
from legion.domain.states import RunStatus
from legion.events.sqlite_store import SqliteEventStore
from legion.events.types import EventType
from legion.kernel.credentials import CredentialMapping, assess
from legion.models.scripted import call, reply
from legion.ports.credentials import Assurance, CredentialRequest, IssuedCredential
from tests.credential_lab import LabAuthority
from tests.unit.test_credential_policy import Clock, setup, spec

E = EventType
READ_A = call("read_repo", {"repo": "repo-A"})
MAPPING = CredentialMapping(
    authority="lab",
    provider="github",
    permissions={"repo.read": ("contents:read",), "repo.issue.create": ("issues:write",)},
)


def mapping(minimum: Assurance) -> CredentialMapping:
    return MAPPING.model_copy(update={"minimum": minimum})


# 1. credential_ref reuse check compares the raw ref with the cleaned/truncated one in the log


@pytest.mark.parametrize("ref", ["r" * 150, "ref\x01same", "ref\u202esame"])
async def test_reused_long_or_dirty_reference_is_refused(ref: str) -> None:
    lab = LabAuthority(fixed_ref=ref)
    h, lab, seen = setup(
        [READ_A, call("read_repo", {"repo": "repo-A"}, id="second"), reply("ok")], lab
    )
    outcome = await h.run(spec())
    # a reference that's too long or has characters the log would change isn't accepted at all,
    # so what's recorded is always exactly what was compared
    assert seen == [], f"reference used {len(seen)} times"
    assert outcome.status is RunStatus.COMPLETED


async def test_reused_long_reference_is_refused_after_restart(tmp_path: Path) -> None:
    from tests.support import SimulatedCrash, crash_at

    store = SqliteEventStore(tmp_path / "e.db")
    # the longest reference allowed: reuse is checked on its digest, across the restart too
    lab = LabAuthority(fixed_ref="z" * 120)
    steps = [READ_A, call("read_repo", {"repo": "repo-A"}, id="second"), reply("ok")]
    h, lab, seen = setup(
        steps, lab, store=store, by_turn=True, faults=crash_at("after:tool.completed")
    )
    with pytest.raises(SimulatedCrash):
        await h.run(spec())
    [summary] = await store.runs()
    await h.restart(faults=None).resume(summary.run_id)
    store.close()
    assert len(seen) == 1, "long reference reused across restart"


# 2. partial secret leak: clean() truncates before redaction, so a long secret in evidence is
#    written as a 120-char prefix that doesn't match the remembered secret


@dataclass
class LongSecretAuthority(LabAuthority):
    field_name: str = "revocation_ref"

    async def issue(self, request: CredentialRequest) -> IssuedCredential:
        got = await super().issue(request)
        secret = "S" + "k9Qx7" * 60  # 301 chars, like a long JWT
        self.secrets[-1] = secret
        ev = dict(got.evidence)
        ev[self.field_name] = secret if self.field_name != "credential_ref" else secret[:200]
        return IssuedCredential(Secret(secret), ev)


@pytest.mark.parametrize("field_name", ["revocation_ref", "resource", "credential_ref"])
async def test_long_secret_in_evidence_never_reaches_the_log(
    tmp_path: Path, field_name: str
) -> None:
    store = SqliteEventStore(tmp_path / "e.db")
    lab = LongSecretAuthority(field_name=field_name)
    h, lab, _seen = setup([READ_A, reply("ok")], lab, store=store)
    outcome = await h.run(spec())
    events = await h.events(outcome.run_id)
    store.close()
    raw = (tmp_path / "e.db").read_bytes()
    secret = lab.secrets[0]
    prefix = secret[:60]
    dumped = "\n".join(e.model_dump_json() for e in events)
    shown = "\n".join(describe(e) for e in events)
    assert prefix not in dumped, "secret prefix in events"
    assert prefix.encode() not in raw, "secret prefix in SQLite bytes"
    assert prefix not in shown, "secret prefix in inspect output"


# 3. revoked AND expired before a retry: the ADR says revoked is refused, nothing issued


async def test_revoked_and_expired_before_retry_is_refused_not_replaced() -> None:
    clock = Clock()
    lab = LabAuthority()

    def on_call(n: int) -> None:
        if n == 1:
            lab.revoked.add("cred-001")
            clock.advance(120)

    h, lab, seen = setup(
        [READ_A, reply("ok")], lab, clock=clock, tools={"fail_first": True, "on_call": on_call}
    )
    await h.run(spec())
    assert len(lab.issued) == 1, "revoked credential was replaced by a fresh issue"
    assert len(seen) == 1


# 4. an authority that mutates the (frozen) request it was given


@dataclass
class MutatingAuthority(LabAuthority):
    async def issue(self, request: CredentialRequest) -> IssuedCredential:
        object.__setattr__(request, "permissions", ("admin:org", "contents:read"))
        object.__setattr__(request, "resource", None)
        return await super().issue(request)


@pytest.mark.parametrize("trusted", [(), ("lab",)])
async def test_authority_mutating_the_request_cannot_widen(trusted: tuple[str, ...]) -> None:
    lab = MutatingAuthority()
    h, lab, seen = setup(
        [READ_A, reply("ok")], lab, trusted=trusted, mapping=mapping(Assurance.DECLARED)
    )
    outcome = await h.run(spec())
    used = await h.payloads(outcome.run_id, E.CREDENTIAL_RESOLVED)
    assert not seen, f"admin:org credential used; record={used}"


# 5. malformed evidence that nonetheless shows wider authority


async def test_wide_but_malformed_evidence_is_not_accepted() -> None:
    lab = LabAuthority(mods={"permissions": ["admin:org"], "resource": None, "extra": 1})
    h, lab, seen = setup([READ_A, reply("ok")], lab, mapping=mapping(Assurance.UNVERIFIED))
    outcome = await h.run(spec())
    rec = await h.payloads(outcome.run_id, E.CREDENTIAL_RESOLVED)
    # ADR: "Evidence that shows more authority than was requested is refused outright"
    assert not seen, f"accepted: {rec}"


# 6. authority returning garbage instead of IssuedCredential


@dataclass
class GarbageAuthority(LabAuthority):
    ret: Any = None

    async def issue(self, request: CredentialRequest) -> Any:
        await super().issue(request)
        return self.ret


@pytest.mark.parametrize("ret", [None, "tok-abcdef", {"secret": "tok-abcdef"}])
async def test_authority_returning_garbage_is_a_clean_refusal(ret: Any) -> None:
    lab = GarbageAuthority(ret=ret)
    h, lab, _seen = setup([READ_A, reply("ok")], lab)
    outcome = await h.run(spec())
    assert outcome.status is RunStatus.COMPLETED, outcome
    reasons = [p["reason_code"] for p in await h.payloads(outcome.run_id, E.ACTION_REFUSED)]
    assert reasons == ["credential_refused"]


@pytest.mark.parametrize("secret", ["plain-string-token", b"bytes-token-xyz"])
async def test_non_secret_secret_is_refused(secret: Any) -> None:
    class A(LabAuthority):
        async def issue(self, request: CredentialRequest) -> IssuedCredential:
            got = await super().issue(request)
            return IssuedCredential(secret, got.evidence)  # type: ignore[arg-type]

    h, _lab, seen = setup([READ_A, reply("ok")], A())
    outcome = await h.run(spec())
    assert not seen
    assert outcome.status is RunStatus.COMPLETED


# 7. status answered with something that isn't a status, or slowly


@pytest.mark.parametrize("answer", [1, "yes", "active", [1], object(), True])
async def test_answer_that_isnt_a_status_is_not_yes(answer: Any) -> None:
    class A(LabAuthority):
        async def status(self, credential_ref: str) -> Any:
            return answer

    h, _lab, seen = setup([READ_A, reply("ok")], A())
    outcome = await h.run(spec())
    assert not seen
    [rec] = await h.payloads(outcome.run_id, E.CREDENTIAL_REFUSED)
    assert "didn't say" in rec["problems"][-1]


async def test_slow_status_is_refused() -> None:
    class A(LabAuthority):
        async def status(self, credential_ref: str) -> Any:
            await asyncio.sleep(5)
            return "active"

    h, _lab, seen = setup([READ_A, reply("ok")], A())
    outcome = await h.run(spec())
    assert not seen
    [rec] = await h.payloads(outcome.run_id, E.CREDENTIAL_REFUSED)
    assert "TimeoutError" in rec["problems"][-1]


# 8. same Secret object for two calls


async def test_same_secret_object_for_two_calls() -> None:
    shared = Secret("shared-secret-value-123456")

    class A(LabAuthority):
        async def issue(self, request: CredentialRequest) -> IssuedCredential:
            got = await super().issue(request)
            return IssuedCredential(shared, got.evidence)

    steps = [READ_A, call("read_repo", {"repo": "repo-A"}, id="two"), reply("ok")]
    h, _, _ = setup(steps, A())
    outcome = await h.run(spec())
    events = await h.events(outcome.run_id)
    assert all("shared-secret-value" not in e.model_dump_json() for e in events)


# 9. evidence as other models / edge datetimes (unit level)


def _req() -> CredentialRequest:
    from tests.unit.test_credentials import _request

    return _request()


def _ev(req: CredentialRequest, now: datetime, **mods: Any) -> dict[str, Any]:
    ev = {
        "authority": req.authority,
        "credential_ref": "c1",
        "provider": req.provider,
        "principal": req.principal,
        "subject": req.subject,
        "permissions": list(req.permissions),
        "resource": req.resource,
        "issued_at": now,
        "expires_at": now + timedelta(seconds=60),
        "action_hash": req.action_hash,
        "call_id": req.call_id,
        "grant_fingerprint": req.grant_fingerprint,
    }
    ev.update(mods)
    return ev


NOW = datetime.now(UTC)


@pytest.mark.parametrize(
    ("mods", "want"),
    [
        ({"issued_at": NOW.replace(tzinfo=None)}, Assurance.UNVERIFIED),
        ({"expires_at": NOW.replace(tzinfo=None) + timedelta(seconds=60)}, Assurance.UNVERIFIED),
        ({"expires_at": datetime.max.replace(tzinfo=UTC)}, None),
        ({"issued_at": datetime.min.replace(tzinfo=UTC)}, None),
        ({"expires_at": NOW - timedelta(days=1)}, None),
        ({"verified": "yes"}, Assurance.BOUND),
        ({"verified": "banana"}, Assurance.UNVERIFIED),
        ({"resource": "repo-A/"}, None),
        ({"resource": "REPO-A"}, None),
        ({"resource": "repo\u2010A"}, None),
        ({"resource": "repo-A*"}, None),
        ({"resource": "repo-*"}, None),
        ({"resource": "**"}, None),
        ({"resource": "repo-\u0410"}, None),
        ({"permissions": ["Contents:Read"]}, None),
        ({"permissions": ["contents:read "]}, None),
        ({"permissions": ["contents:*"]}, None),
        ({"permissions": []}, Assurance.BOUND),
        ({"issued_at": 0, "expires_at": 10}, None),
        ({"expires_at": float("nan")}, Assurance.UNVERIFIED),
        (
            {"issued_at": NOW + timedelta(seconds=4), "expires_at": NOW + timedelta(seconds=2)},
            Assurance.BOUND,
        ),
    ],
)
def test_assess_edges(mods: dict[str, Any], want: Assurance | None) -> None:
    req = _req()
    result = assess(req, _ev(req, NOW, **mods), trusted=True, now=NOW)
    assert result.assurance == want, result


def test_evidence_as_other_basemodel() -> None:
    from pydantic import BaseModel

    req = _req()
    data = _ev(req, NOW)

    class Other(BaseModel):
        model_config = {"extra": "allow"}

    other = Other(**data)
    assert assess(req, other, trusted=True, now=NOW).assurance is Assurance.BOUND
    # a model whose dump includes permissions it doesn't show as fields
    from legion.ports.credentials import CredentialEvidence

    constructed = CredentialEvidence.model_construct(**{**data, "permissions": ("admin",)})
    assert assess(req, constructed, trusted=True, now=NOW).assurance is None
