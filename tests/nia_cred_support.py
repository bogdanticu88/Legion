# Shared setup for the NIA credential authority tests: a Legion harness whose `github`
# credential comes from NIA's scoped credentials (through the adapter, over HTTP, to the stand-in
# in nia_cred_lab), optionally with the NIA identity port on the same stand-in.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from legion.access.secrets import EnvResolver
from legion.adapters.nia import NiaIdentityConfig, NiaIdentityPort
from legion.adapters.nia_credentials import NiaCredentialAuthority, NiaCredentialConfig
from legion.domain.agent import AgentSpec
from legion.kernel.credentials import CredentialMapping
from legion.ports.credentials import Assurance
from tests.credential_lab import repo_tools
from tests.nia_cred_lab import ISSUER, VIEWER, FakeNiaCredentials
from tests.support import Harness, agent, build

AGENTS = {
    "tester": "agent:tester",
    "helper": "agent:helper",
    "sibling": "agent:sibling",
}
ENV = EnvResolver({"NIA_ISSUER": ISSUER, "NIA_VIEWER": VIEWER})
MAPPING = CredentialMapping(
    authority="nia",
    provider="nia-gateway",
    permissions={"repo.read": ("repo.read",), "repo.issue.create": ("repo.write",)},
    max_lifetime_s=120,
)


@dataclass
class Clock:
    now: datetime = field(default_factory=lambda: datetime(2026, 9, 30, 10, 0, tzinfo=UTC))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def fake(clock: Clock | None = None) -> FakeNiaCredentials:
    # without a clock, both sides run on real time
    nia = FakeNiaCredentials(clock=clock or (lambda: datetime.now(UTC)))
    nia.register("agent:tester", tools=("repo.read", "repo.write"), data=("repo-A",))
    nia.register("agent:helper", tools=("repo.read",), data=("repo-A",))
    nia.register("agent:sibling", tools=("repo.read",), data=("repo-A",))
    return nia


def authority(url: str, agents: dict[str, str] | None = None, **cfg: Any) -> NiaCredentialAuthority:
    settings: dict[str, Any] = {
        "provider": "nia",
        "endpoint": url,
        "credential": "env:NIA_ISSUER",
        "trusted": True,
        "audience": "nia-gateway",
        "timeout_s": 1.0,
        **cfg,
    }
    return NiaCredentialAuthority(
        "nia", NiaCredentialConfig.model_validate(settings), agents or AGENTS, ENV
    )


def identity(url: str, agents: dict[str, str] | None = None, **cfg: Any) -> NiaIdentityPort:
    settings: dict[str, Any] = {
        "provider": "nia",
        "endpoint": url,
        "credential": "env:NIA_VIEWER",
        "timeout_s": 1.0,
        "agents": agents or AGENTS,
        **cfg,
    }
    return NiaIdentityPort(NiaIdentityConfig.model_validate(settings), ENV)


@dataclass
class NiaRun:
    h: Harness
    authority: NiaCredentialAuthority
    identity: NiaIdentityPort | None
    seen: list[tuple[str, str]]

    async def close(self) -> None:
        await self.authority.aclose()
        if self.identity is not None:
            await self.identity.aclose()


def setup(
    url: str,
    steps: list[Any],
    *,
    with_identity: bool = True,
    minimum: Assurance = Assurance.BOUND,
    mapping: CredentialMapping = MAPPING,
    clock: Clock | None = None,
    auth: NiaCredentialAuthority | None = None,
    tools: list[Any] | None = None,
    echo: bool = False,
    **kw: Any,
) -> NiaRun:
    seen: list[tuple[str, str]] = []
    auth = auth or authority(url)
    ident = identity(url) if with_identity else None
    options = {
        "credential_mappings": {"github": mapping},
        "credential_authorities": {"nia": auth},
        "trusted_authorities": ("nia",),
        "credential_minimum": minimum,
        "credential_timeout_s": 5.0,
        **kw.pop("options", {}),
    }
    h = build(
        steps,
        extra_tools=tools if tools is not None else repo_tools(seen, echo=echo),
        grantable=kw.pop("grantable", ("repo.*", "files.read:**", "agent.delegate:**")),
        identity=ident,
        now=clock,
        options=options,
        **kw,
    )
    return NiaRun(h, auth, ident, seen)


def spec(*caps: str, **kw: Any) -> AgentSpec:
    caps = caps or ("repo.read:repo-A",)
    names = {"repo.read": "read_repo", "repo.issue.create": "create_issue"}
    tools = [tool for cap, tool in names.items() if any(c.startswith(cap + ":") for c in caps)]
    return agent(tools=kw.pop("tools", tools), capabilities=list(caps), **kw)
