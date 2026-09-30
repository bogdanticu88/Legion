# NiaCredentialAuthority behind the generic CredentialAuthority port, against a NIA stand-in that
# answers the way NIA's scoped credential API does and misbehaves on request. The adapter
# translates; Legion's own assessment decides. Every hostile answer has to end in a refused
# call, never a wider credential, and never a secret anywhere but the tool.

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from legion.adapters.nia_credentials import NiaCredentialError
from legion.domain.action import EffectClass
from legion.domain.states import RunStatus
from legion.events.types import EventType
from legion.models.scripted import call, calls, reply
from legion.ports.credentials import CredentialRequest, CredentialStatus, UnknownCredential
from legion.tools.base import ToolContext
from legion.tools.native import tool
from tests.nia_cred_lab import ISSUER, VIEWER, serve
from tests.nia_cred_support import AGENTS, Clock, authority, fake, setup, spec

E = EventType
READ_A = call("read_repo", {"repo": "repo-A"})
HASH = "a" * 64


def request(**kw: Any) -> CredentialRequest:
    base: dict[str, Any] = {
        "authority": "nia",
        "provider": "nia-gateway",
        "permissions": ("repo.read",),
        "resource": "repo-A",
        "capabilities": ("repo.read:repo-A",),
        "principal": "human:tester",
        "subject": "tester",
        "on_behalf_of": (),
        "run_id": "run",
        "task_id": "task",
        "call_id": "lc-0123456789abcdef0123456789abcdef",
        "action_hash": HASH,
        "grant_id": "g",
        "grant_fingerprint": "b" * 64,
        "tool": "read_repo",
        "max_lifetime_s": 60,
    }
    base.update(kw)
    return CredentialRequest(**base)


async def dump(h: Any, run_id: str) -> str:
    return json.dumps([e.model_dump(mode="json") for e in await h.events(run_id)])


def refused(events: list[Any]) -> list[dict[str, Any]]:
    return [e.payload for e in events if e.type is E.CREDENTIAL_REFUSED]


# the adapter on its own


async def test_request_mapping_is_exactly_legions_request() -> None:
    nia = fake()
    with serve(nia) as url:
        auth = authority(url)
        issued = await auth.issue(request())
        await auth.aclose()
    [(endpoint, path, auth_header, body)] = nia.requests
    assert endpoint == "issue" and path == "/agents/agent%3Atester/scoped-credentials"
    assert auth_header == f"Bearer {ISSUER}"
    assert body == {
        "permissions": ["repo.read"],
        "resource": "repo-A",
        "audience": "nia-gateway",
        "action_hash": HASH,
        "call_id": "lc-0123456789abcdef0123456789abcdef",
        "grant_fingerprint": "b" * 64,
        "ttl_seconds": 60,
    }
    ev = issued.evidence
    assert ev["authority"] == "nia" and ev["subject"] == "tester"
    assert ev["external_principal"] == "agent:tester" and ev["provider"] == "nia-gateway"
    assert ev["principal"] == "human:tester"
    assert "verified" not in ev
    assert issued.credential_ref == ev["credential_ref"] == ev["revocation_ref"]
    assert issued.secret.reveal().startswith(issued.credential_ref + ".")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"subject": "nobody"}, "no NIA identity"),
        ({"resource": None}, "need a resource"),
        ({"provider": "github"}, "isn't this NIA authority's audience"),
        ({"resource": "repo-\u0410"}, "resource isn't one NIA accepts"),
        ({"resource": "*"}, "resource isn't one NIA accepts"),
        ({"permissions": ("*",)}, "permission isn't one NIA accepts"),
        ({"permissions": ()}, "permission isn't one NIA accepts"),
        ({"external_principal": "agent:helper"}, "to the identity authority"),
    ],
)
async def test_requests_refused_before_nia_is_asked(change: dict[str, Any], reason: str) -> None:
    nia = fake()
    with serve(nia) as url:
        auth = authority(url)
        with pytest.raises(NiaCredentialError, match=reason):
            await auth.issue(request(**change))
        await auth.aclose()
    assert nia.requests == []


async def test_status_mapping() -> None:
    clock = Clock()
    nia = fake(clock)
    with serve(nia) as url:
        auth = authority(url)
        a = await auth.issue(request())
        assert await auth.status(a.credential_ref) is CredentialStatus.ACTIVE
        clock.advance(61)
        assert await auth.status(a.credential_ref) is CredentialStatus.EXPIRED
        # expired, then revoked: revoked, never collapsed into expired
        await auth.revoke(a.credential_ref)
        assert await auth.status(a.credential_ref) is CredentialStatus.REVOKED
        await auth.revoke(a.credential_ref)  # already revoked is fine
        with pytest.raises(UnknownCredential):
            await auth.status("nia-sc-" + "0" * 32)
        with pytest.raises(UnknownCredential):
            await auth.revoke("nia-sc-" + "0" * 32)
        b = await auth.issue(request())
        del nia.creds[b.credential_ref]  # NIA lost it (restart without Postgres)
        with pytest.raises(UnknownCredential):
            await auth.status(b.credential_ref)
        await auth.aclose()


@pytest.mark.parametrize(
    ("mods", "error"),
    [
        ({"credential_ref": "nia-sc-" + "1" * 32}, NiaCredentialError),
        ({"principal": "agent:helper"}, NiaCredentialError),
        ({"status": "sort-of"}, NiaCredentialError),
        ({"status": "unknown"}, UnknownCredential),
    ],
)
async def test_status_answers_about_something_else(mods: dict[str, Any], error: type) -> None:
    nia = fake()
    with serve(nia) as url:
        auth = authority(url)
        a = await auth.issue(request())
        nia.status_mods = mods
        with pytest.raises(error):
            await auth.status(a.credential_ref)
        await auth.aclose()


@pytest.mark.parametrize(
    "mode",
    [
        "401",
        "403",
        "429",
        "500",
        "502",
        "503",
        "504",
        "redirect",
        "slow",
        "drop",
        "gzip",
        "huge",
        "malformed",
        "not_object",
        "nan",
        "empty",
        "secret_in_error",
    ],
)
async def test_status_failures_are_never_a_status(mode: str) -> None:
    nia = fake()
    with serve(nia) as url:
        auth = authority(url)
        a = await auth.issue(request())
        nia.modes["status"] = mode
        with pytest.raises((NiaCredentialError, UnknownCredential)) as info:
            await auth.status(a.credential_ref)
        await auth.aclose()
    text = str(info.value)
    for planted in (ISSUER, VIEWER, *nia.issued_secrets()):
        assert planted not in text


async def test_issuance_is_not_repeated_once_it_may_have_arrived() -> None:
    nia = fake()
    nia.modes["issue"] = "drop"
    with serve(nia) as url:
        auth = authority(url)
        with pytest.raises(NiaCredentialError):
            await auth.issue(request())
        await auth.aclose()
    assert nia.counts["issue"] == 1


async def test_bad_token_is_revoked_and_refused() -> None:
    nia = fake()
    nia.answer_mods = {"token": "nia-sc-" + "2" * 32 + ".somebody-elses-secret-000"}
    with serve(nia) as url:
        auth = authority(url)
        with pytest.raises(NiaCredentialError, match="no usable token"):
            await auth.issue(request())
        await auth.aclose()
    [cred] = nia.creds.values()
    assert cred.revoked_at is not None


# through Legion: the evidence is judged by Legion's own assessment


@pytest.mark.parametrize(
    ("mods", "why"),
    [
        ({"principal": "agent:helper"}, "different principal"),
        ({"principal": "Agent:Tester"}, "different principal"),
        ({"principal": "agent:tester "}, "different principal"),
        ({"principal": "agent:nobody"}, "different principal"),
        ({"resource": "repo-B"}, "wider than"),
        ({"resource": "repo-a"}, "wider than"),
        ({"permissions": ["repo.read", "repo.write"]}, "permissions wider"),
        ({"permissions": ["repo.write"]}, "permissions wider"),
        ({"action_hash": "f" * 64}, "different action"),
        ({"call_id": "lc-ffffffffffffffffffffffffffffffff"}, "different call"),
        ({"grant_fingerprint": "e" * 64}, "different grant"),
        ({"audience": "other-gateway"}, "for provider"),
        ({"issuer": "mallory"}, "issued by"),
        ({"expires_at": "2027-01-01T00:00:00Z"}, "lives longer"),
        (
            {"issued_at": "2026-09-30T08:00:00Z", "expires_at": "2026-09-30T09:00:00Z"},
            "already expired",
        ),
        ({"credential_ref": "nia-sc-" + "3" * 32}, "doesn't match its evidence"),
        ({"permissions": "repo.read"}, "below the bound"),
        ({"format": "nia.scoped-credential.evidence/v9"}, "below the bound"),
        ({"issued_at": "yesterday"}, "below the bound"),
        ({"resource": "x" * 5000}, "wider"),
    ],
)
async def test_evidence_attacks_are_refused(mods: dict[str, Any], why: str) -> None:
    clock = Clock()
    nia = fake(clock)
    nia.evidence_mods = mods
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")], clock=clock)
        out = await run.h.run(spec())
        await run.close()
    events = await run.h.events(out.run_id)
    [problem] = refused(events)
    assert any(why in p for p in problem["problems"]), problem["problems"]
    assert run.seen == [], "the tool must not run"
    # whatever NIA issued has been revoked again
    assert all(c.revoked_at is not None for c in nia.creds.values())


async def test_nia_saying_verified_is_ignored() -> None:
    nia = fake()
    nia.evidence_mods = {"verified": True, "call_id": "lc-" + "9" * 32}
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")])
        out = await run.h.run(spec())
        await run.close()
    [problem] = refused(await run.h.events(out.run_id))
    assert problem["assurance"] is None and "bound to a different call" in problem["problems"]


async def test_untrusted_nia_is_declared_at_most() -> None:
    nia = fake()
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")], options={"trusted_authorities": ()})
        out = await run.h.run(spec())
        await run.close()
    [problem] = refused(await run.h.events(out.run_id))
    assert problem["assurance"] == "declared"


@pytest.mark.parametrize(
    "mode",
    [
        "400",
        "401",
        "403",
        "404",
        "429",
        "500",
        "502",
        "503",
        "504",
        "redirect",
        "slow",
        "drop",
        "gzip",
        "huge",
        "malformed",
        "not_object",
        "nan",
        "empty",
        "secret_in_error",
    ],
)
async def test_issuance_failures_refuse_the_call_and_leak_nothing(mode: str) -> None:
    nia = fake()
    nia.modes["issue"] = mode
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")])
        out = await run.h.run(spec())
        await run.close()
    events = await run.h.events(out.run_id)
    assert refused(events) and run.seen == []
    text = await dump(run.h, out.run_id)
    for planted in (ISSUER, VIEWER, *nia.issued_secrets()):
        assert planted not in text


class SearchArgs(BaseModel):
    q: str


class RepoArgs(BaseModel):
    repo: str


@tool(effect=EffectClass.READ, capabilities=["repo.read"], credentials=["github"])
def search(args: SearchArgs, ctx: ToolContext) -> str:
    """Search everything."""
    return "results"


async def test_resource_less_call_never_reaches_nia() -> None:
    nia = fake()
    with serve(nia) as url:
        run = setup(url, [call("search", {"q": "x"}), reply("done")], tools=[search])
        out = await run.h.run(spec("repo.read", tools=["search"]))
        await run.close()
    [problem] = refused(await run.h.events(out.run_id))
    assert "NiaCredentialError" in problem["problems"][0]
    assert "issue" not in nia.counts


async def test_two_identical_calls_get_two_call_ids() -> None:
    nia = fake()
    same = {"repo": "repo-A"}
    with serve(nia) as url:
        both = calls([("read_repo", same), ("read_repo", same)], ids=["x", "x"])
        run = setup(url, [both, reply("done")])
        out = await run.h.run(spec())
        await run.close()
    events = await run.h.events(out.run_id)
    issued = [b for (e, _, _, b) in nia.requests if e == "issue"]
    assert len(issued) == 2
    assert issued[0]["action_hash"] == issued[1]["action_hash"]
    assert issued[0]["call_id"] != issued[1]["call_id"]
    proposed = [e.payload for e in events if e.type is E.ACTION_PROPOSED]
    assert {p["legion_call_id"] for p in proposed} == {b["call_id"] for b in issued}
    # the provider's id was renamed to stay unique in the transcript; neither version reached NIA
    assert all(b["call_id"].startswith("lc-") for b in issued)


async def test_provider_call_id_cannot_choose_the_binding() -> None:
    # a model naming its call like a Legion call id changes nothing about what NIA is asked for
    nia = fake()
    forged = "lc-" + "4" * 32
    with serve(nia) as url:
        run = setup(url, [call("read_repo", {"repo": "repo-A"}, id=forged), reply("done")])
        out = await run.h.run(spec())
        await run.close()
    [body] = [b for (e, _, _, b) in nia.requests if e == "issue"]
    assert body["call_id"] != forged
    assert out.status is RunStatus.COMPLETED


async def test_state_change_between_issue_and_status_is_refused_not_reissued() -> None:
    nia = fake()

    def revoke_on_status(endpoint: str, n: int) -> None:
        if endpoint == "status":
            for c in nia.creds.values():
                nia.revoke(c.ref)

    nia.on_request = revoke_on_status
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")])
        out = await run.h.run(spec())
        await run.close()
    [problem] = refused(await run.h.events(out.run_id))
    assert "no longer active at the authority" in problem["problems"]
    assert nia.counts["issue"] == 1 and run.seen == []


async def test_events_reconstruct_the_credential() -> None:
    nia = fake()
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")])
        out = await run.h.run(spec())
        await run.close()
    events = await run.h.events(out.run_id)
    [used] = [e.payload for e in events if e.type is E.CREDENTIAL_RESOLVED]
    [proposed] = [e.payload for e in events if e.type is E.ACTION_PROPOSED]
    [cred] = nia.creds.values()
    assert used["authority"] == "nia" and used["assurance"] == "bound"
    assert used["required"] == "bound"
    assert used["legion_call_id"] == proposed["legion_call_id"] == cred.call_id
    assert used["action_hash"] == proposed["action_hash"] == cred.action_hash
    assert used["external_principal"] == used["evidenced_external_principal"] == "agent:tester"
    assert used["subject"] == "tester" and used["provider"] == "nia-gateway"
    assert used["credential_ref"] == cred.ref and used["resource"] == "repo-A"
    assert used["permissions"] == ["repo.read"] and used["grant_fingerprint"] == cred.grant
    assert used["expires_at"] and used["issued_at"]
    assert cred.secret not in await dump(run.h, out.run_id)


async def test_expired_then_reissued_binds_to_the_same_call() -> None:
    clock = Clock()
    nia = fake(clock)

    def expire_first(endpoint: str, n: int) -> None:
        if endpoint == "status" and n == 1:
            clock.advance(3600)

    nia.on_request = expire_first
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")], clock=clock)
        out = await run.h.run(spec())
        await run.close()
    issued = [b for (e, _, _, b) in nia.requests if e == "issue"]
    assert len(issued) == 2 and issued[0]["call_id"] == issued[1]["call_id"]
    assert out.status is RunStatus.COMPLETED and len(run.seen) == 1


async def test_max_lifetime_above_nia_maximum_is_refused_not_clamped() -> None:
    from legion.kernel.credentials import CredentialMapping

    nia = fake()
    long = CredentialMapping(
        authority="nia",
        provider="nia-gateway",
        permissions={"repo.read": ("repo.read",)},
        max_lifetime_s=3600,
    )
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")], mapping=long)
        out = await run.h.run(spec())
        await run.close()
    assert refused(await run.h.events(out.run_id)) and run.seen == []
    assert not nia.creds


async def test_secret_below_the_model_boundary() -> None:
    nia = fake()
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")], echo=True)
        out = await run.h.run(spec())
        await run.close()
    [cred] = nia.creds.values()
    token = f"{cred.ref}.{cred.secret}"
    assert run.seen == [("read_repo", token)]
    text = await dump(run.h, out.run_id)
    assert cred.secret not in text and ISSUER not in text and VIEWER not in text
    for request in run.h.provider.requests:
        assert cred.secret not in json.dumps(request.model_dump(mode="json"), default=str)


def test_agents_mapping_constant_is_consistent() -> None:
    assert set(AGENTS.values()) == {"agent:tester", "agent:helper", "agent:sibling"}


async def test_status_for_reference_this_process_did_not_issue() -> None:
    nia = fake()
    with serve(nia) as url:
        first, second = authority(url), authority(url)
        a = await first.issue(request())
        with pytest.raises(UnknownCredential):
            await second.status(a.credential_ref)
        await first.aclose()
        await second.aclose()


async def test_clock_skew_on_nia_is_judged_by_legion() -> None:
    clock = Clock()
    nia = fake(clock)
    nia.clock = lambda: clock.now + timedelta(minutes=10)
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")], clock=clock)
        out = await run.h.run(spec())
        await run.close()
    [problem] = refused(await run.h.events(out.run_id))
    assert "issued in the future" in problem["problems"]


# regressions from the Phase 5B.3 review


class _Listener:
    """A TCP listener that records whether anything connected to it."""

    def __init__(self) -> None:
        import socket
        import threading

        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.hits = 0
        self.port = self.sock.getsockname()[1]
        self.stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.hits += 1
            conn.close()

    def close(self) -> None:
        self.stop = True
        self.thread.join(timeout=2)
        self.sock.close()


async def test_proxy_settings_in_the_environment_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.nia_cred_support import identity

    proxy = _Listener()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    nia = fake()
    try:
        with serve(nia) as url:
            auth, ident = authority(url), identity(url)
            issued = await auth.issue(request())
            assert await auth.status(issued.credential_ref) is CredentialStatus.ACTIVE
            await ident.agent_identity("tester")
            await auth.aclose()
            await ident.aclose()
    finally:
        proxy.close()
    assert proxy.hits == 0
    assert [e for (e, _, _, _) in nia.requests] == ["issue", "status", "identity"]


async def test_secret_half_of_the_token_is_scrubbed_on_its_own() -> None:
    from legion.domain.action import EffectClass as Effect

    leaked: list[str] = []

    @tool(
        effect=Effect.READ,
        capabilities=["repo.read"],
        resource=lambda a: a.repo,
        credentials=["github"],
        name="read_repo",
    )
    def half(args: RepoArgs, ctx: ToolContext) -> str:
        """Read, and echo only the secret half of the token."""
        secret = ctx.credentials["github"].reveal().partition(".")[2]
        leaked.append(secret)
        return f"upstream said: bad token {secret}"

    nia = fake()
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")], tools=[half])
        out = await run.h.run(spec())
        await run.close()
    [secret] = leaked
    text = await dump(run.h, out.run_id)
    assert secret not in text and "[redacted]" in text
    for req in run.h.provider.requests:
        assert secret not in json.dumps(req.model_dump(mode="json"), default=str)


async def test_unexpected_issuer_never_matches_the_authority_name() -> None:
    # config issuer is "nia"; NIA's evidence names the Legion authority's own name instead
    from legion.adapters.nia_credentials import NiaCredentialAuthority, NiaCredentialConfig
    from legion.kernel.credentials import assess
    from tests.nia_cred_support import AGENTS, ENV

    nia = fake()
    nia.evidence_mods = {"issuer": "corp-nia"}
    with serve(nia) as url:
        auth = NiaCredentialAuthority(
            "corp-nia",
            NiaCredentialConfig(
                provider="nia",
                endpoint=url,
                credential="env:NIA_ISSUER",
                trusted=True,
                audience="nia-gateway",
            ),
            AGENTS,
            ENV,
        )
        req = request(authority="corp-nia")
        issued = await auth.issue(req)
        await auth.aclose()
    from datetime import UTC, datetime

    result = assess(req, issued.evidence, trusted=True, now=datetime.now(UTC))
    assert result.assurance is None
    assert any("issued by" in p for p in result.problems)


async def test_killed_just_before_dispatch_while_credential_still_active() -> None:
    # NIA's identity says killed on the last look before dispatch, and nothing revoked the
    # credential: Legion's own kill check is what stops the call
    nia = fake()

    def kill_at_final_check(endpoint: str, n: int) -> None:
        if endpoint == "identity" and "status" in nia.counts:
            nia.agents["agent:tester"].state = "killed"

    nia.on_request = kill_at_final_check
    with serve(nia) as url:
        run = setup(url, [READ_A, reply("done")])
        out = await run.h.run(spec())
        await run.close()
    assert run.seen == [] and out.error_code == "killed"
    assert all(c.revoked_at is not None for c in nia.creds.values())


async def test_remembered_references_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import legion.adapters.nia_credentials as adapter

    monkeypatch.setattr(adapter, "MAX_REMEMBERED", 2)
    nia = fake()
    with serve(nia) as url:
        auth = authority(url)
        first = await auth.issue(request())
        await auth.issue(request())
        await auth.issue(request())
        with pytest.raises(UnknownCredential):
            await auth.status(first.credential_ref)
        await auth.aclose()


def test_adapter_timeout_must_be_shorter_than_legions(tmp_path: Any) -> None:
    from legion.config.loader import load_config
    from legion.domain.errors import ConfigError
    from tests.unit.test_nia_optional import _with_nia

    path = _with_nia(tmp_path, timeout_s=10)
    with pytest.raises(ConfigError, match="shorter than credential_policy"):
        load_config(path)
