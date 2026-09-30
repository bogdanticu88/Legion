# Legion's NIA identity port and NIA credential authority against a real NIA control plane
# (cmd/api at 40891e3 or later), started from a binary as its own process and reached only
# through its HTTP API. Skipped unless LEGION_TEST_NIA_BIN points at a built nia-api.
#
# The real stack variant, with the tool presenting its credential to a real nia-gateway, is at
# the end and needs LEGION_TEST_NIA_STACK.

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from legion.access.secrets import EnvResolver
from legion.adapters.nia import NiaIdentityConfig, NiaIdentityPort
from legion.adapters.nia_credentials import NiaCredentialAuthority, NiaCredentialConfig
from legion.domain.action import EffectClass
from legion.domain.errors import IdentityUnavailable
from legion.domain.states import RunStatus
from legion.events.types import EventType
from legion.kernel.credentials import CredentialMapping
from legion.models.scripted import call, reply
from legion.ports.credentials import Assurance
from legion.tools.base import ToolContext
from legion.tools.native import tool
from tests.support import agent, build

pytestmark = pytest.mark.integration
E = EventType

ADMIN = "it-admin-5b3-2c7e9a1f0d"
ISSUER = "it-issuer-5b3-8b4d6f2a1c"
VIEWER = "it-viewer-5b3-3e1a7c9b5d"
REF = "agent:tester"
MAPPING = CredentialMapping(
    authority="nia",
    provider="nia-gateway",
    permissions={"repo.read": ("repo.read",), "repo.issue.create": ("repo.write",)},
    max_lifetime_s=60,
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Nia:
    url: str
    proc: subprocess.Popen[bytes]

    def admin(self, method: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return httpx.request(
            method, self.url + path, json=body, headers={"Authorization": f"Bearer {ADMIN}"}
        )

    def status(self, ref: str) -> str:
        r = httpx.get(
            f"{self.url}/agents/{REF}/scoped-credentials/{ref}",
            headers={"Authorization": f"Bearer {VIEWER}"},
        )
        return str(r.json()["status"])


@pytest.fixture
def nia(tmp_path: Path) -> Iterator[Nia]:
    binary = os.environ.get("LEGION_TEST_NIA_BIN")
    if not binary:
        pytest.skip("set LEGION_TEST_NIA_BIN to a built nia-api")
    tokens = tmp_path / "operators.json"
    tokens.write_text(
        json.dumps(
            [
                {"token": ADMIN, "name": "it-admin", "roles": ["admin"]},
                {"token": ISSUER, "name": "legion-issuer", "roles": ["issuer"]},
                {"token": VIEWER, "name": "legion-viewer", "roles": ["viewer"]},
            ]
        )
    )
    port = free_port()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "NIA_API_ADDR": f"127.0.0.1:{port}",
        "NIA_OPERATOR_TOKENS_PATH": str(tokens),
    }
    proc = subprocess.Popen([binary], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/healthz", timeout=0.2).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("nia-api didn't start")
    n = Nia(url, proc)
    assert n.admin("POST", "/agents", {"ref": REF, "owner": "it"}).status_code == 201
    grant(n)
    yield n
    proc.terminate()
    _, stderr = proc.communicate(timeout=5)
    # NIA's own logs never saw a Legion token either
    for planted in (ISSUER, VIEWER):
        assert planted.encode() not in stderr


def grant(n: Nia) -> None:
    r = n.admin(
        "POST",
        f"/agents/{REF}/grants",
        {"grants": [{"kind": "tool", "object": "repo.read"}, {"kind": "data", "object": "repo-A"}]},
    )
    assert r.status_code == 200


class RepoArgs(BaseModel):
    repo: str
    title: str = ""


SEEN: list[str] = []
HOOK: dict[str, Any] = {}


@tool(
    effect=EffectClass.READ,
    capabilities=["repo.read"],
    resource=lambda a: a.repo,
    credentials=["github"],
)
def read_repo(args: RepoArgs, ctx: ToolContext) -> str:
    """Read a repository."""
    SEEN.append(ctx.credentials["github"].reveal())
    return f"README of {args.repo}"


@tool(
    effect=EffectClass.WRITE,
    capabilities=["repo.issue.create"],
    resource=lambda a: a.repo,
    credentials=["github"],
)
def create_issue(args: RepoArgs, ctx: ToolContext) -> str:
    """Open an issue."""
    SEEN.append(ctx.credentials["github"].reveal())
    return "opened"


def legion(
    n: Nia,
    steps: list[Any],
    *,
    issuer: str = ISSUER,
    viewer: str = VIEWER,
    faults: Any = None,
    authority: Any = None,
) -> tuple[Any, NiaCredentialAuthority, NiaIdentityPort]:
    env = EnvResolver({"I": issuer, "V": viewer})
    auth = NiaCredentialAuthority(
        "nia",
        NiaCredentialConfig(
            provider="nia", endpoint=n.url, credential="env:I", trusted=True, audience="nia-gateway"
        ),
        {"tester": REF},
        env,
    )
    ident = NiaIdentityPort(
        NiaIdentityConfig(
            provider="nia", endpoint=n.url, credential="env:V", agents={"tester": REF}
        ),
        env,
    )
    h = build(
        steps,
        extra_tools=[read_repo, create_issue],
        grantable=("repo.*",),
        identity=ident,
        faults=faults,
        options={
            "credential_mappings": {"github": MAPPING},
            "credential_authorities": {"nia": authority(auth) if authority else auth},
            "trusted_authorities": ("nia",),
            "credential_minimum": Assurance.BOUND,
            "credential_timeout_s": 10.0,
        },
    )
    return h, auth, ident


SPEC = agent(
    tools=["read_repo", "create_issue"],
    capabilities=["repo.read:repo-*", "repo.issue.create:repo-A"],
)


async def run(n: Nia, steps: list[Any], **kw: Any) -> tuple[Any, Any, list[Any]]:
    SEEN.clear()
    h, auth, ident = legion(n, steps, **kw)
    try:
        out = await h.run(SPEC)
    finally:
        await auth.aclose()
        await ident.aclose()
    return h, out, await h.events(out.run_id)


def text(events: list[Any]) -> str:
    return json.dumps([e.model_dump(mode="json") for e in events])


async def test_scoped_credential_end_to_end(nia: Nia) -> None:
    _, out, events = await run(nia, [call("read_repo", {"repo": "repo-A"}), reply("done")])
    assert out.status is RunStatus.COMPLETED
    [used] = [e.payload for e in events if e.type is E.CREDENTIAL_RESOLVED]
    [proposed] = [e.payload for e in events if e.type is E.ACTION_PROPOSED]
    assert used["assurance"] == "bound" and used["required"] == "bound"
    assert used["legion_call_id"] == proposed["legion_call_id"]
    assert used["external_principal"] == used["evidenced_external_principal"] == REF
    assert used["resource"] == "repo-A" and used["permissions"] == ["repo.read"]
    [token] = SEEN
    ref, _, secret = token.partition(".")
    assert used["credential_ref"] == ref and nia.status(ref) == "active"
    assert secret not in text(events)
    # NIA's own record: what it issued, to whom, for which call
    audit = nia.admin("GET", f"/agents/{REF}/audit").json()
    [issued] = [a for a in audit if a["Action"] == "scoped_credential.issued"]
    assert proposed["legion_call_id"] in issued["Detail"] and secret not in json.dumps(audit)


async def test_permission_wider_than_nia_grants(nia: Nia) -> None:
    _, _, events = await run(nia, [call("create_issue", {"repo": "repo-A"}), reply("done")])
    assert [e for e in events if e.type is E.CREDENTIAL_REFUSED] and SEEN == []


async def test_resource_nia_has_not_granted(nia: Nia) -> None:
    _, _, events = await run(nia, [call("read_repo", {"repo": "repo-B"}), reply("done")])
    assert [e for e in events if e.type is E.CREDENTIAL_REFUSED] and SEEN == []


async def test_killed_before_issuance(nia: Nia) -> None:
    nia.admin("POST", "/policy/kill", {"agent_ref": REF, "incident": "it"})
    _, out, events = await run(nia, [call("read_repo", {"repo": "repo-A"}), reply("done")])
    assert out.error_code == "killed" and SEEN == []
    assert not [e for e in events if e.type is E.CREDENTIAL_RESOLVED]


async def test_killed_after_issuance_then_restored(nia: Nia) -> None:
    def kill_after_issue(name: str) -> None:
        if name == "credential:obtained":
            nia.admin("POST", "/policy/kill", {"agent_ref": REF, "incident": "it"})

    _, out, events = await run(
        nia, [call("read_repo", {"repo": "repo-A"}), reply("done")], faults=kill_after_issue
    )
    assert out.status is RunStatus.FAILED and SEEN == []
    issued = [
        e.payload["credential_ref"]
        for e in events
        if e.type in (E.CREDENTIAL_RESOLVED, E.CREDENTIAL_REFUSED) and e.payload["credential_ref"]
    ]
    assert issued and all(nia.status(r) == "revoked" for r in issued)
    # restored: the old credential stays revoked; a new run works once grants are written again
    nia.admin("POST", "/policy/restore", {"agent_ref": REF})
    grant(nia)
    assert all(nia.status(r) == "revoked" for r in issued)
    _, again, _ = await run(nia, [call("read_repo", {"repo": "repo-A"}), reply("done")])
    assert again.status is RunStatus.COMPLETED and len(SEEN) == 1


async def test_expired_is_replaced_for_the_same_call(nia: Nia) -> None:
    short = MAPPING.model_copy(update={"max_lifetime_s": 1})
    waited: list[bool] = []

    def wait_once(name: str) -> None:
        if name == "credential:obtained" and not waited:
            waited.append(True)
            time.sleep(1.3)

    h, auth, ident = legion(
        nia, [call("read_repo", {"repo": "repo-A"}), reply("done")], faults=wait_once
    )
    h.legion.broker.mapped["github"] = short
    SEEN.clear()
    out = await h.run(SPEC)
    await auth.aclose()
    await ident.aclose()
    events = await h.events(out.run_id)
    used = [e.payload for e in events if e.type is E.CREDENTIAL_RESOLVED]
    assert out.status is RunStatus.COMPLETED and len(used) == 2 and len(SEEN) == 1
    assert used[0]["legion_call_id"] == used[1]["legion_call_id"]
    assert used[0]["credential_ref"] != used[1]["credential_ref"]


async def test_revoked_before_dispatch_is_not_reissued(nia: Nia) -> None:
    def revoke(name: str) -> None:
        if name == "credential:obtained":
            [ref] = [r for r in _refs(nia)]
            nia.admin("POST", f"/agents/{REF}/scoped-credentials/{ref}/revoke", {})

    _, _, events = await run(
        nia, [call("read_repo", {"repo": "repo-A"}), reply("done")], faults=revoke
    )
    assert SEEN == []
    assert len([e for e in events if e.type is E.CREDENTIAL_RESOLVED]) == 1
    [refused] = [e.payload for e in events if e.type is E.CREDENTIAL_REFUSED]
    assert "no longer active at the authority" in refused["problems"]


def _refs(n: Nia) -> list[str]:
    audit = n.admin("GET", f"/agents/{REF}/audit").json()
    refs = []
    for a in audit:
        if a["Action"] == "scoped_credential.issued":
            refs.append(a["Detail"].split("ref=")[1].split()[0])
    return refs[-1:]


class Replayer:
    """Hands back the first credential NIA issued for every later request: a real NIA
    credential, presented for a call it wasn't issued for."""

    def __init__(self, inner: NiaCredentialAuthority) -> None:
        self.inner = inner
        self.name = inner.name
        self.first: Any = None

    async def issue(self, request: Any) -> Any:
        got = await self.inner.issue(request)
        if self.first is None:
            self.first = got
            return got
        return self.first

    async def status(self, ref: str) -> Any:
        return await self.inner.status(ref)

    async def revoke(self, ref: str) -> None:
        await self.inner.revoke(ref)


async def test_real_credential_for_another_call_is_refused(nia: Nia) -> None:
    from legion.models.scripted import calls

    two = calls([("read_repo", {"repo": "repo-A"}), ("read_repo", {"repo": "repo-A"})])
    _, _, events = await run(nia, [two, reply("done")], authority=Replayer)
    assert len(SEEN) == 1
    [refused] = [e.payload for e in events if e.type is E.CREDENTIAL_REFUSED]
    joined = " ".join(refused["problems"])
    assert "already used in this run" in joined or "different call" in joined


async def test_nia_down(nia: Nia) -> None:
    h, auth, ident = legion(nia, [call("read_repo", {"repo": "repo-A"}), reply("done")])
    nia.proc.terminate()
    nia.proc.wait(timeout=5)
    SEEN.clear()
    with pytest.raises(IdentityUnavailable):
        await h.run(SPEC)
    await auth.aclose()
    await ident.aclose()
    assert SEEN == []


async def test_issuer_token_refused(nia: Nia) -> None:
    _, _, events = await run(
        nia, [call("read_repo", {"repo": "repo-A"}), reply("done")], issuer="not-a-token"
    )
    assert [e for e in events if e.type is E.CREDENTIAL_REFUSED] and SEEN == []
    assert "not-a-token" not in text(events)


async def test_viewer_token_refused(nia: Nia) -> None:
    SEEN.clear()
    h, auth, ident = legion(nia, [reply("done")], viewer="not-a-token")
    with pytest.raises(IdentityUnavailable):
        await h.run(SPEC)
    await auth.aclose()
    await ident.aclose()


async def test_issuer_token_cannot_widen_its_agent(nia: Nia) -> None:
    r = httpx.post(
        f"{nia.url}/agents/{REF}/grants",
        json={"grants": [{"kind": "tool", "object": "repo.write"}]},
        headers={"Authorization": f"Bearer {ISSUER}"},
    )
    assert r.status_code == 403


# the whole stack: the tool presents its credential to a real nia-gateway


async def test_stack_tool_uses_credential_at_the_gateway() -> None:
    raw = os.environ.get("LEGION_TEST_NIA_STACK")
    if not raw:
        pytest.skip("set LEGION_TEST_NIA_STACK to run against a running compose stack")
    stack = json.loads(raw)
    api, gw = stack["api"], stack["gateway"]
    admin = {"Authorization": f"Bearer {stack['admin']}"}
    ref = f"agent:legion-{int(time.time())}"
    httpx.post(f"{api}/agents", json={"ref": ref, "owner": "it"}, headers=admin).raise_for_status()
    httpx.post(
        f"{api}/agents/{ref}/grants",
        json={
            "grants": [
                {"kind": "tool", "object": "repo.read"},
                {"kind": "data", "object": "repo-A"},
                # the agent holds repo-B too; the credential doesn't
                {"kind": "data", "object": "repo-B"},
            ]
        },
        headers=admin,
    ).raise_for_status()
    for name in ("repo.read",):
        httpx.post(f"{api}/tools", json={"name": name}, headers=admin)
    answers: list[int] = []

    @tool(
        effect=EffectClass.READ,
        capabilities=["repo.read"],
        resource=lambda a: a.repo,
        credentials=["github"],
        name="gateway_read",
    )
    def gateway_read(args: RepoArgs, ctx: ToolContext) -> str:
        """Read a repository through the NIA gateway."""
        r = httpx.post(
            f"{gw}/tools/repo.read/call",
            json={"arguments": {"repo": args.repo}},
            headers={"Authorization": f"Bearer {ctx.credentials['github'].reveal()}"},
        )
        answers.append(r.status_code)
        other = httpx.post(
            f"{gw}/tools/repo.read/call",
            json={"arguments": {"repo": "repo-B"}},
            headers={"Authorization": f"Bearer {ctx.credentials['github'].reveal()}"},
        )
        answers.append(other.status_code)
        return f"gateway answered {r.status_code}"

    env = EnvResolver({"I": stack["issuer"], "V": stack["viewer"]})
    auth = NiaCredentialAuthority(
        "nia",
        NiaCredentialConfig(
            provider="nia", endpoint=api, credential="env:I", trusted=True, audience="nia-gateway"
        ),
        {"tester": ref},
        env,
    )
    ident = NiaIdentityPort(
        NiaIdentityConfig(provider="nia", endpoint=api, credential="env:V", agents={"tester": ref}),
        env,
    )
    h = build(
        [call("gateway_read", {"repo": "repo-A"}), reply("done")],
        extra_tools=[gateway_read],
        grantable=("repo.*",),
        identity=ident,
        options={
            "credential_mappings": {"github": MAPPING},
            "credential_authorities": {"nia": auth},
            "trusted_authorities": ("nia",),
            "credential_minimum": Assurance.BOUND,
        },
    )
    out = await h.run(agent(tools=["gateway_read"], capabilities=["repo.read:repo-A"]))
    await auth.aclose()
    await ident.aclose()
    # the gateway took the credential for its resource and refused it for another one the agent
    # does hold: the credential's scope, not just the agent's grants
    assert out.status is RunStatus.COMPLETED and answers == [200, 403]
    events = await h.events(out.run_id)
    [used] = [e.payload for e in events if e.type is E.CREDENTIAL_RESOLVED]
    assert used["assurance"] == "bound"
    assert stack["issuer"] not in text(events) and stack["viewer"] not in text(events)


def test_demo_runs_against_real_nia() -> None:
    binary = os.environ.get("LEGION_TEST_NIA_BIN")
    if not binary:
        pytest.skip("set LEGION_TEST_NIA_BIN to a built nia-api")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(root / "examples" / "nia_credential_demo.py")],
        env={**os.environ, "LEGION_DEMO_NIA_BIN": binary},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    out = result.stdout
    assert "1. read repo-A\n  run: completed\n  used: bound" in out
    assert "4. killed in NIA\n  run: failed (killed)" in out
    assert "the credential from 1 is now revoked" in out
    assert "the credential from 1 is still revoked" in out
    assert "why: no longer active at the authority" in out
