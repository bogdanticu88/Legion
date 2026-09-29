# Through the CLI: a long secret echoed in evidence, and evidence that raises with the secret in
# its message, must not come out in any command's output or the database.

import json
from pathlib import Path

import yaml

from tests.unit.test_credential_exfiltration import cli, project  # noqa: F401

LONG = "JWT" + "abcdefghij" * 20  # 203 chars

AUTH = f'''
from datetime import UTC, datetime, timedelta
from pydantic import BaseModel
from legion.access.secrets import Secret
from legion.ports.credentials import CredentialStatus, IssuedCredential

class Authority:
    name = "canary"
    def __init__(self):
        self.n = 0
    async def issue(self, request):
        self.n += 1
        secret = "{LONG}" + str(self.n)
        now = datetime.now(UTC)
        ev = {{
            "authority": "canary", "credential_ref": f"ref-{{self.n}}",
            "provider": request.provider, "principal": request.principal,
            "subject": request.subject, "permissions": list(request.permissions),
            "resource": request.resource, "issued_at": now,
            "expires_at": now + timedelta(seconds=30), "action_hash": request.action_hash,
            "call_id": request.call_id, "grant_fingerprint": request.grant_fingerprint,
            "revocation_ref": secret,
        }}
        if self.n == 2:
            class Ev(BaseModel):
                def model_dump(self, *a, **k):
                    raise RuntimeError("evidence broke for " + secret)
            return IssuedCredential(Secret(secret), Ev(), credential_ref=f"ref-{{self.n}}")
        return IssuedCredential(Secret(secret), ev, credential_ref=f"ref-{{self.n}}")
    async def status(self, ref):
        return CredentialStatus.ACTIVE
    async def revoke(self, ref):
        pass

AUTHORITY = Authority()
'''

SCRIPT = [
    {"tool_calls": [{"name": "read_repo", "arguments": {"repo": "repo-A"}}]},
    {"tool_calls": [{"name": "read_repo", "arguments": {"repo": "repo-A", "mode": "b"}}]},
    {"text": "done"},
]


def test_long_or_raising_evidence_leaks_nothing(project: Path) -> None:  # noqa: F811
    (project / "canary_authority.py").write_text(AUTH)
    (project / "scripts" / "main.yaml").write_text(yaml.safe_dump(SCRIPT))
    run_out = cli("run", "agents/main.yaml", "go", "--json")
    run_id = next(w for w in cli("runs").split() if w.startswith("run_"))
    outs = {
        "run": run_out,
        "inspect": cli("inspect", run_id),
        "inspect-json": cli("inspect", run_id, "--json"),
        "credentials": cli("credentials", run_id),
        "credentials-json": cli("credentials", run_id, "--json"),
        "tasks": cli("tasks", run_id),
        "runs": cli("runs"),
    }
    data = b"".join(p.read_bytes() for p in (project / ".legion").rglob("*") if p.is_file())
    prefix = LONG[:80]
    hits = {k: prefix in v for k, v in outs.items()}
    hits["sqlite"] = prefix.encode() in data
    full = {k: (LONG + "2") in v for k, v in outs.items()}
    full["sqlite"] = (LONG + "2").encode() in data
    assert not any(hits.values()), hits
    assert not any(full.values()), full
    # both paths really ran: one credential used with the secret echoed in its evidence, one
    # refused because its evidence raised
    rows = [json.loads(line) for line in outs["credentials-json"].splitlines()]
    assert [r["decision"] for r in rows] == ["used", "refused"]
    assert rows[1]["problems"] == ["evidence couldn't be read: RuntimeError"]
