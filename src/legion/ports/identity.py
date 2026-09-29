# Port to an external identity/authorization service (NIA or MIA). Adapters come in Phase 7;
# see ARCHITECTURE.md for how the methods map onto each.

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
    # default when no NIA/MIA is configured: never killed, never vetoes
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
