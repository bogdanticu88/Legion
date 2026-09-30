# NIA's scoped credentials behind Legion's generic CredentialAuthority port. Nothing in Legion's
# kernel, domain or ports imports this; the config loader builds it when a credential authority
# says `provider: nia`.
#
# What it uses: NIA's control plane API, with an operator token holding NIA's issuer role:
#   POST /agents/{ref}/scoped-credentials                      issue
#   GET  /agents/{ref}/scoped-credentials/{credential_ref}     status
#   POST /agents/{ref}/scoped-credentials/{credential_ref}/revoke
#
# Its job is translation, not judgement. It builds NIA's request from Legion's request (which
# Legion built from the authorized Action and the operator's mapping), and turns NIA's answer
# into Legion's generic evidence without deciding whether it's good enough: Legion's own
# assessment does that, field by field, as for any authority. Where NIA says something that
# doesn't fit (another principal, another issuer), the evidence says so and Legion refuses it;
# the adapter never makes a disagreement look like agreement.

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from legion.access.secrets import CredentialResolver, Secret, SecretRef
from legion.adapters.nia import (
    MAX_RESPONSE_BYTES,
    NIA_REF,
    _Bearer,
    _finite,
    _no_constants,
    nia_endpoint,
)
from legion.ports.credentials import (
    CredentialRequest,
    CredentialStatus,
    IssuedCredential,
    UnknownCredential,
)

# What NIA accepts (docs/SCOPED_CREDENTIALS.md in NIA). Checked here too, so a value NIA would
# refuse never leaves Legion, and nothing outside plain ASCII is sent at all.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
_RESOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")
_CREDENTIAL_REF = re.compile(r"nia-sc-[0-9a-f]{32}")
_TOKEN_SECRET = re.compile(r"[A-Za-z0-9_-]{16,128}")
EVIDENCE_FORMAT = "nia.scoped-credential.evidence/v1"
MAX_REMEMBERED = 10_000
_STATUSES = {
    "active": CredentialStatus.ACTIVE,
    "expired": CredentialStatus.EXPIRED,
    "revoked": CredentialStatus.REVOKED,
}


class NiaCredentialError(Exception):
    """NIA couldn't give an answer Legion can use. The message is built from Legion's own
    strings and status codes, never from anything NIA sent."""


class NiaCredentialConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    provider: Literal["nia"]
    # NIA's control plane (cmd/api); plain http only to a literal loopback address
    endpoint: str
    # an operator token with NIA's issuer role, as a secret reference. Not the identity
    # adapter's viewer token: that one can't issue, and this one isn't needed to read identity.
    credential: str
    # only a trusted authority's evidence can make a credential verified or bound
    trusted: bool = False
    # the NIA gateway audience the credential is for; a credential mapping's `provider` has to
    # name it, so what Legion asks for and what NIA issues are compared as one value
    audience: str
    # the issuer name NIA puts in its evidence
    issuer: str = "nia"
    timeout_s: float = Field(default=5.0, gt=0, le=30)
    # tries per status or revoke; issuance is only retried when nothing reached NIA
    attempts: int = Field(default=2, ge=1, le=3)
    # Legion agent name -> NIA agent ref. Left out, the identity block's mapping is used; given
    # in both places, they have to agree.
    agents: dict[str, str] | None = None

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        return nia_endpoint(value)

    @field_validator("credential")
    @classmethod
    def _credential(cls, value: str) -> str:
        SecretRef.parse(value)
        return value

    @field_validator("audience", "issuer")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("must be 1-128 characters from [A-Za-z0-9._:/@+-]")
        return value

    @field_validator("agents")
    @classmethod
    def _agents(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is not None:
            if not value:
                raise ValueError("agents can't be empty")
            for ref in value.values():
                if not NIA_REF.fullmatch(ref):
                    raise ValueError("NIA agent refs are 1-128 characters from [A-Za-z0-9._:@-]")
        return value


class NiaCredentialAuthority:
    """A CredentialAuthority backed by NIA's scoped credentials."""

    def __init__(
        self,
        name: str,
        config: NiaCredentialConfig,
        agents: dict[str, str],
        resolver: CredentialResolver,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not agents:
            raise ValueError("the NIA credential authority needs an agent -> NIA ref mapping")
        self.name = name
        self.config = config
        self._agents = dict(agents)
        # which NIA principal each credential this process issued belongs to: status and revoke
        # are addressed under it, and a reference this process didn't issue is unknown
        self._issued: dict[str, str] = {}
        self._client = httpx.AsyncClient(
            base_url=config.endpoint,
            timeout=httpx.Timeout(config.timeout_s),
            follow_redirects=False,
            # no proxy, CA bundle or netrc from the environment: the issuer token and the scoped
            # secrets go to the configured endpoint and nowhere else
            trust_env=False,
            transport=transport,
            auth=_Bearer(resolver, SecretRef.parse(config.credential), NiaCredentialError),
            headers={"Accept-Encoding": "identity"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # CredentialAuthority

    async def issue(self, request: CredentialRequest) -> IssuedCredential:
        principal = self._principal(request)
        if request.resource is None:
            # NIA scopes every credential to one resource, and there's no value meaning "no
            # resource" that both sides understand. Inventing one would be scope nobody chose.
            raise NiaCredentialError("NIA credentials need a resource; this call names none")
        if request.provider != self.config.audience:
            raise NiaCredentialError(
                f"the credential mapping's provider {request.provider!r} isn't this NIA "
                f"authority's audience {self.config.audience!r}"
            )
        if not _RESOURCE.fullmatch(request.resource):
            raise NiaCredentialError("the resource isn't one NIA accepts")
        if not request.permissions or any(not _NAME.fullmatch(p) for p in request.permissions):
            raise NiaCredentialError("a permission isn't one NIA accepts")
        body = {
            "permissions": sorted(request.permissions),
            "resource": request.resource,
            "audience": self.config.audience,
            "action_hash": request.action_hash,
            "call_id": request.call_id,
            "grant_fingerprint": request.grant_fingerprint,
            "ttl_seconds": request.max_lifetime_s,
        }
        path = f"/agents/{quote(principal, safe='')}/scoped-credentials"
        # Not retried once anything may have reached NIA: a second issuance would leave a live
        # credential Legion never heard about.
        status, answer = await self._send("POST", path, body, attempts=1, retry_connect=True)
        if status != 201:
            raise NiaCredentialError(f"NIA refused to issue ({status})")
        return await self._issued_credential(request, principal, answer)

    async def status(self, credential_ref: str) -> CredentialStatus:
        principal = self._issued.get(credential_ref)
        if principal is None:
            raise UnknownCredential("not a credential this authority issued")
        path = (
            f"/agents/{quote(principal, safe='')}/scoped-credentials/"
            f"{quote(credential_ref, safe='')}"
        )
        status, answer = await self._send("GET", path, None, attempts=self.config.attempts)
        if status == 404:
            raise UnknownCredential("NIA has no such credential for this agent")
        if status != 200:
            raise NiaCredentialError(f"NIA answered {status} to a status check")
        if (
            not isinstance(answer, dict)
            or answer.get("credential_ref") != credential_ref
            or answer.get("principal") != principal
        ):
            raise NiaCredentialError("NIA's status answer was about some other credential")
        value = answer.get("status")
        if value == "unknown":
            raise UnknownCredential("NIA has no such credential for this agent")
        if not isinstance(value, str) or value not in _STATUSES:
            raise NiaCredentialError("NIA gave no usable status")
        return _STATUSES[value]

    async def revoke(self, credential_ref: str) -> None:
        principal = self._issued.get(credential_ref)
        if principal is None:
            raise UnknownCredential("not a credential this authority issued")
        await self._revoke(principal, credential_ref)

    # the rest

    def _principal(self, request: CredentialRequest) -> str:
        # Always from the operator's mapping, by the Legion agent the Grant names. An identity
        # authority's id for the agent, when there is one, has to be the same ref.
        principal = self._agents.get(request.subject)
        if principal is None:
            raise NiaCredentialError(f"no NIA identity is configured for agent {request.subject}")
        if request.external_principal is not None and request.external_principal != principal:
            raise NiaCredentialError(
                f"agent {request.subject} is {request.external_principal} to the identity "
                f"authority but {principal} here"
            )
        return principal

    async def _issued_credential(
        self, request: CredentialRequest, principal: str, answer: Any
    ) -> IssuedCredential:
        if not isinstance(answer, dict):
            raise NiaCredentialError("NIA's issuance answer isn't an object")
        ref = answer.get("credential_ref")
        token = answer.get("token")
        if not isinstance(ref, str) or not _CREDENTIAL_REF.fullmatch(ref):
            raise NiaCredentialError("NIA's issuance answer has no usable credential reference")
        # from here on NIA has a live credential; if Legion can't use it, it's revoked
        self._remember(ref, principal)
        secret = token.partition(".")[2] if isinstance(token, str) else ""
        if (
            not isinstance(token, str)
            or not token.startswith(ref + ".")
            or not _TOKEN_SECRET.fullmatch(secret)
        ):
            await self._discard(principal, ref)
            raise NiaCredentialError("NIA's issuance answer has no usable token for its reference")
        raw = answer.get("evidence")
        evidence = self._evidence(request, ref, raw) if isinstance(raw, dict) else None
        # the reference is shown in Legion's log and CLI; the part after it is the secret, and
        # is scrubbed on its own too
        return IssuedCredential(
            secret=Secret(token),
            evidence=evidence,
            credential_ref=ref,
            also_scrub=(Secret(secret),),
        )

    def _evidence(self, request: CredentialRequest, ref: str, raw: dict[str, Any]) -> Any:
        # NIA's evidence in Legion's generic shape, every field carried over as NIA stated it
        # except where Legion's shape needs one of its own names. Wrong types are left wrong, so
        # Legion's validation sees them. Nothing here decides assurance, and NIA's own opinion
        # of the credential isn't carried over at all.
        if raw.get("format") != EVIDENCE_FORMAT:
            return None
        nia_principal = raw.get("principal")
        return {
            # the authority is this configured authority only when NIA names the issuer it's
            # configured with; anything else can't equal a Legion authority name
            "authority": self.name
            if raw.get("issuer") == self.config.issuer
            else f"unexpected NIA issuer {raw.get('issuer')!r}",
            "credential_ref": raw.get("credential_ref"),
            "provider": raw.get("audience"),
            # NIA doesn't know Legion's principal (the human or service the run is for); what it
            # evidences is the agent, so Legion's principal is carried from the request
            "principal": request.principal,
            # the Legion agent NIA's principal is mapped to, when it's exactly one; anything
            # else can't match the request
            "subject": self._subject_for(nia_principal),
            "external_principal": nia_principal,
            "permissions": raw.get("permissions"),
            "resource": raw.get("resource"),
            "issued_at": raw.get("issued_at"),
            "expires_at": raw.get("expires_at"),
            "action_hash": raw.get("action_hash"),
            "call_id": raw.get("call_id"),
            "grant_fingerprint": raw.get("grant_fingerprint"),
            "revocation_ref": ref if raw.get("credential_ref") == ref else None,
        }

    def _subject_for(self, nia_principal: Any) -> Any:
        if not isinstance(nia_principal, str):
            return nia_principal
        names = [name for name, ref in self._agents.items() if ref == nia_principal]
        if len(names) == 1:
            return names[0]
        # unmapped, or mapped from several Legion agents (which Legion can't tell apart)
        return f"unmapped NIA principal {nia_principal}"

    def _remember(self, ref: str, principal: str) -> None:
        # Bounded: a long-running process issuing all day doesn't grow without limit. A
        # reference that fell off answers unknown, which Legion refuses rather than trusts.
        self._issued[ref] = principal
        while len(self._issued) > MAX_REMEMBERED:
            del self._issued[next(iter(self._issued))]

    async def _discard(self, principal: str, ref: str) -> None:
        # best effort: the credential is short-lived and Legion never used it
        with contextlib.suppress(Exception):
            await self._revoke(principal, ref)

    async def _revoke(self, principal: str, ref: str) -> None:
        path = (
            f"/agents/{quote(principal, safe='')}/scoped-credentials/{quote(ref, safe='')}/revoke"
        )
        status, answer = await self._send(
            "POST", path, {"reason": "revoked by Legion"}, attempts=self.config.attempts
        )
        if status == 404:
            raise UnknownCredential("NIA has no such credential for this agent")
        if status != 200:
            raise NiaCredentialError(f"NIA answered {status} to a revocation")
        if (
            not isinstance(answer, dict)
            or answer.get("credential_ref") != ref
            or answer.get("status") != "revoked"
        ):
            raise NiaCredentialError("NIA's revocation answer was about some other credential")

    async def _send(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        *,
        attempts: int,
        retry_connect: bool = False,
    ) -> tuple[int, Any]:
        """One call to NIA: the status code and the parsed JSON body (None when there's none
        to read). Tokens go in the header only, and no text from NIA or httpx is passed on."""
        last = "no answer"
        # a connection that was never made can always be tried again: nothing reached NIA
        tries = max(attempts, 2) if retry_connect else attempts
        for attempt in range(tries):
            if attempt:
                await asyncio.sleep(0.2 * attempt)
            try:
                async with (
                    asyncio.timeout(self.config.timeout_s),
                    self._client.stream(method, path, json=body) as response,
                ):
                    status = response.status_code
                    if status in (502, 503, 504) and attempt + 1 < attempts:
                        last = f"NIA answered {status}"
                        continue
                    encoding = response.headers.get("content-encoding", "identity").lower()
                    if encoding.strip() != "identity":
                        raise NiaCredentialError("NIA's answer was compressed")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw += chunk
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise NiaCredentialError("NIA's answer was too large")
            except NiaCredentialError:
                raise
            except httpx.ConnectError as exc:
                last = f"couldn't reach NIA ({type(exc).__name__})"
                continue
            except (httpx.TransportError, TimeoutError) as exc:
                if retry_connect and attempts == 1:
                    # the request may have reached NIA; don't send it again
                    raise NiaCredentialError(
                        f"lost the connection to NIA ({type(exc).__name__})"
                    ) from None
                last = f"couldn't reach NIA ({type(exc).__name__})"
                if attempt + 1 >= attempts:
                    break
                continue
            except Exception as exc:
                raise NiaCredentialError(f"NIA request failed ({type(exc).__name__})") from None
            if not raw:
                return status, None
            try:
                return status, json.loads(
                    bytes(raw), parse_constant=_no_constants, parse_float=_finite
                )
            except (ValueError, RecursionError):
                if status in (200, 201):
                    raise NiaCredentialError("NIA's answer isn't valid JSON") from None
                return status, None
        raise NiaCredentialError(f"{last}, giving up")


# building one from legion.yaml; the loader calls these only when a credential authority says
# `provider: nia`


def validate_config(raw: dict[str, Any]) -> NiaCredentialConfig:
    try:
        return NiaCredentialConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError("; ".join(e["msg"] for e in exc.errors()[:5])) from None


def agents_for(
    name: str, config: NiaCredentialConfig, identity: dict[str, Any] | None
) -> dict[str, str]:
    """The Legion agent -> NIA ref mapping this authority issues under: its own, or the NIA
    identity block's. Both given, they must agree; and the two use different tokens, since the
    identity port only needs NIA's viewer role and this one needs issuer."""
    nia_identity = identity if identity is not None and identity.get("provider") == "nia" else None
    if nia_identity is not None and nia_identity.get("credential") == config.credential:
        raise ValueError(
            f"credential authority {name} uses the identity block's token; give it its own, "
            f"with NIA's issuer role (the identity token only needs viewer)"
        )
    shared = nia_identity.get("agents") if nia_identity is not None else None
    if config.agents is not None and shared is not None and config.agents != shared:
        raise ValueError(
            f"credential authority {name} maps agents differently from the identity block"
        )
    agents = config.agents if config.agents is not None else shared
    if not agents:
        raise ValueError(
            f"credential authority {name} needs `agents:`, or a NIA identity block to take them "
            f"from"
        )
    return dict(agents)


def check(
    name: str,
    config: NiaCredentialConfig,
    identity: dict[str, Any] | None,
    kernel_timeout_s: float,
) -> None:
    agents_for(name, config, identity)
    if config.timeout_s >= kernel_timeout_s:
        # Legion gives up on issue() after credential_policy.timeout_s; if that could happen
        # while NIA is still answering, NIA would hold a live credential Legion never heard of
        raise ValueError(
            f"credential authority {name}'s timeout_s ({config.timeout_s}) has to be shorter "
            f"than credential_policy.timeout_s ({kernel_timeout_s})"
        )


def from_config(
    name: str, raw: dict[str, Any], identity: dict[str, Any] | None, resolver: CredentialResolver
) -> tuple[NiaCredentialAuthority, SecretRef, bool]:
    config = validate_config(raw)
    authority = NiaCredentialAuthority(name, config, agents_for(name, config, identity), resolver)
    return authority, SecretRef.parse(config.credential), config.trusted
