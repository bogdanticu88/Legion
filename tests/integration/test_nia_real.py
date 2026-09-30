# Legion against a real NIA control plane (cmd/api), started from a binary as its own process
# and reached only through its HTTP API. Skipped unless LEGION_TEST_NIA_BIN points at a built
# nia-api (go build -o nia-api ./cmd/api in the NIA repository).

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from legion.cli import app
from legion.config.templates import write_project

pytestmark = pytest.mark.integration

LEGION_TOKEN = "legion-viewer-token-4d2c9e1a7b"
ADMIN_TOKEN = "test-admin-token-8f3b6a0d5e"
REF = "agent:notes-assistant"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def nia(tmp_path: Path) -> Any:
    binary = os.environ.get("LEGION_TEST_NIA_BIN")
    if not binary:
        pytest.skip("set LEGION_TEST_NIA_BIN to a built nia-api")
    tokens = tmp_path / "operators.json"
    # Legion only reads; the test itself acts as the NIA operator
    tokens.write_text(
        json.dumps(
            [
                {"token": LEGION_TOKEN, "name": "legion", "roles": ["viewer"]},
                {"token": ADMIN_TOKEN, "name": "test-admin", "roles": ["admin"]},
            ]
        )
    )
    port = free_port()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "NIA_API_ADDR": f"127.0.0.1:{port}",
        "NIA_OPERATOR_TOKENS_PATH": str(tokens),
    }
    proc = subprocess.Popen([binary], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    yield url, proc
    proc.terminate()
    proc.wait(timeout=5)


def admin(url: str, method: str, path: str, body: dict[str, Any]) -> httpx.Response:
    return httpx.request(
        method, f"{url}{path}", json=body, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    )


def test_legion_against_real_nia(nia: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url, proc = nia
    project = tmp_path / "project"
    write_project(project)
    config = yaml.safe_load((project / "legion.yaml").read_text())
    config["identity"] = {
        "provider": "nia",
        "endpoint": url,
        "credential": "env:LEGION_NIA_TOKEN",
        "timeout_s": 2,
        "agents": {"notes-assistant": REF},
    }
    (project / "legion.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.chdir(project)
    monkeypatch.setenv("LEGION_NIA_TOKEN", LEGION_TOKEN)

    def run() -> dict[str, Any]:
        result = CliRunner().invoke(app, ["run", "agents/assistant.yaml", "go", "--json"])
        assert LEGION_TOKEN not in result.output
        try:
            return dict(json.loads(result.output.splitlines()[0]))
        except (ValueError, IndexError):
            return {"status": "not started", "error": result.output}

    # 1. unknown to NIA: nothing starts
    unknown = run()
    assert unknown["status"] == "not started" and "NIA has no agent" in unknown["error"]

    # 2-3. registered and active: runs
    assert (
        admin(
            url, "POST", "/agents", {"ref": REF, "display_name": "notes", "owner": "t"}
        ).status_code
        == 201
    )
    active = run()
    assert active["status"] == "completed", active

    # 4-5. killed through NIA: the next run is refused before any tool runs
    killed = admin(
        url, "POST", "/policy/kill", {"agent_ref": REF, "incident": "test", "operator": "t"}
    )
    assert killed.status_code == 200
    refused = run()
    assert refused["status"] == "failed" and refused["error_code"] == "killed", refused
    assert REF in refused["error"]

    # 6-7. restored: NIA clears the kill sentinel only (its grants and credentials stay revoked,
    # which Legion doesn't use), and Legion runs the agent again
    restored = admin(url, "POST", "/policy/restore", {"agent_ref": REF, "operator": "t"})
    assert restored.status_code == 200
    assert "grants and credentials were not restored" in restored.json()["note"]
    state = httpx.get(
        f"{url}/agents/{REF}", headers={"Authorization": f"Bearer {LEGION_TOKEN}"}
    ).json()
    assert state["effective_state"] == "active" and state["kill_sentinel_checked"] is True
    again = run()
    assert again["status"] == "completed", again

    # Legion's read-only token can't kill anything
    denied = httpx.post(
        f"{url}/policy/kill",
        json={"agent_ref": REF, "incident": "x", "operator": "legion"},
        headers={"Authorization": f"Bearer {LEGION_TOKEN}"},
    )
    assert denied.status_code == 403

    # 8. NIA gone: Legion fails closed
    proc.terminate()
    proc.wait(timeout=5)
    down = run()
    assert down["status"] == "not started" and "couldn't reach NIA" in down["error"]
