# Port to an external identity/authorization service: who is acting, whether they may, and
# whether they've been killed. Issuing credentials is a separate port (ports/credentials.py).
# Implementations live in legion.adapters and are only loaded when configured.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from legion.domain.action import Action
from legion.domain.grant import Grant


class KillState(StrEnum):
    ACTIVE = "active"
    KILLED = "killed"


@dataclass(frozen=True)
class AgentIdentity:
    agent_ref: str
    source: str
    external_id: str | None = None


@dataclass(frozen=True)
class ExternalDecision:
    allowed: bool
    source: str
    reason: str = ""
    # what the decision was based on, recorded on action.authorized; never secrets
    details: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ServerCredentialClaim:
    # What an identity service says about the credential an MCP server holds for the whole
    # process. Recorded next to each call; it never raises that call's credential assurance
    # above declared, because it isn't bound to the call.
    source: str
    subject: str
    scopes: tuple[str, ...] = ()
    claimed_verified: bool = False


@dataclass(frozen=True)
class ExternalEvidenceRef:
    source: str
    kind: str
    ref: str


class IdentityPort(Protocol):
    async def agent_identity(self, agent_ref: str) -> AgentIdentity: ...

    async def authorize(self, action: Action, identity: AgentIdentity) -> ExternalDecision: ...

    async def kill_state(self, identity: AgentIdentity) -> KillState: ...

    async def on_delegation(self, parent: Grant, child: Grant) -> None: ...

    async def evidence(self, action_hash: str) -> list[ExternalEvidenceRef]: ...

    async def credential_evidence(self, server: str) -> ServerCredentialClaim | None: ...


class NullIdentityPort:
    # default when no identity service is configured: never killed, never vetoes
    source = "local"

    async def agent_identity(self, agent_ref: str) -> AgentIdentity:
        return AgentIdentity(agent_ref=agent_ref, source=self.source)

    async def authorize(self, action: Action, identity: AgentIdentity) -> ExternalDecision:
        return ExternalDecision(allowed=True, source=self.source, reason="no external authority")

    async def kill_state(self, identity: AgentIdentity) -> KillState:
        return KillState.ACTIVE

    async def on_delegation(self, parent: Grant, child: Grant) -> None:
        return None

    async def evidence(self, action_hash: str) -> list[ExternalEvidenceRef]:
        return []

    async def credential_evidence(self, server: str) -> ServerCredentialClaim | None:
        return None
