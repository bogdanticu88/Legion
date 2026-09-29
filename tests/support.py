"""Builders shared by the tests. Everything runs on the scripted provider and in memory."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from legion.access.secrets import CredentialResolver, SecretRef
from legion.artifacts import MemoryArtifactStore
from legion.authority.policy import PolicyDecisionPoint, Rule, RuleTablePolicy, default_rules
from legion.domain.action import EffectClass
from legion.domain.agent import AgentSpec
from legion.domain.budget import BudgetLimits
from legion.domain.capability import Capability
from legion.domain.principal import Principal, PrincipalKind
from legion.events.store import EventStore, MemoryEventStore
from legion.events.types import Event, EventType
from legion.kernel.runtime import Legion
from legion.kernel.services import RetryPolicy
from legion.models.resolver import ModelBinding, ModelResolver, Pricing
from legion.models.scripted import ScriptedProvider, Step
from legion.ports.identity import IdentityPort
from legion.tools.base import ToolContext
from legion.tools.native import tool
from legion.tools.registry import ToolRegistry

PRINCIPAL = Principal(kind=PrincipalKind.HUMAN, id="tester")


class PathArgs(BaseModel):
    path: str


class WriteArgs(BaseModel):
    path: str
    text: str = ""


@dataclass
class Files:
    """An in-memory file system the example tools act on, so tests can see effects."""

    content: dict[str, str] = field(default_factory=dict)
    writes: list[str] = field(default_factory=list)


def file_tools(files: Files) -> list[Any]:
    @tool(effect=EffectClass.READ, capabilities=["files.read"], resource=lambda a: a.path)
    def read_file(args: PathArgs, ctx: ToolContext) -> str:
        """Read a file."""
        if args.path not in files.content:
            raise FileNotFoundError(args.path)
        return files.content[args.path]

    @tool(effect=EffectClass.WRITE, capabilities=["files.write"], resource=lambda a: a.path)
    def write_file(args: WriteArgs, ctx: ToolContext) -> str:
        """Write a file."""
        files.content[args.path] = args.text
        files.writes.append(args.path)
        return "ok"

    return [read_file, write_file]


def agent(**overrides: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "tester",
        "instructions": "Do the task.",
        "tools": ["read_file", "write_file"],
        "capabilities": ["files.read:docs/**", "files.write:out/**"],
        "budget": BudgetLimits(),
    }
    base.update(overrides)
    return AgentSpec.model_validate(base)


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


@dataclass
class Harness:
    legion: Legion
    provider: ScriptedProvider
    store: EventStore
    files: Files
    sleeps: Sleeps
    artifacts: MemoryArtifactStore

    async def run(self, spec: AgentSpec | None = None, objective: str = "do it", **kw: Any) -> Any:
        return await self.legion.run(spec or agent(), objective, principal=PRINCIPAL, **kw)

    async def events(self, run_id: str) -> list[Event]:
        return await self.store.read(run_id)

    async def types(self, run_id: str) -> list[EventType]:
        return [e.type for e in await self.events(run_id)]

    async def payloads(self, run_id: str, kind: EventType) -> list[dict[str, Any]]:
        return [e.payload for e in await self.events(run_id) if e.type is kind]


def build(
    steps: Sequence[Step],
    *,
    extra_tools: Sequence[Any] = (),
    grantable: Sequence[str] = ("files.read:**", "files.write:**"),
    rules: list[Rule] | None = None,
    policy: PolicyDecisionPoint | None = None,
    identity: IdentityPort | None = None,
    store: EventStore | None = None,
    pricing: Pricing | None = None,
    credentials: CredentialResolver | None = None,
    credential_bindings: dict[str, SecretRef] | None = None,
    files: Files | None = None,
    retry: RetryPolicy | None = None,
) -> Harness:
    files = files or Files({"docs/a.md": "alpha", "secret/b.md": "beta"})
    provider = ScriptedProvider(steps)
    registry = ToolRegistry([*file_tools(files), *extra_tools])
    sleeps = Sleeps()
    artifacts = MemoryArtifactStore()
    store = store or MemoryEventStore()
    legion = Legion(
        resolver=ModelResolver(
            [
                ModelBinding(
                    profile="general/default", provider="script", model="scripted", pricing=pricing
                )
            ],
            {"script": provider},
        ),
        tools=registry,
        store=store,
        policy=policy or RuleTablePolicy(rules if rules is not None else default_rules()),
        grantable=[Capability.parse(c) for c in grantable],
        identity=identity,
        credentials=credentials,
        credential_bindings=credential_bindings,
        artifacts=artifacts,
        retry=retry or RetryPolicy(max_attempts=3, base_delay=1.0, max_delay=8.0),
        sleep=sleeps,
    )
    return Harness(legion, provider, store, files, sleeps, artifacts)
