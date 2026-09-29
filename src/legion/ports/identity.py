"""The boundary to an external identity and authorization authority such as NIA or MIA.

Legion enforces inside one run. Identity, credential issuance, per-agent grants, kill state and
risk across runs belong to the external authority. The effective permission for an action is
Legion's grant intersected with `authorize`, and `kill_state` is checked before every model call
and every tool execution. Legion must work with no authority at all, which is what
`NullIdentityPort` is.

Mapping to the known authorities (adapters are Phase 7):

    method           NIA                                    MIA
    kill_state       kill sentinel / revoked credential     mandate revoked or suspect
    authorize        gateway decision for the tool call     authz.authorize
    credential       POST /agents/{ref}/credentials         token exchange
    on_delegation    register child, grant a subset        mandates.delegate
    evidence         incidents and audit records            audit records
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from legion.access.secrets import SecretRef
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


@dataclass(frozen=True)
class ExternalEvidenceRef:
    source: str
    kind: str
    ref: str


class IdentityPort(Protocol):
    async def agent_identity(self, agent_ref: str) -> AgentIdentity: ...

    async def credential(self, agent_ref: str, purpose: str) -> SecretRef | None: ...

    async def authorize(self, action: Action, identity: AgentIdentity) -> ExternalDecision: ...

    async def kill_state(self, identity: AgentIdentity) -> KillState: ...

    async def on_delegation(self, parent: Grant, child: Grant) -> None: ...

    async def evidence(self, action_hash: str) -> list[ExternalEvidenceRef]: ...


class NullIdentityPort:
    """No external authority: local identity, never killed, no external veto."""

    source = "local"

    async def agent_identity(self, agent_ref: str) -> AgentIdentity:
        return AgentIdentity(agent_ref=agent_ref, source=self.source)

    async def credential(self, agent_ref: str, purpose: str) -> SecretRef | None:
        return None

    async def authorize(self, action: Action, identity: AgentIdentity) -> ExternalDecision:
        return ExternalDecision(allowed=True, source=self.source, reason="no external authority")

    async def kill_state(self, identity: AgentIdentity) -> KillState:
        return KillState.ACTIVE

    async def on_delegation(self, parent: Grant, child: Grant) -> None:
        return None

    async def evidence(self, action_hash: str) -> list[ExternalEvidenceRef]:
        return []
