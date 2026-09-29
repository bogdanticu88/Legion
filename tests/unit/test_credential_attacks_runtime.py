# More attacks from the Phase 5A review: an approval across a crash between refusal events,
# config typos, duplicate references within a call, kill state during replacement, and credentials
# the authority gave no usable evidence for.

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from legion.access.secrets import EnvResolver, Secret, SecretRef
from legion.authority.policy import Rule, Verdict
from legion.domain.action import EffectClass
from legion.domain.errors import ConfigError
from legion.domain.states import RunStatus
from legion.events.types import EventType
from legion.kernel.credentials import CredentialBroker, CredentialMapping
from legion.models.scripted import call, reply
from legion.ports.credentials import Assurance, CredentialRequest, IssuedCredential
from legion.tools.base import ToolContext
from legion.tools.native import tool
from tests.credential_lab import LabAuthority, RepoArgs
from tests.support import SimulatedCrash, agent, build, crash_at
from tests.unit.test_credential_boundaries import approved_run
from tests.unit.test_credential_policy import MAPPING, Clock, setup, spec

E = EventType
READ_A = call("read_repo", {"repo": "repo-A"})


async def test_crash_between_credential_refused_and_action_refused_reuses_the_approval() -> None:
    lab = LabAuthority(fail="crash")
    h, first, seen = await approved_run(lab)
    crashing = h.restart(faults=crash_at("after:credential.refused"))
    with pytest.raises(SimulatedCrash):
        await crashing.resume(first.run_id)
    assert seen == []
    lab.fail = None  # authority is back
    await h.restart(faults=None).resume(first.run_id)
    # ADR: a credential failure after approval consumes it; the call doesn't run
    assert seen == [], "approved call ran after a recorded credential refusal"


# two credential names on one tool, same reference from the authority for both


async def test_same_reference_for_two_names_in_one_call() -> None:
    lab = LabAuthority(fixed_ref="one-ref")
    seen: list[str] = []

    @tool(
        effect=EffectClass.READ,
        capabilities=["repo.read"],
        resource=lambda a: a.repo,
        credentials=["github", "github2"],
        name="read_two",
    )
    def read_two(args: RepoArgs, ctx: ToolContext) -> str:
        """Read with two creds."""
        seen.append(ctx.credentials["github"].reveal())
        return "ok"

    h = build(
        [call("read_two", {"repo": "repo-A"}), reply("ok")],
        extra_tools=[read_two],
        grantable=("repo.*",),
        options={
            "credential_mappings": {"github": MAPPING, "github2": MAPPING},
            "credential_authorities": {"lab": lab},
            "trusted_authorities": ("lab",),
            "credential_timeout_s": 0.2,
        },
    )
    await h.run(agent(tools=["read_two"], capabilities=["repo.read:repo-A"]))
    assert seen == []


# the requirement can't be lowered


def test_mapping_minimum_cannot_lower_global() -> None:
    low = MAPPING.model_copy(update={"minimum": Assurance.UNVERIFIED})
    b = CredentialBroker(
        resolver=EnvResolver({}),
        mapped={"github": low},
        authorities={"lab": LabAuthority()},
        minimum=Assurance.BOUND,
    )
    assert b.required("github") is Assurance.BOUND


@pytest.mark.parametrize(
    "policy",
    [
        {"minimum": "Bound"},
        {"minimum": "bound "},
        {"minimun": "bound"},
        {"minimum": None},
        {"minimum": 3},
        {"minimum": "BOUND"},
    ],
)
def test_credential_policy_typos_are_rejected(policy: dict[str, Any]) -> None:
    from legion.config.loader import CredentialPolicy

    with pytest.raises((ValidationError, ValueError)):
        CredentialPolicy.model_validate(policy)


def test_rule_credential_assurance_typo_is_rejected() -> None:
    with pytest.raises((ValidationError, ValueError)):
        Rule.model_validate({"decision": "allow", "credential_assurance": "Bound"})
    with pytest.raises((ValidationError, ValueError)):
        Rule.model_validate({"decision": "allow", "credential_assurence": "bound"})


def test_mapping_typos() -> None:
    with pytest.raises((ValidationError, ValueError)):
        CredentialMapping.model_validate(
            {
                "authority": "lab",
                "provider": "gh",
                "permissions": {"repo.read": ["x"]},
                "minimum": "Bound",
            }
        )
    with pytest.raises((ValidationError, ValueError)):
        CredentialMapping.model_validate(
            {
                "authority": "lab",
                "provider": "gh",
                "permissions": {"repo.read": ["x"]},
                "max_lifetime_s": -1,
            }
        )
    with pytest.raises((ValidationError, ValueError)):
        CredentialMapping.model_validate(
            {"authority": "lab", "provider": "gh", "permissions": {"repo.read": "contents:read"}}
        )


def test_yaml_duplicate_credential_keys(tmp_path: Path) -> None:
    from legion.config.loader import load_config
    from legion.config.templates import write_project

    write_project(tmp_path)
    cfg = tmp_path / "legion.yaml"
    text = cfg.read_text()
    (tmp_path / "auth.py").write_text(
        "from tests.credential_lab import LabAuthority\nAUTHORITY = LabAuthority(name='lab')\n"
    )
    text += (
        "\ncredentials:\n  github: env:GH\n  github:\n    authority: lab\n    provider: github\n"
        "    permissions: {repo.read: [contents:read]}\n"
        "credential_authorities:\n  lab: {module: auth.py, trusted: true}\n"
    )
    cfg.write_text(text)
    with pytest.raises(ConfigError):
        load_config(cfg)


def test_authority_with_wrong_name(tmp_path: Path) -> None:
    from legion.config.loader import load_authority_module

    (tmp_path / "a.py").write_text(
        "from tests.credential_lab import LabAuthority\nAUTHORITY = LabAuthority(name='other')\n"
    )
    with pytest.raises(ConfigError):
        load_authority_module(tmp_path / "a.py", "lab")


def test_undefined_authority_in_broker() -> None:
    with pytest.raises(ConfigError):
        CredentialBroker(resolver=EnvResolver({}), mapped={"github": MAPPING}, authorities={})
    with pytest.raises(ConfigError):
        CredentialBroker(
            resolver=EnvResolver({}),
            static={"github": SecretRef.parse("env:GH")},
            mapped={"github": MAPPING},
            authorities={"lab": LabAuthority()},
        )


# MCP with a policy rule


async def test_mcp_respects_policy_rule_requirement() -> None:
    pytest.importorskip("mcp")
    from tests.mcp_lab import Lab, connect, pinned, server_config

    lab = Lab()
    config = await pinned(lab, server_config(read_note={"effect": "read", "resource_arg": "path"}))
    conn, found = await connect(lab, config)
    mcp_spec = agent(tools=["mcp_lab_read_note"], capabilities=["mcp.lab.read_note:notes/**"])
    steps = [call("mcp_lab_read_note", {"path": "notes/a.md"}), reply("ok")]
    rule = Rule(decision=Verdict.ALLOW, capability="mcp.*", credential_assurance=Assurance.VERIFIED)
    h = build(steps, extra_tools=found.tools, grantable=("mcp.*",), rules=[rule])
    await h.run(mcp_spec)
    await conn.aclose()
    assert not lab.effects


# a revoked credential's reference is added to `seen` (refused events too) so a re-issue under
# the same reference is refused


async def test_kill_during_issue_revokes() -> None:
    from tests.unit.test_credential_boundaries import Switch

    ident = Switch()
    lab = LabAuthority(on_issue=lambda n: setattr(ident, "dead", True))
    h, lab, seen = setup([READ_A, reply("ok")], lab, identity=ident)
    await h.run(spec())
    assert seen == []
    assert "cred-001" in lab.revoked


# evidence processing that raises something other than ValidationError/TypeError/ValueError


class HostileEvidence:
    pass


def hostile_authority(kind: str) -> LabAuthority:
    from pydantic import BaseModel

    class A(LabAuthority):
        async def issue(self, request: CredentialRequest) -> IssuedCredential:
            got = await super().issue(request)
            secret = self.secrets[-1]

            class Ev(BaseModel):
                def model_dump(self, *a: Any, **k: Any) -> Any:  # type: ignore[override]
                    raise RuntimeError(f"cannot dump evidence for {secret}")

            if kind == "model_dump":
                return IssuedCredential(Secret(secret), Ev())

            class Bad(dict):  # type: ignore[type-arg]
                def __getitem__(self, key: Any) -> Any:
                    raise KeyError(secret)

                def keys(self) -> Any:
                    raise LookupError(secret)

                def items(self) -> Any:
                    raise LookupError(secret)

            return IssuedCredential(Secret(secret), Bad(got.evidence))

    return A()


@pytest.mark.parametrize("kind", ["model_dump", "mapping"])
async def test_hostile_evidence_object_is_refused_and_secret_not_recorded(
    kind: str, tmp_path: Path
) -> None:
    from legion.events.sqlite_store import SqliteEventStore

    store = SqliteEventStore(tmp_path / "e.db")
    lab = hostile_authority(kind)
    h, lab, seen = setup([READ_A, reply("ok")], lab, store=store)
    try:
        outcome = await h.run(spec())
        status = outcome.status
    except Exception as exc:
        status = f"raised {type(exc).__name__}: {exc}"
    [summary] = await store.runs()
    events = await h.events(summary.run_id)
    store.close()
    secret = lab.secrets[0]
    dumped = "\n".join(e.model_dump_json() for e in events)
    assert secret not in dumped, f"secret in events; status={status}"
    assert secret.encode() not in (tmp_path / "e.db").read_bytes()
    assert seen == []
    assert status is RunStatus.COMPLETED, status


# kill between the first issue and the expiry replacement


async def test_kill_between_issue_and_replacement_issues_nothing_more() -> None:
    from tests.unit.test_credential_boundaries import Switch

    clock = Clock()
    ident = Switch()
    done = [False]

    def hook(point: str) -> None:
        if point == "after:credential.resolved" and not done[0]:
            done[0] = True
            ident.dead = True
            clock.advance(120)

    h, lab, seen = setup([READ_A, reply("ok")], clock=clock, identity=ident, faults=hook)
    with contextlib.suppress(Exception):
        await h.run(spec())
    assert seen == []
    assert len(lab.issued) == 1, "a replacement was issued to a killed agent"


# missing evidence on a refused credential: can't be revoked (ADR says revoked best effort)


async def test_refused_missing_evidence_credential_is_revoked() -> None:
    lab = LabAuthority(evidence="missing")
    h, lab, seen = setup([READ_A, reply("ok")], lab)  # mapping minimum bound -> refused
    await h.run(spec())
    assert seen == []
    assert lab.revoked, "issued credential never revoked"


# malformed evidence + revoked at the authority: dispatched anyway at unverified requirement


async def test_revoked_credential_with_malformed_evidence_is_not_used() -> None:
    lab = LabAuthority(evidence="malformed", revoke_on_issue=True)
    low = MAPPING.model_copy(update={"minimum": Assurance.UNVERIFIED})
    h, lab, seen = setup([READ_A, reply("ok")], lab, mapping=low)
    await h.run(spec())
    assert seen == [], "revoked credential used because Legion never learnt its reference"
