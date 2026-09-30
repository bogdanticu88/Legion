# Legion with NIA as its identity and credential authority, against a real nia-api you point it
# at. It starts nia-api on 127.0.0.1 with throwaway operator tokens, registers an agent, grants it
# repo.read on repo-A, and walks through what Legion does:
#
#   1. a read of repo-A: NIA issues a credential for exactly that call, Legion checks it (bound),
#      the tool gets it
#   2. a write NIA never granted: NIA refuses, nothing runs
#   3. a read of repo-B, which NIA never granted: refused
#   4. the agent is killed in NIA: the next run is refused, the credential it had is revoked
#   5. restored, and granted again: a fresh credential works; the old one stays revoked
#   6. revoked just before dispatch: refused, not reissued
#
# Nothing else in Legion needs NIA; this is the optional adapter at work.
#
#   (cd ../nia && go build -o /tmp/nia-api ./cmd/api)
#   LEGION_DEMO_NIA_BIN=/tmp/nia-api uv run python examples/nia_credential_demo.py

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from legion.access.secrets import EnvResolver
from legion.adapters.nia import NiaIdentityConfig, NiaIdentityPort
from legion.adapters.nia_credentials import NiaCredentialAuthority, NiaCredentialConfig
from legion.authority.policy import RuleTablePolicy
from legion.domain.action import EffectClass
from legion.domain.agent import AgentSpec
from legion.domain.capability import Capability
from legion.domain.principal import Principal, PrincipalKind
from legion.events.store import MemoryEventStore
from legion.events.types import EventType
from legion.kernel.credentials import CredentialMapping
from legion.kernel.runtime import Legion
from legion.models.resolver import ModelBinding, ModelResolver
from legion.models.scripted import ScriptedProvider, call, reply
from legion.ports.credentials import Assurance
from legion.tools.base import ToolContext
from legion.tools.native import tool
from legion.tools.registry import ToolRegistry

REF = "agent:demo"
ADMIN, ISSUER, VIEWER = (secrets.token_hex(16) for _ in range(3))
USED: list[str] = []


class RepoArgs(BaseModel):
    repo: str


@tool(
    effect=EffectClass.READ,
    capabilities=["repo.read"],
    resource=lambda a: a.repo,
    credentials=["repo"],
)
def read_repo(args: RepoArgs, ctx: ToolContext) -> str:
    """Read a repository."""
    USED.append(ctx.credentials["repo"].reveal())
    return f"README of {args.repo}"


@tool(
    effect=EffectClass.WRITE,
    capabilities=["repo.write"],
    resource=lambda a: a.repo,
    credentials=["repo"],
)
def write_repo(args: RepoArgs, ctx: ToolContext) -> str:
    """Write to a repository."""
    USED.append(ctx.credentials["repo"].reveal())
    return "written"


def start_nia(binary: str, workdir: Path) -> tuple[str, subprocess.Popen[bytes]]:
    tokens = workdir / "operators.json"
    tokens.write_text(
        json.dumps(
            [
                {"token": ADMIN, "name": "demo-admin", "roles": ["admin"]},
                {"token": ISSUER, "name": "legion", "roles": ["issuer"]},
                {"token": VIEWER, "name": "legion-viewer", "roles": ["viewer"]},
            ]
        )
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "NIA_API_ADDR": f"127.0.0.1:{port}",
        "NIA_OPERATOR_TOKENS_PATH": str(tokens),
    }
    # the binary is the one the person running the demo named
    proc = subprocess.Popen(  # noqa: S603
        [binary], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/healthz", timeout=0.2).status_code == 200:
                return url, proc
        except httpx.TransportError:
            time.sleep(0.05)
    proc.kill()
    raise SystemExit("nia-api didn't start")


def admin(url: str, method: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.request(
        method, url + path, json=body, headers={"Authorization": f"Bearer {ADMIN}"}
    )


def nia_status(url: str, ref: str) -> str:
    r = httpx.get(
        f"{url}/agents/{REF}/scoped-credentials/{ref}",
        headers={"Authorization": f"Bearer {VIEWER}"},
    )
    return str(r.json()["status"])


def grant_read(url: str) -> None:
    body = {
        "grants": [{"kind": "tool", "object": "repo.read"}, {"kind": "data", "object": "repo-A"}]
    }
    admin(url, "POST", f"/agents/{REF}/grants", body).raise_for_status()


async def run(url: str, steps: list[Any], faults: Any = None) -> tuple[str, list[dict[str, Any]]]:
    env = EnvResolver({"ISSUER": ISSUER, "VIEWER": VIEWER})
    authority = NiaCredentialAuthority(
        "nia",
        NiaCredentialConfig(
            provider="nia",
            endpoint=url,
            credential="env:ISSUER",
            trusted=True,
            audience="nia-gateway",
        ),
        {"demo": REF},
        env,
    )
    identity = NiaIdentityPort(
        NiaIdentityConfig(
            provider="nia", endpoint=url, credential="env:VIEWER", agents={"demo": REF}
        ),
        env,
    )
    store = MemoryEventStore()
    legion = Legion(
        resolver=ModelResolver(
            [ModelBinding(profile="general/default", provider="script", model="scripted")],
            {"script": ScriptedProvider(steps)},
        ),
        tools=ToolRegistry([read_repo, write_repo]),
        store=store,
        policy=RuleTablePolicy([]),
        grantable=[Capability.parse("repo.*")],
        identity=identity,
        credential_mappings={
            "repo": CredentialMapping(
                authority="nia",
                provider="nia-gateway",
                permissions={"repo.read": ("repo.read",), "repo.write": ("repo.write",)},
                max_lifetime_s=120,
            )
        },
        credential_authorities={"nia": authority},
        trusted_authorities=["nia"],
        credential_minimum=Assurance.BOUND,
        **({"faults": faults} if faults else {}),
    )
    spec = AgentSpec(
        name="demo",
        instructions="do it",
        tools=("read_repo", "write_repo"),
        capabilities=("repo.read:repo-*", "repo.write:repo-A"),
    )
    try:
        outcome = await legion.run(
            spec, "go", principal=Principal(kind=PrincipalKind.HUMAN, id="ana")
        )
    except Exception as exc:
        await authority.aclose()
        await identity.aclose()
        return f"not started ({type(exc).__name__})", []
    await authority.aclose()
    await identity.aclose()
    creds = [
        {"decision": "used" if e.type is EventType.CREDENTIAL_RESOLVED else "refused", **e.payload}
        for e in await store.read(outcome.run_id)
        if e.type in (EventType.CREDENTIAL_RESOLVED, EventType.CREDENTIAL_REFUSED)
    ]
    return (
        f"{outcome.status.value}{' (' + outcome.error_code + ')' if outcome.error_code else ''}",
        creds,
    )


def show(title: str, status: str, creds: list[dict[str, Any]]) -> None:
    print(f"\n{title}\n  run: {status}")
    for c in creds:
        why = "; ".join(c["problems"])
        print(
            f"  {c['decision']}: {c['assurance'] or 'rejected'} (needs {c['required']}), "
            f"{', '.join(c['permissions']) or '-'} on {c['resource'] or '-'}, "
            f"call {c['legion_call_id']}, "
            f"NIA principal {c['evidenced_external_principal'] or '-'}, "
            f"ref {c['credential_ref'] or '-'}" + (f"\n    why: {why}" if why else "")
        )


async def main() -> None:
    binary = os.environ.get("LEGION_DEMO_NIA_BIN")
    if not binary:
        sys.exit("set LEGION_DEMO_NIA_BIN to a built nia-api (go build ./cmd/api in the NIA repo)")
    with tempfile.TemporaryDirectory() as tmp:
        url, proc = start_nia(binary, Path(tmp))
        try:
            admin(url, "POST", "/agents", {"ref": REF, "owner": "demo"}).raise_for_status()
            grant_read(url)
            read_a = [call("read_repo", {"repo": "repo-A"}), reply("done")]

            status, creds = await run(url, read_a)
            show("1. read repo-A", status, creds)
            first = creds[0]["credential_ref"]
            show(
                "2. write repo-A (NIA never granted repo.write)",
                *await run(url, [call("write_repo", {"repo": "repo-A"}), reply("done")]),
            )
            show(
                "3. read repo-B (NIA never granted repo-B)",
                *await run(url, [call("read_repo", {"repo": "repo-B"}), reply("done")]),
            )
            admin(url, "POST", "/policy/kill", {"agent_ref": REF, "incident": "demo"})
            show("4. killed in NIA", *await run(url, read_a))
            print(f"  NIA says the credential from 1 is now {nia_status(url, first)}")
            admin(url, "POST", "/policy/restore", {"agent_ref": REF})
            grant_read(url)
            show("5. restored and granted again", *await run(url, read_a))
            print(f"  and the credential from 1 is still {nia_status(url, first)}")

            def revoke_after_issue(name: str) -> None:
                if name == "credential:obtained":
                    audit = admin(url, "GET", f"/agents/{REF}/audit").json()
                    ref = [a for a in audit if a["Action"] == "scoped_credential.issued"][-1]
                    cred = ref["Detail"].split("ref=")[1].split()[0]
                    admin(url, "POST", f"/agents/{REF}/scoped-credentials/{cred}/revoke", {})

            show(
                "6. revoked in NIA just before dispatch",
                *await run(url, read_a, revoke_after_issue),
            )
            print(f"\nthe tool received {len(USED)} credential(s); none of them is printed above")
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    asyncio.run(main())
