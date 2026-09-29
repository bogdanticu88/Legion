# Eight cases showing what Legion does with credentials. Everything is local and deterministic:
# a scripted model, an in-memory log, and a small credential authority defined below that can
# be told to hand out the wrong thing. No NIA, network or API key.
#
#   uv run python examples/credential_demo.py

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from legion.access.secrets import EnvResolver, Secret, SecretRef
from legion.authority.policy import RuleTablePolicy
from legion.domain.action import EffectClass
from legion.domain.agent import AgentSpec, ModelRequirement
from legion.domain.capability import Capability
from legion.domain.errors import ToolRetryable
from legion.domain.grant import DelegationLimits
from legion.domain.principal import Principal, PrincipalKind
from legion.events.store import MemoryEventStore
from legion.events.types import EventType
from legion.kernel.credentials import CredentialMapping
from legion.kernel.runtime import Legion
from legion.models.resolver import ModelBinding, ModelResolver
from legion.models.scripted import ScriptedProvider, call, reply
from legion.ports.credentials import (
    Assurance,
    CredentialRequest,
    CredentialStatus,
    IssuedCredential,
)
from legion.tools.base import ToolContext
from legion.tools.native import tool
from legion.tools.registry import ToolRegistry

ANA = Principal(kind=PrincipalKind.HUMAN, id="ana")


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


@dataclass
class DemoAuthority:
    """Issues a credential for exactly what was asked, unless told to misbehave."""

    clock: Clock
    name: str = "demo"
    grant_more: dict[str, Any] = field(default_factory=dict)
    fail: bool = False
    revoke_at_once: bool = False
    replay_first: bool = False
    issued: list[CredentialRequest] = field(default_factory=list)
    revoked: set[str] = field(default_factory=set)

    async def issue(self, request: CredentialRequest) -> IssuedCredential:
        if self.fail:
            raise ConnectionError("authority unavailable")
        self.issued.append(request)
        n = len(self.issued)
        basis = self.issued[0] if self.replay_first else request
        now = self.clock()
        evidence = {
            "authority": self.name,
            "credential_ref": f"demo-{n}",
            "provider": basis.provider,
            "principal": basis.principal,
            "subject": basis.subject,
            "permissions": list(basis.permissions),
            "resource": basis.resource,
            "issued_at": now,
            "expires_at": now + timedelta(seconds=60),
            "action_hash": basis.action_hash,
            "call_id": basis.call_id,
            "grant_fingerprint": basis.grant_fingerprint,
        }
        evidence.update(self.grant_more)
        if self.revoke_at_once:
            self.revoked.add(f"demo-{n}")
        return IssuedCredential(
            Secret(f"demo-token-{n}-0f1e2d3c"), evidence, credential_ref=f"demo-{n}"
        )

    async def status(self, credential_ref: str) -> CredentialStatus:
        if credential_ref in self.revoked:
            return CredentialStatus.REVOKED
        return CredentialStatus.ACTIVE

    async def revoke(self, credential_ref: str) -> None:
        self.revoked.add(credential_ref)


class RepoArgs(BaseModel):
    repo: str
    attempt: str = ""


def tools(clock: Clock, flaky: bool = False) -> list[Any]:
    tries: list[int] = []

    @tool(
        effect=EffectClass.READ,
        capabilities=["repo.read"],
        resource=lambda a: a.repo,
        credentials=["github"],
    )
    def read_repo(args: RepoArgs, ctx: ToolContext) -> str:
        """Read a repository."""
        tries.append(1)
        if flaky and len(tries) == 1:
            clock.t += timedelta(seconds=120)  # the credential expires before the retry
            raise ToolRetryable("upstream hiccup")
        return f"README of {args.repo}"

    return [read_repo]


MAPPING = CredentialMapping(
    authority="demo",
    provider="github",
    permissions={"repo.read": ("contents:read",)},
    max_lifetime_s=60,
    minimum=Assurance.BOUND,
)


def spec(name: str = "reader", profile: str = "demo/main", **kw: Any) -> AgentSpec:
    return AgentSpec(
        name=name,
        instructions="Read what you're asked to.",
        model=ModelRequirement(profile=profile),
        tools=kw.pop("tools", ("read_repo",)),
        capabilities=kw.pop("capabilities", ("repo.read:repo-A",)),
        **kw,
    )


async def run(
    steps: list[Any],
    authority: DemoAuthority | None,
    *,
    agent: AgentSpec | None = None,
    mapping: CredentialMapping | None = MAPPING,
    static: bool = False,
    child_steps: list[Any] | None = None,
    agents: dict[str, AgentSpec] | None = None,
    flaky: bool = False,
    clock: Clock | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    clock = clock or (authority.clock if authority else Clock())
    bindings = [ModelBinding(profile="demo/main", provider="main", model="scripted")]
    providers: dict[str, Any] = {"main": ScriptedProvider(steps)}
    if child_steps is not None:
        bindings.append(ModelBinding(profile="demo/child", provider="child", model="scripted"))
        providers["child"] = ScriptedProvider(child_steps)
    store = MemoryEventStore()
    legion = Legion(
        resolver=ModelResolver(bindings, providers),
        tools=ToolRegistry(tools(clock, flaky)),
        store=store,
        policy=RuleTablePolicy([]),
        grantable=[Capability.parse("repo.read:**"), Capability.parse("agent.delegate:**")],
        credentials=EnvResolver({"GITHUB_TOKEN": "static-demo-token-123"}),
        credential_bindings={"github": SecretRef.parse("env:GITHUB_TOKEN")} if static else None,
        credential_mappings={"github": mapping} if mapping and not static else None,
        credential_authorities={"demo": authority} if authority else None,
        trusted_authorities=["demo"],
        agents=agents,
        now=clock,
    )
    outcome = await legion.run(agent or spec(), "read the repository", principal=ANA)
    events = await store.read(outcome.run_id)
    reads = {
        (e.task_id, e.payload["call_id"])
        for e in events
        if e.type is EventType.ACTION_PROPOSED and e.payload["tool"] == "read_repo"
    }
    ran = sum(
        1
        for e in events
        if e.type is EventType.TOOL_COMPLETED and (e.task_id, e.payload["call_id"]) in reads
    )
    records = [
        {"decision": "used" if e.type is EventType.CREDENTIAL_RESOLVED else "refused", **e.payload}
        for e in events
        if e.type in (EventType.CREDENTIAL_RESOLVED, EventType.CREDENTIAL_REFUSED)
    ]
    return ("executed" if ran else "not executed") + f" (read_repo ran {ran} time(s))", records


def show(number: int, title: str, expected: str, result: tuple[str, list[dict[str, Any]]]) -> None:
    outcome, records = result
    print(f"CASE {number}  {title}")
    print(f"  expected: {expected}")
    print(f"  got:      {outcome}")
    for r in records:
        if r["authority"] == "static":
            scope = "static secret, scope unknown"
        else:
            evidenced = (
                f"evidence {', '.join(r['permissions'])} on {r['resource'] or 'any resource'}"
                if r["permissions"]
                else "no evidence"
            )
            scope = (
                f"asked {', '.join(r['requested_permissions'])} on {r['requested_resource']}, "
                f"{evidenced}"
            )
        line = (
            f"  credential {r['decision']}: {r['subject']} {r['call_id']} "
            f"needs {r['required']}, got {r['assurance'] or 'rejected'}; {scope}"
        )
        print(line)
        if r["problems"]:
            print(f"    why: {'; '.join(r['problems'])}")
    print()


async def main() -> None:
    read_a = call("read_repo", {"repo": "repo-A"})

    show(
        1,
        "matched authority",
        "executes, assurance bound",
        await run([read_a, reply("ok")], DemoAuthority(Clock())),
    )

    show(
        2,
        "credential amplification",
        "refused before the tool runs",
        await run(
            [read_a, reply("ok")],
            DemoAuthority(Clock(), grant_more={"permissions": ["admin"], "resource": None}),
        ),
    )

    child = spec("child-reader", "demo/child")
    parent = spec(
        "parent",
        tools=("read_repo", "delegate"),
        capabilities=("repo.read:repo-A", "agent.delegate:**"),
        delegation=DelegationLimits(max_depth=1, max_children=1),
    )
    show(
        3,
        "child amplification",
        "the child's call is refused",
        await run(
            [call("delegate", {"agent": "child-reader", "objective": "read"}), reply("ok")],
            DemoAuthority(Clock(), grant_more={"permissions": ["contents:write"]}),
            agent=parent,
            child_steps=[read_a, reply("read")],
            agents={"child-reader": child},
        ),
    )

    show(
        4,
        "authority unavailable, verified required",
        "fails closed",
        await run(
            [read_a, reply("ok")],
            DemoAuthority(Clock(), fail=True),
            mapping=MAPPING.model_copy(update={"minimum": Assurance.VERIFIED}),
        ),
    )

    show(
        5,
        "static credential, unverified allowed",
        "executes, assurance unverified",
        await run([read_a, reply("ok")], None, mapping=None, static=True),
    )

    show(
        6,
        "call substitution",
        "the second call is refused",
        await run(
            [read_a, call("read_repo", {"repo": "repo-A"}), reply("ok")],
            DemoAuthority(Clock(), replay_first=True),
        ),
    )

    show(
        7,
        "revoked before dispatch",
        "refused",
        await run([read_a, reply("ok")], DemoAuthority(Clock(), revoke_at_once=True)),
    )

    clock = Clock()
    show(
        8,
        "expiry before a safe read retry",
        "a fresh credential, same authority, succeeds",
        await run([read_a, reply("ok")], DemoAuthority(clock), flaky=True, clock=clock),
    )


if __name__ == "__main__":
    asyncio.run(main())
