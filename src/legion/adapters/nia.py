# NIA as Legion's identity authority, over NIA's HTTP API. Nothing else in Legion imports this;
# the config loader builds it when `identity.provider` is `nia`.
#
# What it uses: GET /agents/{ref} on NIA's control plane, which returns the agent's record and a
# live check of its kill sentinel. What it doesn't: NIA credentials, grants or its gateway. So
# this gives Legion identity and kill state; it is not a credential authority (ADR 0020).
#
# Any doubt means no: an agent with no mapping, one NIA doesn't know, an answer that doesn't
# say it checked the kill sentinel, a wrong agent in the answer, an error, a timeout, a redirect
# or anything that doesn't parse. Legion then refuses to start or continue that agent's work.

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from legion.access.base import check_endpoint
from legion.access.secrets import CredentialResolver, SecretRef
from legion.domain.action import Action
from legion.domain.errors import IdentityUnavailable, IdentityUnknown, LegionError
from legion.domain.grant import Grant
from legion.ports.identity import (
    AgentIdentity,
    ExternalDecision,
    ExternalEvidenceRef,
    KillState,
    ServerCredentialClaim,
)

# NIA refs Legion will ask about: no slash (it's a path segment), and not just dots, which a URL
# library would treat as "this directory" or "the one above"
NIA_REF = re.compile(r"(?!\.+\Z)[A-Za-z0-9._:@-]{1,128}")
MAX_RESPONSE_BYTES = 64 * 1024
_STATES = {"active": KillState.ACTIVE, "killed": KillState.KILLED, "suspended": KillState.KILLED}


def nia_endpoint(value: str) -> str:
    """Check a NIA endpoint that a token will be sent to. Shared by both NIA adapters."""
    check_endpoint(value, carries_credentials=True)
    parts = urlsplit(value)
    # refs go in the path; a query or fragment here would move them somewhere else
    if parts.query or parts.fragment or ";" in parts.path or value.rstrip().endswith("?"):
        raise ValueError("the NIA endpoint can't have a query, fragment or parameters")
    try:
        httpx.URL(value).port  # noqa: B018
        parts.port  # noqa: B018
    except (httpx.InvalidURL, ValueError):
        raise ValueError("the NIA endpoint's port isn't valid") from None
    return value.rstrip("/")


class NiaIdentityConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    provider: Literal["nia"]
    # NIA's control plane (cmd/api), e.g. https://nia.internal:8080
    endpoint: str
    # an operator token with read permission, as a secret reference
    credential: str
    timeout_s: float = Field(default=5.0, gt=0, le=30)
    # how many times to try one lookup when the connection fails or NIA answers 502/503/504
    attempts: int = Field(default=2, ge=1, le=3)
    # Legion agent name -> NIA agent ref. An agent that isn't here doesn't run: identities come
    # from this file, never from the model or from anything NIA says.
    agents: dict[str, str] = Field(min_length=1)

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        return nia_endpoint(value)

    @field_validator("credential")
    @classmethod
    def _credential(cls, value: str) -> str:
        SecretRef.parse(value)
        return value

    @field_validator("agents")
    @classmethod
    def _agents(cls, value: dict[str, str]) -> dict[str, str]:
        for ref in value.values():
            if not NIA_REF.fullmatch(ref):
                raise ValueError("NIA agent refs are 1-128 characters from [A-Za-z0-9._:@-]")
        return value


class NiaIdentityPort:
    source = "nia"

    def __init__(
        self,
        config: NiaIdentityConfig,
        resolver: CredentialResolver,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self._resolver = resolver
        self._now = now
        # No redirects: the token goes to the configured endpoint and nowhere else. No
        # compression: a small compressed answer could still expand past the size cap.
        self._client = httpx.AsyncClient(
            base_url=config.endpoint,
            timeout=httpx.Timeout(config.timeout_s),
            follow_redirects=False,
            # no proxy, CA bundle or netrc from the environment: the token goes to the
            # configured endpoint and nowhere else
            trust_env=False,
            transport=transport,
            auth=_Bearer(resolver, SecretRef.parse(config.credential)),
            headers={"Accept-Encoding": "identity"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # IdentityPort

    async def agent_identity(self, agent_ref: str) -> AgentIdentity:
        ref = self._ref(agent_ref)
        # unknown or unreachable fails here, before the run exists; a killed agent is caught by
        # the kill check that comes right after
        await self._state(ref)
        return AgentIdentity(agent_ref=agent_ref, source=self.source, external_id=ref)

    async def kill_state(self, identity: AgentIdentity) -> KillState:
        state, _ = await self._state(self._identity_ref(identity))
        return state

    async def authorize(self, action: Action, identity: AgentIdentity) -> ExternalDecision:
        # NIA's API has no per-action decision for an operator caller; this is identity state
        # only, checked again for this call
        ref = self._identity_ref(identity)
        state, details = await self._state(ref)
        allowed = state is KillState.ACTIVE
        reason = f"{ref} {details['state']}" + ("" if allowed else ", refused")
        return ExternalDecision(allowed=allowed, source=self.source, reason=reason, details=details)

    async def on_delegation(self, parent: Grant, child: Grant) -> None:
        # NIA doesn't do delegation; what it can say is whether the child's own identity exists
        # and is active. The child needs its own mapping: it doesn't borrow the parent's.
        for grant in (parent, child):
            ref = self._ref(grant.identity.agent_ref)
            state, details = await self._state(ref)
            if state is not KillState.ACTIVE:
                raise IdentityUnavailable(f"NIA reports {ref} {details['state']}")

    async def evidence(self, action_hash: str) -> list[ExternalEvidenceRef]:
        return []

    async def credential_evidence(self, server: str) -> ServerCredentialClaim | None:
        # NIA credentials go through nia_credentials, not the identity port
        return None

    # the one lookup everything goes through

    def _ref(self, agent_ref: str) -> str:
        ref = self.config.agents.get(agent_ref)
        if ref is None:
            raise IdentityUnknown(f"no NIA identity is configured for agent {agent_ref}")
        return ref

    def _identity_ref(self, identity: AgentIdentity) -> str:
        # always from the mapping: an identity made by something else (or before a config
        # change) doesn't get to name its own NIA ref
        ref = self._ref(identity.agent_ref)
        if identity.external_id is not None and identity.external_id != ref:
            raise IdentityUnavailable(
                f"agent {identity.agent_ref} was {identity.external_id}, the mapping now says {ref}"
            )
        return ref

    async def _state(self, ref: str) -> tuple[KillState, dict[str, str]]:
        body = await self._get(ref)
        # the answer has to be about the agent asked for, with a live kill check behind it
        if not isinstance(body, dict) or body.get("Ref") != ref:
            raise IdentityUnavailable(f"NIA's answer for {ref} was about some other agent")
        effective = body.get("effective_state")
        if not isinstance(effective, str) or effective not in _STATES:
            raise IdentityUnavailable(f"NIA gave no usable state for {ref}")
        if body.get("kill_sentinel_checked") is not True:
            raise IdentityUnavailable(f"NIA couldn't confirm {ref}'s kill state")
        details = {
            "provider": self.source,
            "ref": ref,
            "state": effective,
            "checked_at": self._now().isoformat(),
        }
        return _STATES[effective], details

    async def _get(self, ref: str) -> Any:
        # Tokens go in the header only, and no error text from NIA or httpx is passed on: a
        # message could carry the request or its headers.
        path = f"/agents/{quote(ref, safe='')}"
        last = "no answer"
        for attempt in range(self.config.attempts):
            if attempt:
                await asyncio.sleep(0.2 * attempt)
            try:
                # the whole exchange, not just each read: a slow trickle can't outlast it
                async with (
                    asyncio.timeout(self.config.timeout_s),
                    self._client.stream("GET", path) as response,
                ):
                    status = response.status_code
                    if status in (502, 503, 504):
                        last = f"NIA answered {status}"
                        continue
                    if status == 404:
                        raise IdentityUnknown(f"NIA has no agent {ref}")
                    if status in (401, 403):
                        raise IdentityUnavailable(f"NIA refused Legion's credential ({status})")
                    if status != 200:
                        raise IdentityUnavailable(f"NIA answered {status} for {ref}")
                    # Legion asked for no compression; a compressed answer is refused before
                    # it's read, since a small body could expand far past the size cap
                    encoding = response.headers.get("content-encoding", "identity").lower()
                    if encoding.strip() != "identity":
                        raise IdentityUnavailable(f"NIA's answer for {ref} was compressed")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw += chunk
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise IdentityUnavailable(f"NIA's answer for {ref} was too large")
            except LegionError:
                raise
            except (httpx.TransportError, TimeoutError) as exc:
                last = f"couldn't reach NIA ({type(exc).__name__})"
                continue
            except Exception as exc:
                raise IdentityUnavailable(f"NIA lookup failed ({type(exc).__name__})") from None
            try:
                return json.loads(bytes(raw), parse_constant=_no_constants, parse_float=_finite)
            except (ValueError, RecursionError):
                raise IdentityUnavailable(f"NIA's answer for {ref} isn't valid JSON") from None
        raise IdentityUnavailable(f"{last}, giving up on {ref}")


class _Bearer(httpx.Auth):
    # Adds the token as the request goes out, so it's never a local in the adapter's own code
    # where a traceback would show it.

    def __init__(
        self,
        resolver: CredentialResolver,
        ref: SecretRef,
        error: Callable[[str], Exception] = IdentityUnavailable,
    ) -> None:
        self._resolver = resolver
        self._ref = ref
        self._error = error

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        secret = await self._resolver.resolve(self._ref)
        value = secret.reveal()
        # printable ASCII only: anything else can't be sent as a header value as it is
        if not all(0x21 <= ord(c) <= 0x7E for c in value):
            raise self._error("Legion's NIA credential isn't a usable token")
        request.headers["Authorization"] = f"Bearer {value}"
        del secret, value
        yield request


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} isn't JSON")


def _finite(text: str) -> float:
    value = float(text)
    if value in (float("inf"), float("-inf")):
        raise ValueError("number out of range")
    return value
