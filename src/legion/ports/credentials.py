# Port to whatever issues credentials for tool calls: anything that can scope a credential to one
# call (an adapter in legion.adapters, or an operator module). Legion asks for exactly what an
# authorized Action needs and checks what comes back. The authority issues and vouches for
# credentials; it never decides what Legion allows.
#
# Three things that are easy to mix up and mustn't be:
#   identity              who is acting (principal, agent, delegation chain)
#   logical authority     what the task's Grant lets that identity do
#   credential authority  what the downstream system will accept the credential for
# The credential itself is secret material. Credential authority is metadata about it.

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from legion.access.secrets import Secret


class Assurance(StrEnum):
    # nothing trustworthy is known about what the credential can do (static secrets)
    UNVERIFIED = "unverified"
    # the operator or an untrusted authority says what it can do; nothing checked it
    DECLARED = "declared"
    # a trusted authority says what it can do, and that's within what the Action needs
    VERIFIED = "verified"
    # verified, and tied to this principal, Action, Grant and expiry
    BOUND = "bound"

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK = {Assurance.UNVERIFIED: 0, Assurance.DECLARED: 1, Assurance.VERIFIED: 2, Assurance.BOUND: 3}


class CredentialStatus(StrEnum):
    # What an authority says about a credential it issued. Expired and revoked are different:
    # an expired credential can be replaced, a revoked one can't be worked around. Any other
    # answer counts as not knowing.
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


# An authority's reference for a credential: printable, short, nothing to clean away, so what's
# recorded is exactly what was compared. (Pydantic's regex engine: `$` is the true end.)
REF_CHARS = r"[A-Za-z0-9._:/@+=-]{1,120}"
REF_PATTERN = rf"^{REF_CHARS}$"


@dataclass(frozen=True)
class CredentialRequest:
    """What Legion asks an authority for, built only from the authorized Action and config."""

    authority: str
    provider: str
    permissions: tuple[str, ...]
    resource: str | None
    # the Legion capabilities that authorized the call, for the authority's records
    capabilities: tuple[str, ...]
    principal: str
    subject: str
    on_behalf_of: tuple[str, ...]
    run_id: str
    task_id: str
    # Legion's own id for the call (events.projections.legion_call_id), never the model's or
    # provider's tool call id: two identical calls differ here, retries of one call don't
    call_id: str
    action_hash: str
    grant_id: str
    grant_fingerprint: str
    tool: str
    max_lifetime_s: int
    # The identity authority's id for the subject (AgentIdentity.external_id), when there is
    # one. An authority that knows identities should issue to exactly this principal.
    external_principal: str | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "authority": self.authority,
            "provider": self.provider,
            "permissions": list(self.permissions),
            "resource": self.resource,
            "max_lifetime_s": self.max_lifetime_s,
        }


# Evidence goes into the event log, so it's bounded: an authority can't fill the log with it.
_Text = Annotated[str, Field(max_length=512)]


class CredentialEvidence(BaseModel):
    """What an authority says about a credential it issued. Legion checks every field."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authority: _Text
    credential_ref: str = Field(pattern=REF_PATTERN)
    provider: _Text
    principal: _Text
    subject: _Text
    # the authority's own id for who the credential was issued to, when it has one
    external_principal: _Text | None = None
    permissions: tuple[Annotated[str, Field(min_length=1, max_length=200)], ...] = Field(
        max_length=64
    )
    # None means any resource
    resource: _Text | None
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    # Binding: the Action's hash, the call within the task (two identical calls share a hash),
    # and the Grant it runs under.
    action_hash: _Text | None = None
    call_id: _Text | None = None
    grant_fingerprint: _Text | None = None
    revocation_ref: _Text | None = None
    # The authority's own opinion. Recorded, never relied on: assurance is Legion's decision.
    verified: bool = False


class UnknownCredential(Exception):
    """Raised by CredentialAuthority.status or revoke: the authority has no record of this
    reference (or not one it will show this caller). Legion treats it as not active."""


@dataclass(frozen=True)
class IssuedCredential:
    secret: Secret
    # Whatever the authority sent back: a CredentialEvidence, a mapping, or None. Legion
    # validates it; a malformed one counts as no evidence.
    evidence: Any = None
    # The authority's reference for it, outside the evidence so Legion can ask about it and
    # revoke it even when the evidence is unusable. Must match REF_PATTERN and the evidence.
    credential_ref: str | None = None
    # Parts of the secret that are secret on their own and could turn up without the rest (the
    # half after the reference in a "<ref>.<secret>" token, say). Scrubbed like the secret.
    also_scrub: tuple[Secret, ...] = ()

    def __repr__(self) -> str:
        return "IssuedCredential(secret=Secret(****))"


class CredentialAuthority(Protocol):
    name: str

    async def issue(self, request: CredentialRequest) -> IssuedCredential: ...

    async def status(self, credential_ref: str) -> CredentialStatus: ...

    async def revoke(self, credential_ref: str) -> None: ...
