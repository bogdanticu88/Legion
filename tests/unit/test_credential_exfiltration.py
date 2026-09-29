# A distinctive credential, issued through a real legion.yaml, and every way out we could think
# of: tool results and errors, in-doubt messages, what the authority says and raises, child
# results, approvals, every CLI command's output, the raw database and debug logs. The canary
# must not come out anywhere.

import json
import logging
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from legion.cli import app
from legion.config.templates import write_project

CANARY = "LEGION-CANARY-7f3e9a1c5d2b"
OTHER = "STATIC-CANARY-4e8d2a9b6c1f"

AUTHORITY = f'''
from datetime import UTC, datetime, timedelta

from legion.access.secrets import Secret
from legion.ports.credentials import CredentialStatus, IssuedCredential

CANARY = "{CANARY}"
OTHER = "{OTHER}"


class Authority:
    name = "canary"

    def __init__(self):
        self.n = 0

    async def issue(self, request):
        self.n += 1
        n = self.n
        secret = f"{{CANARY}}-{{n}}"
        now = datetime.now(UTC)
        evidence = {{
            "authority": "canary",
            "credential_ref": f"ref-{{n}}",
            "provider": request.provider,
            "principal": request.principal,
            "subject": request.subject,
            "permissions": list(request.permissions),
            "resource": request.resource,
            "issued_at": now,
            "expires_at": now + timedelta(seconds=30),
            "action_hash": request.action_hash,
            "call_id": request.call_id,
            "grant_fingerprint": request.grant_fingerprint,
            "revocation_ref": secret + "\\x1b[2J\\x1b]0;owned\\x07",
        }}
        mode = n % 6
        if mode == 2:
            raise RuntimeError(f"could not deliver {{secret}}")
        if mode == 3:
            return IssuedCredential(
                Secret(secret), {{"nested": {{"secret": secret}}}}, credential_ref=f"ref-{{n}}"
            )
        if mode == 4:
            evidence["revocation_ref"] = OTHER
            evidence["permissions"] = [*evidence["permissions"], secret]
        if mode == 5:
            evidence["resource"] = secret + "x" * 5000
        return IssuedCredential(Secret(secret), evidence, credential_ref=f"ref-{{n}}")

    async def status(self, ref):
        return CredentialStatus.ACTIVE

    async def revoke(self, ref):
        pass


AUTHORITY = Authority()
'''

TOOLS = """
from pydantic import BaseModel

from legion.domain.action import EffectClass
from legion.domain.errors import ActionInDoubt
from legion.tools.base import ToolContext
from legion.tools.native import tool


class Args(BaseModel):
    repo: str
    mode: str = "echo"


@tool(effect=EffectClass.READ, capabilities=["repo.read"], resource=lambda a: a.repo,
      credentials=["github"])
def read_repo(args: Args, ctx: ToolContext) -> str:
    "Read a repository."
    secret = ctx.credentials["github"].reveal()
    if args.mode == "raise":
        raise RuntimeError("upstream said no to " + secret)
    return "README (token " + secret + ")"


@tool(effect=EffectClass.WRITE, capabilities=["repo.issue.create"], resource=lambda a: a.repo,
      credentials=["github"])
def create_issue(args: Args, ctx: ToolContext) -> str:
    "Open an issue."
    raise ActionInDoubt("lost the answer while using " + ctx.credentials["github"].reveal())


@tool(effect=EffectClass.READ, capabilities=["legacy.read"], resource=lambda a: a.repo,
      credentials=["legacy"])
def read_legacy(args: Args, ctx: ToolContext) -> str:
    "Read with the old static token."
    return "legacy " + ctx.credentials["legacy"].reveal()


TOOLS = [read_repo, create_issue, read_legacy]
"""

MAIN_SCRIPT = [
    {"tool_calls": [{"name": "read_repo", "arguments": {"repo": "repo-A"}}]},
    {"tool_calls": [{"name": "read_repo", "arguments": {"repo": "repo-A", "mode": "raise"}}]},
    {"tool_calls": [{"name": "read_repo", "arguments": {"repo": "repo-A", "mode": "b"}}]},
    {"tool_calls": [{"name": "read_repo", "arguments": {"repo": "repo-A", "mode": "c"}}]},
    {"tool_calls": [{"name": "read_repo", "arguments": {"repo": "repo-A", "mode": "d"}}]},
    {"tool_calls": [{"name": "read_legacy", "arguments": {"repo": "repo-A"}}]},
    {"tool_calls": [{"name": "delegate", "arguments": {"agent": "helper", "objective": "read"}}]},
    {"tool_calls": [{"name": "create_issue", "arguments": {"repo": "repo-A"}}]},
    {"text": "done"},
]
CHILD_SCRIPT = [
    {"tool_calls": [{"name": "read_repo", "arguments": {"repo": "repo-A"}}]},
    {"text": "the helper read it"},
]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    write_project(tmp_path)
    (tmp_path / "canary_authority.py").write_text(AUTHORITY)
    (tmp_path / "canary_tools.py").write_text(TOOLS)
    (tmp_path / "scripts" / "main.yaml").write_text(yaml.safe_dump(MAIN_SCRIPT))
    (tmp_path / "scripts" / "child.yaml").write_text(yaml.safe_dump(CHILD_SCRIPT))
    (tmp_path / "agents" / "main.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "main",
                "instructions": "work",
                "model": {"profile": "canary/main", "needs": ["tools"]},
                "tools": ["read_repo", "create_issue", "read_legacy", "delegate"],
                "capabilities": [
                    "repo.read:repo-A",
                    "repo.issue.create:repo-A",
                    "legacy.read:repo-A",
                    "agent.delegate:helper",
                ],
                "delegation": {"max_depth": 1, "max_children": 1},
                "budget": {"steps": 30, "tool_calls": 30, "model_calls": 30},
            }
        )
    )
    (tmp_path / "agents" / "helper.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "helper",
                "instructions": "read",
                "model": {"profile": "canary/child", "needs": ["tools"]},
                "tools": ["read_repo"],
                "capabilities": ["repo.read:repo-A"],
            }
        )
    )
    config = yaml.safe_load((tmp_path / "legion.yaml").read_text())
    config["tool_modules"].append("canary_tools.py")
    config["authority"]["grantable"] += ["repo.*", "legacy.read:**", "agent.delegate:**"]
    config["providers"]["main-script"] = {"kind": "scripted", "script": "scripts/main.yaml"}
    config["providers"]["child-script"] = {"kind": "scripted", "script": "scripts/child.yaml"}
    config["models"] += [
        {"profile": "canary/main", "provider": "main-script", "model": "x"},
        {"profile": "canary/child", "provider": "child-script", "model": "x"},
    ]
    config["credentials"] = {
        "legacy": "env:LEGACY_TOKEN",
        "github": {
            "authority": "canary",
            "provider": "github",
            "permissions": {"repo.read": ["contents:read"], "repo.issue.create": ["issues:write"]},
            "max_lifetime_s": 60,
            "minimum": "unverified",
        },
    }
    config["credential_authorities"] = {
        "canary": {"module": "canary_authority.py", "trusted": True}
    }
    config["policy"] = {
        "default": "allow",
        "rules": [{"decision": "require_approval", "tool": "create_issue"}],
    }
    (tmp_path / "legion.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.setenv("LEGACY_TOKEN", OTHER)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def cli(*args: str) -> str:
    result = CliRunner().invoke(app, list(args))
    return result.output + (str(result.exception) if result.exception else "")


def test_the_canary_never_comes_out(project: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    outputs = [cli("run", "agents/main.yaml", "go", "--json")]
    run_id = json.loads(outputs[0].splitlines()[0])["run_id"] if outputs[0].startswith("{") else ""
    if not run_id:
        run_id = next(w for w in cli("runs").split() if w.startswith("run_"))
    approval = next(
        (w for w in cli("approvals").split() if w.startswith("apr_")),
        None,
    )
    if approval:
        outputs += [cli("approval", "show", approval), cli("approve", approval, "--note", "ok")]
        outputs.append(cli("resume", run_id, "--json"))
    for command in (
        ["inspect", run_id],
        ["inspect", run_id, "--json"],
        ["tasks", run_id],
        ["runs"],
        ["credentials", run_id],
        ["credentials", run_id, "--json"],
        ["approvals"],
        ["verify", run_id],
    ):
        outputs.append(cli(*command))
    blocked = [w for w in cli("resume", run_id, "--json").split('"') if w.startswith("call_")]
    if blocked:
        outputs.append(cli("reconcile", run_id, blocked[0], "--outcome", "applied"))
        outputs.append(cli("resume", run_id, "--json"))

    everything = "\n".join(outputs) + caplog.text
    # the run really went through the dangerous paths
    creds = [json.loads(line) for line in cli("credentials", run_id, "--json").splitlines()]
    # every authority behaviour ran: good, raising, malformed, other secret, oversized
    assert len(creds) >= 8
    problems = " ".join(p for c in creds for p in c["problems"])
    assert "authority failed" in problems and "malformed evidence" in problems
    assert "permissions wider" in problems
    assert any(c["authority"] == "static" for c in creds)
    assert any(c["subject"] == "helper" for c in creds)
    assert "[redacted]" in cli("inspect", run_id, "--json")

    assert CANARY not in everything
    assert OTHER not in everything
    assert "\x1b" not in everything
    data = b"".join(p.read_bytes() for p in (project / ".legion").rglob("*") if p.is_file())
    assert CANARY.encode() not in data
    assert OTHER.encode() not in data
