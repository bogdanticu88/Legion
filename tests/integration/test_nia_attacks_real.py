# Attacks against Legion's NIA adapter with a real nia-api binary, from the Phase 5B.1 review.
# Skipped unless LEGION_TEST_NIA_BIN points at it.

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from legion.access.secrets import EnvResolver
from legion.adapters.nia import NiaIdentityConfig, NiaIdentityPort
from legion.domain.errors import LegionError
from legion.ports.identity import AgentIdentity, KillState

pytestmark = pytest.mark.integration

VIEW = "legion-viewer-token-ATTACK-11"
ADMIN = "admin-token-ATTACK-22"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def nia_proc(tmp: Path, port: int | None = None, **env: str) -> Iterator[str]:
    binary = os.environ.get("LEGION_TEST_NIA_BIN")
    if not binary:
        pytest.skip("set LEGION_TEST_NIA_BIN")
    tokens = tmp / "ops.json"
    tokens.write_text(
        json.dumps(
            [
                {"token": VIEW, "name": "legion", "roles": ["viewer"]},
                {"token": ADMIN, "name": "admin", "roles": ["admin"]},
            ]
        )
    )
    port = port or free_port()
    with (tmp / f"nia-{port}.log").open("a") as log:
        proc = subprocess.Popen(
            [binary],
            env={
                "PATH": os.environ.get("PATH", ""),
                "NIA_API_ADDR": f"127.0.0.1:{port}",
                "NIA_OPERATOR_TOKENS_PATH": str(tokens),
                **env,
            },
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
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
        try:
            yield url
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def admin(url: str, method: str, path: str, body: dict[str, Any]) -> httpx.Response:
    return httpx.request(
        method, f"{url}{path}", json=body, headers={"Authorization": f"Bearer {ADMIN}"}
    )


def register(url: str, ref: str) -> int:
    return admin(
        url, "POST", "/agents", {"ref": ref, "display_name": "x", "owner": "t"}
    ).status_code


def kill(url: str, ref: str) -> int:
    return admin(
        url, "POST", "/policy/kill", {"agent_ref": ref, "incident": "i", "operator": "t"}
    ).status_code


def restore(url: str, ref: str) -> int:
    return admin(url, "POST", "/policy/restore", {"agent_ref": ref, "operator": "t"}).status_code


def mk(url: str, ref: str, token: str = VIEW, **kw: Any) -> NiaIdentityPort:
    cfg = NiaIdentityConfig(
        provider="nia",
        endpoint=url,
        credential="env:T",
        timeout_s=kw.pop("timeout_s", 1.0),
        attempts=kw.pop("attempts", 1),
        agents={"a": ref},
        **kw,
    )
    return NiaIdentityPort(cfg, EnvResolver({"T": token}))


async def check(url: str, ref: str, **kw: Any) -> Any:
    p = mk(url, ref, **kw)
    try:
        return await p.kill_state(AgentIdentity(agent_ref="a", source="nia", external_id=ref))
    except LegionError as exc:
        return f"{type(exc).__name__}: {exc.message}"
    finally:
        await p.aclose()


async def test_lifecycle_active_kill_restore_unknown_down(tmp_path: Path) -> None:
    with nia_proc(tmp_path) as url:
        assert (await check(url, "agent:x")).startswith("IdentityUnknown")
        assert register(url, "agent:x") == 201
        assert await check(url, "agent:x") is KillState.ACTIVE
        assert kill(url, "agent:x") == 200
        assert await check(url, "agent:x") is KillState.KILLED
        assert restore(url, "agent:x") == 200
        assert await check(url, "agent:x") is KillState.ACTIVE
        # wrong token / no permission
        assert "401" in await check(url, "agent:x", token="nope")
    assert "couldn't reach NIA" in await check(url, "agent:x", attempts=2)


async def test_refs_with_special_characters_round_trip(tmp_path: Path) -> None:
    refs = ["agent:x", "a@b:c", "a.b-c_d", "A:B", "x" * 128, "a..", ".a", "-", "@", ":"]
    out = {}
    with nia_proc(tmp_path) as url:
        for ref in refs:
            assert register(url, ref) == 201, ref
            out[ref] = await check(url, ref)
    assert all(v is KillState.ACTIVE for v in out.values()), out


async def test_dot_refs_are_refused_before_nia_is_asked(tmp_path: Path) -> None:
    # NIA accepts "." and ".." as refs, but a URL library resolves them as path segments, so
    # Legion refuses them in its config rather than asking about some other path
    from legion.adapters.nia import NiaIdentityConfig

    for ref in (".", "..", "..."):
        with pytest.raises(ValueError, match="refs are"):
            NiaIdentityConfig(
                provider="nia", endpoint="http://127.0.0.1:1", credential="env:T", agents={"a": ref}
            )


async def test_case_and_encoding_are_exact(tmp_path: Path) -> None:
    with nia_proc(tmp_path) as url:
        register(url, "agent:x")
        assert (await check(url, "Agent:X")).startswith("IdentityUnknown")
        assert (await check(url, "agent:x.")).startswith("IdentityUnknown")
        # NIA decodes the path segment: %2F in a ref registered with "/" is reachable only raw
        register(url, "a/b")
        httpx.get(f"{url}/agents/a%2Fb", headers={"Authorization": f"Bearer {VIEW}"})
        register(url, "a%3Ab")  # literal percent in a ref
        httpx.get(f"{url}/agents/a%253Ab", headers={"Authorization": f"Bearer {VIEW}"})


async def test_kill_before_registration_is_honoured(tmp_path: Path) -> None:
    with nia_proc(tmp_path) as url:
        kill(url, "agent:late")
        register(url, "agent:late")
        assert await check(url, "agent:late") is KillState.KILLED


async def test_observe_nia_restart_forgets_kills(tmp_path: Path) -> None:
    # NIA's default in-memory registry and policy: after a restart the agent is gone (so Legion
    # refuses, identity_unknown); if someone re-registers it, the old kill is gone too
    port = free_port()
    with nia_proc(tmp_path, port=port) as url:
        register(url, "agent:x")
        kill(url, "agent:x")
        assert await check(url, "agent:x") is KillState.KILLED
    with nia_proc(tmp_path, port=port) as url:
        assert (await check(url, "agent:x")).startswith("IdentityUnknown")
        register(url, "agent:x")
        assert await check(url, "agent:x") is KillState.ACTIVE


async def test_unreachable_policy_backend_means_unchecked_and_refused(tmp_path: Path) -> None:
    import base64

    key = base64.b64encode(b"k" * 32).decode()
    with nia_proc(
        tmp_path, NIA_TESSERA_BASE_URL="http://127.0.0.1:1", NIA_TESSERA_JWT_SIGNING_KEY=key
    ) as url:
        register(url, "agent:x")
        httpx.get(f"{url}/agents/agent:x", headers={"Authorization": f"Bearer {VIEW}"})
        got = await check(url, "agent:x", timeout_s=10)
    assert got is not KillState.ACTIVE


async def test_token_not_logged_against_real_nia(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with nia_proc(tmp_path) as url:
        register(url, "agent:x")
        await check(url, "agent:x")
        await check(url, "agent:nope")
        await check(url, "agent:x", token=VIEW + "-wrong")
    assert VIEW not in caplog.text
    "".join(p.read_text() for p in tmp_path.glob("nia-*.log"))


async def test_literal_loopback_reaches_real_nia_not_an_ipv6_squatter(tmp_path: Path) -> None:
    # Real nia-api on 127.0.0.1:P with the agent killed; a local process binds [::1]:P and says
    # "active". "localhost" isn't accepted any more, and 127.0.0.1 reaches NIA.
    from tests.nia_raw import fixed, raw_server, record

    port = free_port()
    with nia_proc(tmp_path, port=port) as url:
        register(url, "agent:x")
        kill(url, "agent:x")
        fake = json.dumps(record("agent:x")).encode()
        with raw_server(fixed(200, fake), family=socket.AF_INET6, host="::1", port=port) as (
            _,
            squatter,
        ):
            got = await check(f"http://127.0.0.1:{port}", "agent:x")
    assert not any(VIEW.encode() in r for r in squatter)
    assert got is KillState.KILLED


@pytest.mark.xfail(
    strict=True,
    reason="known limitation (ADR 0020): with NIA bound to a wildcard address, another local "
    "process can take the loopback address on the same port on some systems (seen on macOS) "
    "and receive the token. Plain http to loopback trusts every local process; use https or "
    "bind NIA to a specific loopback address",
)
@pytest.mark.parametrize(
    "squat_host,family,endpoint_host",
    [
        ("127.0.0.1", socket.AF_INET, "127.0.0.1"),
        ("::1", socket.AF_INET6, "[::1]"),
    ],
)
async def test_known_limitation_wildcard_nia_can_be_squatted_on_loopback(
    tmp_path: Path, squat_host: str, family: int, endpoint_host: str
) -> None:
    # NIA's default NIA_API_ADDR is ":8080" (wildcard). On macOS/BSD another local user can
    # still bind the specific loopback address on the same port (SO_REUSEADDR) and receives the
    # connections for it. Platform-dependent; this records what happens here.
    from tests.nia_raw import fixed, raw_server, record

    binary = os.environ.get("LEGION_TEST_NIA_BIN")
    if not binary:
        pytest.skip("set LEGION_TEST_NIA_BIN")
    port = free_port()
    tokens = tmp_path / "ops.json"
    tokens.write_text(
        json.dumps(
            [
                {"token": VIEW, "name": "l", "roles": ["viewer"]},
                {"token": ADMIN, "name": "a", "roles": ["admin"]},
            ]
        )
    )
    proc = subprocess.Popen(
        [binary],
        env={
            "PATH": os.environ.get("PATH", ""),
            "NIA_API_ADDR": f":{port}",
            "NIA_OPERATOR_TOKENS_PATH": str(tokens),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(f"{url}/healthz", timeout=0.2).status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.05)
        register(url, "agent:x")
        kill(url, "agent:x")
        fake = json.dumps(record("agent:x")).encode()
        with raw_server(fixed(200, fake), family=family, host=squat_host, port=port) as (_, sq):
            got = await check(f"http://{endpoint_host}:{port}", "agent:x")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    stolen = any(VIEW.encode() in r for r in sq)
    assert not stolen and got is KillState.KILLED
