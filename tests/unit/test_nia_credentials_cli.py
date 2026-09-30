# A real Legion project on disk with both NIA adapters configured in legion.yaml, run through the
# CLI with an approval and a resume, HTTP debug logging on, and a tool that raises with its
# credential in the message. Then every CLI view, the SQLite file, the state directory and the
# logs are searched for the viewer token, the issuer token and every scoped secret NIA issued,
# whole and in parts.

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from legion.cli import app
from legion.config.templates import write_project
from tests.nia_cred_lab import ISSUER, VIEWER, serve
from tests.nia_cred_support import fake

TOOLS = '''
from pydantic import BaseModel
from legion.domain.action import EffectClass
from legion.tools.base import ToolContext
from legion.tools.native import tool


class RepoArgs(BaseModel):
    repo: str
    title: str = ""


@tool(effect=EffectClass.READ, capabilities=["repo.read"], resource=lambda a: a.repo,
      credentials=["github"])
def read_repo(args: RepoArgs, ctx: ToolContext) -> str:
    """Read a repository."""
    token = ctx.credentials["github"].reveal()
    if args.title == "raise":
        raise RuntimeError(f"upstream refused {token} and {token.partition('.')[2]}")
    return f"README of {args.repo}, token {token}, secret {token.partition('.')[2]}"


TOOLS = [read_repo]
'''


def project(tmp_path: Path, url: str) -> Path:
    write_project(tmp_path)
    (tmp_path / "repo_tools.py").write_text(TOOLS)
    path = tmp_path / "legion.yaml"
    config = yaml.safe_load(path.read_text())
    config["tool_modules"] = [*config.get("tool_modules", []), "repo_tools.py"]
    config["authority"]["grantable"] = [*config["authority"]["grantable"], "repo.*"]
    config["identity"] = {
        "provider": "nia",
        "endpoint": url,
        "credential": "env:T_VIEWER",
        "timeout_s": 2,
        "agents": {"notes-assistant": "agent:tester"},
    }
    config["credential_authorities"] = {
        "nia": {
            "provider": "nia",
            "endpoint": url,
            "credential": "env:T_ISSUER",
            "audience": "nia-gateway",
            "trusted": True,
            "timeout_s": 2,
        }
    }
    config["credential_policy"] = {"minimum": "bound", "timeout_s": 10}
    config["credentials"] = {
        "github": {
            "authority": "nia",
            "provider": "nia-gateway",
            "permissions": {"repo.read": ["repo.read"]},
            "max_lifetime_s": 120,
        }
    }
    config["policy"] = {
        "default": "allow",
        "rules": [{"tool": "read_repo", "decision": "require_approval"}],
    }
    path.write_text(yaml.safe_dump(config))
    spec_path = tmp_path / "agents" / "assistant.yaml"
    spec = yaml.safe_load(spec_path.read_text())
    spec["tools"] = ["read_repo"]
    spec["capabilities"] = ["repo.read:repo-A"]
    spec_path.write_text(yaml.safe_dump(spec))
    return tmp_path


def script(tmp_path: Path) -> None:
    # the template's scripted model: call the tool twice (once raising), then answer
    steps = [
        {"tool_calls": [{"name": "read_repo", "arguments": {"repo": "repo-A"}}]},
        {"tool_calls": [{"name": "read_repo", "arguments": {"repo": "repo-A", "title": "raise"}}]},
        {"text": "done"},
    ]
    (tmp_path / "scripts" / "assistant.yaml").write_text(yaml.safe_dump(steps))


def test_nothing_secret_anywhere_the_cli_or_store_can_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    nia = fake()
    runner = CliRunner()
    outputs: list[str] = []
    with serve(nia) as url:
        root = project(tmp_path, url)
        script(root)
        monkeypatch.chdir(root)
        monkeypatch.setenv("T_VIEWER", VIEWER)
        monkeypatch.setenv("T_ISSUER", ISSUER)
        caplog.set_level(logging.DEBUG)

        def cli(*args: str) -> Any:
            result = runner.invoke(app, list(args))
            outputs.append(result.output)
            if result.exception is not None:
                outputs.append(repr(result.exception))
            return result

        first = cli("run", "agents/assistant.yaml", "go", "--json")
        assert first.output.startswith("{"), first.output[:500]
        run_id = json.loads(first.output.splitlines()[0])["run_id"]
        for _ in range(3):
            cli("approvals")
            pending = re.findall(r"apr_[0-9a-f]{16}", outputs[-1])
            if not pending:
                break
            approval = pending[0]
            cli("approval", "show", approval)
            cli("approve", approval)
            cli("resume", run_id, "--json")
        for command in (
            ("inspect", run_id),
            ("credentials", run_id),
            ("credentials", run_id, "--json"),
            ("runs",),
            ("tasks", run_id),
            ("verify", run_id),
        ):
            cli(*command)
    planted = [ISSUER, VIEWER]
    for c in nia.creds.values():
        planted += [c.secret, f"{c.ref}.{c.secret}"]
    # both calls got a credential (the second tool raised with it in the message)
    assert len(nia.creds) == 2, "the test didn't exercise both calls"
    assert any("upstream refused" in o or "RuntimeError" in o for o in outputs)
    haystacks = [*outputs, caplog.text]
    for path in (root / ".legion").rglob("*"):
        if path.is_file():
            haystacks.append(path.read_bytes().decode("utf-8", "replace"))
    for i, text in enumerate(haystacks):
        for value in planted:
            assert value not in text, f"secret in output {i}"
    joined = "\n".join(outputs)
    assert "credential" in joined and "lc-" in joined
