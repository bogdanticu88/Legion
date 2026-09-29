# Credentials for tool calls. A credential is either static (a secret reference in legion.yaml,
# same for every call, assurance unverified) or issued per call by a credential authority from a
# mapping the operator wrote. The request is built from the authorized Action and that mapping,
# never from model text, tool output or anything a server says. Whatever the authority returns
# is checked against the request, and Legion decides the assurance level, not the authority.

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from legion.access.secrets import CredentialResolver, Secret, SecretRef
from legion.canonical import digest
from legion.domain.action import Action
from legion.domain.capability import Capability, glob_contains
from legion.domain.errors import ConfigError, CredentialUnavailable
from legion.domain.grant import Grant
from legion.ports.credentials import (
    REF_CHARS,
    Assurance,
    CredentialAuthority,
    CredentialEvidence,
    CredentialRequest,
    CredentialStatus,
    IssuedCredential,
)

# an authority's clock may be a little off from ours
CLOCK_SKEW = timedelta(seconds=5)
# why a credential held from an earlier attempt can't be used for the next one
EXPIRED = "expired before this attempt"
REVOKED = "no longer active at the authority"
UNKNOWN = "the authority didn't say whether it's still active"
WIDER = "permissions wider than requested"
_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f\u061c\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")


class CredentialMapping(BaseModel):
    """How a credential name maps onto an authority. Written by the operator."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    authority: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    # Legion capability name -> the provider permissions a call needing it gets
    permissions: dict[str, tuple[str, ...]]
    max_lifetime_s: int = Field(default=300, gt=0, le=3600)
    minimum: Assurance = Assurance.BOUND

    @field_validator("permissions")
    @classmethod
    def _perms(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        if not value:
            raise ValueError("a credential mapping needs at least one capability")
        for name, perms in value.items():
            if Capability(name=name).name.endswith(".*"):
                raise ValueError(f"map concrete capabilities, not {name}")
            if not perms or any(not p for p in perms):
                raise ValueError(f"{name} maps to no permissions")
        return value

    def describe(self) -> str:
        perms = {k: sorted(v) for k, v in sorted(self.permissions.items())}
        return f"issued:{digest({'m': self.model_dump(mode='json'), 'p': perms})[:16]}"


@dataclass(frozen=True)
class Assessment:
    # None means refused
    assurance: Assurance | None
    problems: tuple[str, ...] = ()
    evidence: CredentialEvidence | None = None


@dataclass
class Obtained:
    """One credential for one call. `secret` is None if it was refused."""

    name: str
    authority: str
    assurance: Assurance | None
    required: Assurance
    secret: Secret | None = None
    evidence: CredentialEvidence | None = None
    request: CredentialRequest | None = None
    problems: tuple[str, ...] = ()
    issuer: CredentialAuthority | None = field(default=None, repr=False)
    # what the authority handed over, kept only so it can be scrubbed if the credential is
    # refused (an authority can put it in the evidence); never given to a tool
    issued: Secret | None = field(default=None, repr=False)
    # the authority's reference, for asking about the credential and revoking it
    ref: str | None = None

    @property
    def refused(self) -> bool:
        return self.secret is None


def grant_fingerprint(grant: Grant) -> str:
    return digest(grant.model_dump(mode="json"))


def derive_request(
    mapping: CredentialMapping,
    action: Action,
    grant: Grant,
    *,
    run_id: str,
    call_id: str,
) -> CredentialRequest | str:
    """The request an authorized Action implies, or why there isn't one."""
    permissions: set[str] = set()
    for cap in action.required:
        mapped = mapping.permissions.get(cap.name)
        if mapped is None:
            return f"no credential mapping for capability {cap.name}"
        permissions.update(mapped)
    identity = grant.identity
    return CredentialRequest(
        authority=mapping.authority,
        provider=mapping.provider,
        permissions=tuple(sorted(permissions)),
        resource=action.resource,
        capabilities=tuple(sorted(str(c) for c in action.required)),
        principal=str(identity.principal),
        subject=identity.agent_ref,
        on_behalf_of=identity.on_behalf_of,
        run_id=run_id,
        task_id=action.task_id,
        call_id=call_id,
        action_hash=action.hash,
        grant_id=grant.id,
        grant_fingerprint=grant_fingerprint(grant),
        tool=action.tool,
        max_lifetime_s=mapping.max_lifetime_s,
    )


def ref_digest(run_id: str, credential_ref: str) -> str:
    # Reuse is checked on the exact reference, not on what's shown in the log: this digest is
    # what the log keeps for that.
    return digest({"run": run_id, "ref": credential_ref})


def assess(
    request: CredentialRequest,
    raw: Any,
    *,
    trusted: bool,
    now: datetime,
    seen: frozenset[str] | set[str] = frozenset(),
) -> Assessment:
    """Compare what the authority says the credential can do with what the request asked for.

    Anything wider than the request is refused, trusted authority or not: then Legion knows the
    credential is too strong. That includes evidence that doesn't validate but still shows more
    than was asked. Missing or malformed evidence is otherwise unverified, never verified. Only a
    trusted authority's evidence can be verified, and only evidence tied to this Action, call and
    Grant is bound. The authority's own `verified` flag plays no part. `seen` holds
    `ref_digest`s of references already used in the run.
    """
    if raw is None:
        return Assessment(Assurance.UNVERIFIED, ("no evidence",))
    try:
        data = raw.model_dump() if isinstance(raw, BaseModel) else raw
        evidence = CredentialEvidence.model_validate(data)
    except (ValidationError, TypeError, ValueError):
        wider = _visibly_wider(raw, request)
        if wider:
            return Assessment(None, ("malformed evidence", *wider))
        return Assessment(Assurance.UNVERIFIED, ("malformed evidence",))

    problems: list[str] = []
    if evidence.authority != request.authority:
        problems.append(f"issued by {printable(evidence.authority)}, not {request.authority}")
    if evidence.provider != request.provider:
        problems.append(f"for provider {printable(evidence.provider)}, not {request.provider}")
    extra = sorted(set(evidence.permissions) - set(request.permissions))
    if extra:
        problems.append(f"{WIDER}: {printable(', '.join(extra))}")
    if not _resource_within(evidence.resource, request.resource):
        problems.append(
            f"resource {printable(str(evidence.resource or 'any'))} is wider than "
            f"{request.resource or 'none'}"
        )
    if evidence.principal != request.principal or evidence.subject != request.subject:
        problems.append("issued to a different principal or agent")
    if evidence.action_hash is not None and evidence.action_hash != request.action_hash:
        problems.append("bound to a different action")
    if evidence.call_id is not None and evidence.call_id != request.call_id:
        problems.append("bound to a different call")
    if (
        evidence.grant_fingerprint is not None
        and evidence.grant_fingerprint != request.grant_fingerprint
    ):
        problems.append("bound to a different grant")
    allowed = timedelta(seconds=request.max_lifetime_s) + CLOCK_SKEW
    if evidence.expires_at <= now:
        problems.append("already expired")
    elif evidence.expires_at > now + allowed or evidence.expires_at - evidence.issued_at > allowed:
        # both what's left and the whole life: an old long-lived credential near its end
        # isn't a short-lived one
        problems.append(f"lives longer than the {request.max_lifetime_s}s allowed")
    if evidence.issued_at > now + CLOCK_SKEW:
        problems.append("issued in the future")
    if ref_digest(request.run_id, evidence.credential_ref) in seen:
        problems.append("credential reference was already used in this run")
    if problems:
        return Assessment(None, tuple(problems), evidence)

    if not trusted:
        return Assessment(Assurance.DECLARED, ("authority isn't trusted",), evidence)
    bound = (
        evidence.action_hash == request.action_hash
        and evidence.call_id == request.call_id
        and evidence.grant_fingerprint == request.grant_fingerprint
    )
    return Assessment(Assurance.BOUND if bound else Assurance.VERIFIED, (), evidence)


def _visibly_wider(raw: Any, request: CredentialRequest) -> list[str]:
    # Evidence that doesn't validate is no evidence, unless what it does say is more than was
    # asked for: that's still a reason to refuse.
    if not isinstance(raw, Mapping):
        return []
    found = []
    perms = raw.get("permissions")
    if isinstance(perms, list | tuple) and any(p not in request.permissions for p in perms):
        found.append(WIDER)
    if "resource" in raw:
        resource = raw["resource"]
        if not isinstance(resource, str | None) or not _resource_within(resource, request.resource):
            found.append("resource is wider than requested")
    return found


def _resource_within(issued: str | None, requested: str | None) -> bool:
    if issued is None:
        # a credential for any resource is only fine if the call names none
        return requested is None
    if requested is None:
        return False
    return issued == requested or glob_contains(requested, issued)


def printable(text: str) -> str:
    # authority-supplied text, with anything that could move a terminal cursor or reorder text
    # replaced; not shortened, so secrets are still whole when they're scrubbed
    return _UNSAFE.sub("?", text)


def clean(text: str) -> str:
    # for the log: printable and short. Scrub secrets before calling this, not after, or a
    # shortened secret no longer matches.
    return printable(text)[:200]


class CredentialBroker:
    """Resolves the credentials a tool call needs, and decides whether they're good enough."""

    def __init__(
        self,
        *,
        resolver: CredentialResolver,
        static: Mapping[str, SecretRef] | None = None,
        mapped: Mapping[str, CredentialMapping] | None = None,
        authorities: Mapping[str, CredentialAuthority] | None = None,
        trusted: frozenset[str] = frozenset(),
        minimum: Assurance = Assurance.UNVERIFIED,
        timeout_s: float = 10.0,
    ) -> None:
        self.resolver = resolver
        self.static = dict(static or {})
        self.mapped = dict(mapped or {})
        self.authorities = dict(authorities or {})
        self.trusted = trusted
        self.minimum = minimum
        self.timeout_s = timeout_s
        both = set(self.static) & set(self.mapped)
        if both:
            raise ConfigError(f"credentials defined twice: {sorted(both)}")
        for name, mapping in self.mapped.items():
            if mapping.authority not in self.authorities:
                raise ConfigError(
                    f"credential {name} uses authority {mapping.authority!r}, which isn't set up"
                )

    def required(self, name: str) -> Assurance:
        mapping = self.mapped.get(name)
        if mapping is None or mapping.minimum.rank < self.minimum.rank:
            return self.minimum
        return mapping.minimum

    def describe(self) -> dict[str, str]:
        """What each credential name stands for, without secrets: part of an approval's binding."""
        out = {name: str(ref) for name, ref in self.static.items()}
        out.update({name: m.describe() for name, m in self.mapped.items()})
        return out

    def static_refs(self) -> list[SecretRef]:
        return list(self.static.values())

    async def obtain(
        self,
        name: str,
        action: Action,
        grant: Grant,
        *,
        run_id: str,
        call_id: str,
        seen: set[str],
        now: datetime,
        remember: Callable[[str], None],
        at_least: Assurance | None = None,
    ) -> Obtained:
        """Get and assess one credential. `remember` is called with any secret the authority
        hands over, before anything else is done with it, so it's scrubbed whatever happens."""
        # policy can only raise what the configuration requires, never lower it
        required = self.required(name)
        if at_least is not None and at_least.rank > required.rank:
            required = at_least
        if name in self.static:
            return await self._static(name, required, remember)
        mapping = self.mapped.get(name)
        if mapping is None:
            raise CredentialUnavailable(f"credential {name!r} is not configured")
        issuer = self.authorities[mapping.authority]
        refused = Obtained(name, mapping.authority, None, required, issuer=issuer)

        request = derive_request(mapping, action, grant, run_id=run_id, call_id=call_id)
        if isinstance(request, str):
            refused.problems = (request,)
            return refused
        refused.request = request
        try:
            async with asyncio.timeout(self.timeout_s):
                # a copy: the authority can't change what Legion compares against and records
                issued = await issuer.issue(dataclasses.replace(request))
        except Exception as exc:
            # only the type: an authority's error text could hold the secret it was minting
            refused.problems = (f"authority failed: {type(exc).__name__}",)
            return refused
        if not isinstance(issued, IssuedCredential) or not isinstance(issued.secret, Secret):
            refused.problems = ("authority returned no credential",)
            return refused
        secret = issued.secret
        remember(secret.reveal())
        refused.issued = secret
        try:
            return self._assess(name, mapping, issuer, request, issued, required, seen, now)
        except Exception as exc:
            # whatever the authority's evidence does when read, it's a refusal, recorded by type
            refused.problems = (f"evidence couldn't be read: {type(exc).__name__}",)
            refused.ref = _usable_ref(issued.credential_ref)
            return refused

    def _assess(
        self,
        name: str,
        mapping: CredentialMapping,
        issuer: CredentialAuthority,
        request: CredentialRequest,
        issued: IssuedCredential,
        required: Assurance,
        seen: set[str],
        now: datetime,
    ) -> Obtained:
        result = assess(
            request, issued.evidence, trusted=mapping.authority in self.trusted, now=now, seen=seen
        )
        ref = _usable_ref(issued.credential_ref)
        problems = list(result.problems)
        if result.evidence is not None:
            if ref is not None and ref != result.evidence.credential_ref:
                problems.append("the credential's reference doesn't match its evidence")
            ref = result.evidence.credential_ref
        got = Obtained(
            name,
            mapping.authority,
            result.assurance,
            required,
            secret=issued.secret,
            evidence=result.evidence,
            request=request,
            problems=tuple(problems),
            issuer=issuer,
            issued=issued.secret,
            ref=ref,
        )
        if result.assurance is None or len(problems) > len(result.problems):
            got.assurance = None
            return self._refuse(got)
        if result.assurance.rank < required.rank:
            got.problems = (*got.problems, f"{result.assurance} is below the {required} required")
            return self._refuse(got)
        # its status with the authority is checked just before dispatch, with everything else
        return got

    async def check(self, got: Obtained, now: datetime) -> str | None:
        """Why a credential issued for this call can't be used for the next attempt: None if it
        can, EXPIRED if it may be replaced, anything else if the call has to stop.

        The authority is asked even when Legion's clock says expired: a credential that is both
        expired and revoked is revoked, and isn't replaced.
        """
        if got.issuer is None:
            return None  # static: nothing to expire or ask about
        if got.ref is None:
            # issued by an authority, but nothing to ask it about
            return UNKNOWN
        try:
            async with asyncio.timeout(self.timeout_s):
                status = await got.issuer.status(got.ref)
        except Exception as exc:
            # unreachable is not "still active"
            return f"couldn't check it's still active: {type(exc).__name__}"
        if status is CredentialStatus.REVOKED:
            return REVOKED
        expired = got.evidence is not None and got.evidence.expires_at <= now
        if status is CredentialStatus.EXPIRED or (status is CredentialStatus.ACTIVE and expired):
            return EXPIRED
        if status is CredentialStatus.ACTIVE:
            return None
        return UNKNOWN

    async def revoke(self, got: Obtained) -> None:
        # best effort: the credential wasn't used, and it's short-lived anyway
        if got.issuer is None or got.ref is None:
            return
        with contextlib.suppress(Exception):
            async with asyncio.timeout(self.timeout_s):
                await got.issuer.revoke(got.ref)

    async def _static(
        self, name: str, required: Assurance, remember: Callable[[str], None]
    ) -> Obtained:
        secret = await self.resolver.resolve(self.static[name])
        remember(secret.reveal())
        got = Obtained(name, "static", Assurance.UNVERIFIED, required, secret=secret, issued=secret)
        if required.rank > Assurance.UNVERIFIED.rank:
            got.problems = (f"static credentials are unverified; {required} is required",)
            return self._refuse(got)
        return got

    @staticmethod
    def _refuse(got: Obtained) -> Obtained:
        # the secret isn't handed on; the reference stays so the credential can be revoked
        got.secret = None
        return got


def _usable_ref(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(REF_CHARS, value) else None
