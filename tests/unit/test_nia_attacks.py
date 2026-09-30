# Attacks on NiaIdentityPort itself over raw sockets, from the Phase 5B.1 review: wrong agents,
# odd bodies and statuses, retries, timeouts, token handling, URLs. test_observe_* and
# test_known_limitation_* pin behaviour that isn't a vulnerability under ADR 0020 but should
# stay visible.

from __future__ import annotations

import gzip
import json
import logging
import socket
import time
import traceback
from typing import Any

import pytest

from legion.access.secrets import EnvResolver
from legion.adapters.nia import NiaIdentityConfig, NiaIdentityPort
from legion.domain.errors import IdentityUnavailable, LegionError
from legion.ports.identity import AgentIdentity, KillState
from tests.nia_raw import fixed, http_response, raw_server, record

TOKEN = "nia-op-token-CANARY-ATTACK-77aa"
REF = "agent:a"


def mk(endpoint: str, token: str = TOKEN, **kw: Any) -> NiaIdentityPort:
    cfg = NiaIdentityConfig(
        provider="nia",
        endpoint=endpoint,
        credential="env:T",
        timeout_s=kw.pop("timeout_s", 1.0),
        attempts=kw.pop("attempts", 1),
        agents=kw.pop("agents", {"a": REF}),
        **kw,
    )
    return NiaIdentityPort(cfg, EnvResolver({"T": token}))


async def state_of(port: NiaIdentityPort, name: str = "a") -> KillState:
    try:
        return await port.kill_state(AgentIdentity(agent_ref=name, source="nia", external_id=REF))
    finally:
        await port.aclose()


def body(**over: Any) -> bytes:
    return json.dumps(record(REF, **over)).encode()


# ---- response content --------------------------------------------------------------------


async def test_baseline_active() -> None:
    with raw_server(fixed(200, body())) as (port, _):
        assert await state_of(mk(f"http://127.0.0.1:{port}")) is KillState.ACTIVE


@pytest.mark.parametrize(
    "raw",
    [
        # missing / wrong-typed kill_sentinel_checked
        json.dumps({"Ref": REF, "effective_state": "active"}).encode(),
        body(kill_sentinel_checked=1),
        body(kill_sentinel_checked="true"),
        body(kill_sentinel_checked=None),
        # Ref variants
        body(Ref=REF.upper()),
        body(Ref=REF + " "),
        body(Ref="agent:\u0430"),
        body(Ref=None),
        json.dumps(
            {"ref": REF, "effective_state": "active", "kill_sentinel_checked": True}
        ).encode(),
        # unknown / case-variant states
        body(effective_state="Active"),
        body(effective_state="ACTIVE"),
        body(effective_state="active "),
        body(effective_state=""),
        body(effective_state=None),
        body(effective_state=["active"]),
        # duplicate kill_sentinel_checked, last is false
        (
            b'{"Ref": "agent:a", "effective_state": "active", "kill_sentinel_checked": true,'
            b' "kill_sentinel_checked": false}'
        ),
        b"",
        b"null",
        b'"active"',
        b"[]",
        body() + b" trailing",
        b'{"Ref": "agent:a", "effective_state": "active", "kill_sentinel_checked": true, "x": Infinity}',
        b'{"Ref": "agent:a", "effective_state": "active", "kill_sentinel_checked": true, "x": '
        + b"1" * 5000
        + b"}",
        b'{"Ref": "agent:a", "effective_state": "active", "kill_sentinel_checked": true, "x": "\xff"}',
    ],
)
async def test_bad_bodies_never_allow(raw: bytes) -> None:
    with raw_server(fixed(200, raw)) as (port, _):
        try:
            got = await state_of(mk(f"http://127.0.0.1:{port}"))
        except LegionError:
            return
    pytest.fail(f"accepted -> {got}")


async def test_huge_float_is_refused() -> None:
    # 1e99999 parses to inf; no non-finite number is accepted anywhere in the answer
    raw = b'{"Ref": "agent:a", "effective_state": "active", "kill_sentinel_checked": true, "x": 1e99999}'
    with (
        raw_server(fixed(200, raw)) as (port, _),
        pytest.raises(IdentityUnavailable, match="isn't valid JSON"),
    ):
        await state_of(mk(f"http://127.0.0.1:{port}"))


async def test_observe_invalid_utf8_in_unused_field_rejected() -> None:
    raw = b'{"Ref": "agent:a", "effective_state": "active", "kill_sentinel_checked": true, "x": "\xff"}'
    with raw_server(fixed(200, raw)) as (port, _), pytest.raises(IdentityUnavailable):
        await state_of(mk(f"http://127.0.0.1:{port}"))


async def test_observe_duplicate_effective_state_last_wins() -> None:
    # first key says killed, last says active: Python (like Go) keeps the last
    raw = (
        b'{"Ref": "agent:a", "effective_state": "killed", "kill_sentinel_checked": true,'
        b' "effective_state": "active"}'
    )
    with raw_server(fixed(200, raw)) as (port, _):
        assert await state_of(mk(f"http://127.0.0.1:{port}")) is KillState.ACTIVE


async def test_observe_duplicate_ref_last_wins() -> None:
    raw = (
        b'{"Ref": "agent:other", "effective_state": "active", "kill_sentinel_checked": true,'
        b' "Ref": "agent:a"}'
    )
    with raw_server(fixed(200, raw)) as (port, _):
        assert await state_of(mk(f"http://127.0.0.1:{port}")) is KillState.ACTIVE


async def test_observe_registry_state_killed_but_effective_active_is_active() -> None:
    # State is ignored entirely; real NIA derives effective_state from State (only ever
    # forcing it to killed), so it can't send this combination
    with raw_server(fixed(200, body(State="killed", KillIncident="x"))) as (port, _):
        assert await state_of(mk(f"http://127.0.0.1:{port}")) is KillState.ACTIVE


async def test_suspended_is_killed() -> None:
    with raw_server(fixed(200, body(effective_state="suspended"))) as (port, _):
        assert await state_of(mk(f"http://127.0.0.1:{port}")) is KillState.KILLED


# ---- status codes, redirects -------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        201,
        202,
        203,
        204,
        206,
        226,
        299,
        300,
        301,
        302,
        303,
        304,
        307,
        308,
        400,
        405,
        410,
        418,
        429,
        500,
        501,
        502,
        503,
        504,
        599,
    ],
)
async def test_every_non_200_fails_closed(status: int) -> None:
    raw = body()  # a perfectly good active record, with the wrong status
    with (
        raw_server(
            fixed(
                status, b"" if status in (204, 304) else raw, {"Location": "http://127.0.0.1:1/x"}
            )
        ) as (port, reqs),
        pytest.raises(LegionError),
    ):
        await state_of(mk(f"http://127.0.0.1:{port}", attempts=2))
    assert len(reqs) <= 2


async def test_100_continue_then_200_is_just_http() -> None:
    def h(req: bytes, conn: socket.socket) -> None:
        conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n" + http_response(200, body()))

    with raw_server(h) as (port, _):
        assert await state_of(mk(f"http://127.0.0.1:{port}")) is KillState.ACTIVE


async def test_redirect_to_other_listener_carries_no_token() -> None:
    with raw_server(fixed(200, body())) as (other, other_reqs):
        for status in (301, 302, 303, 307, 308):
            with (
                raw_server(
                    fixed(status, b"", {"Location": f"http://127.0.0.1:{other}/agents/agent%3Aa"})
                ) as (port, _),
                pytest.raises(IdentityUnavailable),
            ):
                await state_of(mk(f"http://127.0.0.1:{port}"))
    assert other_reqs == []


# ---- retries -----------------------------------------------------------------------------


async def test_retry_cannot_launder_a_bad_answer() -> None:
    answers = [http_response(503, b""), http_response(200, body(Ref="agent:b"))]

    def h(req: bytes, conn: socket.socket) -> None:
        conn.sendall(answers.pop(0))

    with raw_server(h) as (port, _), pytest.raises(IdentityUnavailable, match="other agent"):
        await state_of(mk(f"http://127.0.0.1:{port}", attempts=2))


async def test_killed_is_final_no_retry() -> None:
    answers = [http_response(200, body(effective_state="killed")), http_response(200, body())]

    def h(req: bytes, conn: socket.socket) -> None:
        conn.sendall(answers.pop(0))

    with raw_server(h) as (port, reqs):
        assert await state_of(mk(f"http://127.0.0.1:{port}", attempts=3)) is KillState.KILLED
    assert len(reqs) == 1


async def test_observe_truncated_killed_answer_is_retried_and_later_active_wins() -> None:
    # first answer says killed but the connection drops mid-body (Content-Length too big): that's
    # a transport error, so it's asked again, and a later "active" is used. A fresh answer, not a
    # laundered one, but a killed record that never finished arriving does not stick.
    killed = body(effective_state="killed")
    answers = [
        b"HTTP/1.1 200 OK\r\nContent-Length: 9999\r\n\r\n" + killed,
        http_response(200, body()),
    ]

    def h(req: bytes, conn: socket.socket) -> None:
        conn.sendall(answers.pop(0))

    with raw_server(h) as (port, reqs):
        assert await state_of(mk(f"http://127.0.0.1:{port}", attempts=2)) is KillState.ACTIVE
    assert len(reqs) == 2


# ---- time ---------------------------------------------------------------------------------


def trickle_body(req: bytes, conn: socket.socket) -> None:
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
    for _ in range(200):
        conn.sendall(b" ")
        time.sleep(0.1)


def trickle_headers(req: bytes, conn: socket.socket) -> None:
    conn.sendall(b"HTTP/1.1 200 OK\r\n")
    for _ in range(200):
        conn.sendall(b"X")
        time.sleep(0.1)


def silent(req: bytes, conn: socket.socket) -> None:
    time.sleep(10)


@pytest.mark.parametrize("handler", [trickle_body, trickle_headers, silent])
@pytest.mark.parametrize("attempts", [1, 3])
async def test_timeout_bounds_each_attempt(handler: Any, attempts: int) -> None:
    with raw_server(handler) as (port, _):
        p = mk(f"http://127.0.0.1:{port}", timeout_s=0.5, attempts=attempts)
        start = time.monotonic()
        with pytest.raises(IdentityUnavailable):
            await state_of(p)
        took = time.monotonic() - start
    # per attempt 0.5 s plus the 0.2*n backoff; the whole lookup is NOT bounded by timeout_s
    bound = attempts * 0.5 + sum(0.2 * i for i in range(attempts)) + 0.5
    assert took < bound, took
    if attempts == 3:
        assert took > 1.5  # i.e. ~3x timeout_s: timeout_s is per attempt, not per lookup


async def test_compressed_answer_is_refused_without_inflating_it() -> None:
    import tracemalloc

    bomb = gzip.compress(b"{" + b" " * (60 * 1024 * 1024) + b"}", compresslevel=9)
    assert len(bomb) < 64 * 1024

    with raw_server(fixed(200, bomb, {"Content-Encoding": "gzip"})) as (port, _):
        tracemalloc.start()
        with pytest.raises(IdentityUnavailable, match="compressed"):
            await state_of(mk(f"http://127.0.0.1:{port}", timeout_s=10))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    # nowhere near the 60 MB it would have become
    assert peak < 5_000_000, peak


# ---- token handling ----------------------------------------------------------------------


def echo_auth(status: int, in_header: bool = False) -> Any:
    def h(req: bytes, conn: socket.socket) -> None:
        auth = [h for h in req.split(b"\r\n") if h.lower().startswith(b"authorization")]
        echoed = b" ".join(auth)
        payload = b'{"error": "' + echoed + b'", "Ref": "' + echoed + b'"}'
        headers = {"X-Echo": echoed.decode()} if in_header else {}
        conn.sendall(http_response(status, payload, headers))

    return h


@pytest.mark.parametrize("status", [200, 302, 401, 404, 500, 503])
async def test_token_not_in_exceptions_tracebacks_or_debug_logs(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore", "asyncio", ""):
        logging.getLogger(name).setLevel(logging.DEBUG)
    with raw_server(echo_auth(status)) as (port, reqs), pytest.raises(LegionError) as caught:
        await state_of(mk(f"http://127.0.0.1:{port}", attempts=2))
    assert any(TOKEN.encode() in r for r in reqs)  # it was sent
    exc = caught.value
    text = "".join(traceback.format_exception(exc)) + repr(exc) + str(exc)
    text += repr(exc.__cause__) + repr(exc.__context__)
    assert TOKEN not in text
    assert TOKEN not in caplog.text, [
        r.getMessage() for r in caplog.records if TOKEN in r.getMessage()
    ]


async def test_token_is_not_in_the_tracebacks_frame_locals() -> None:
    # Something that renders locals (Typer's pretty exceptions with show_locals, Sentry,
    # pytest --showlocals) mustn't find the token in any frame an error passes through.
    with raw_server(fixed(500, b"")) as (port, _):
        try:
            await state_of(mk(f"http://127.0.0.1:{port}"))
        except LegionError as exc:
            tb = exc.__traceback__
            found = False
            while tb is not None:
                # Legion's frames only: this test's own locals hold the server's copy of the
                # request, which of course has the header in it
                own = "/legion/" in tb.tb_frame.f_code.co_filename.replace("\\", "/")
                if own and "/tests/" not in tb.tb_frame.f_code.co_filename:
                    found = found or TOKEN in repr(tb.tb_frame.f_locals)
                tb = tb.tb_next
            assert not found


@pytest.mark.parametrize(
    "bad_token",
    ["abc\r\nX-Injected: 1", "abc\nX-Injected: 1", "abc\x00def", "t\u00e9st-\u2603"],
)
async def test_token_header_injection_refused(bad_token: str) -> None:
    with raw_server(fixed(200, body())) as (port, reqs), pytest.raises(LegionError) as caught:
        await state_of(mk(f"http://127.0.0.1:{port}", token=bad_token, attempts=2))
    assert not any(b"X-Injected" in r for r in reqs)
    assert bad_token not in str(caught.value)


# ---- URLs ---------------------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["..", "."])
def test_dot_refs_are_refused(ref: str) -> None:
    # a URL library treats "." and ".." as path segments to resolve, so the request would leave
    # /agents/{ref} ("." -> the agent list, ".." -> the parent of /agents)
    with pytest.raises(ValueError, match="refs are"):
        mk("http://127.0.0.1:1/api", agents={"a": ref})


async def state_of_ref(port: NiaIdentityPort, ref: str) -> KillState:
    try:
        return await port.kill_state(AgentIdentity(agent_ref="a", source="nia", external_id=ref))
    finally:
        await port.aclose()


@pytest.mark.parametrize("suffix", ["?x=1", "/?x=1", "#frag", "/p?q#f"])
async def test_endpoint_query_or_fragment_is_refused(suffix: str) -> None:
    # config accepts an endpoint with a query/fragment; with a query the ref ends up in the
    # query string of a request for "/" (fails closed, but the config should have been refused)
    with raw_server(fixed(404, b"")) as (port, reqs):
        try:
            p = mk(f"http://127.0.0.1:{port}{suffix}")
        except ValueError:
            return
        with pytest.raises(LegionError):
            await state_of(p)
    target = reqs[0].split(b" ")[1]
    assert target.split(b"?")[0].endswith(b"/agents/agent%3Aa"), target


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:80:evil.com", "http://[::1]:80evil"])
def test_bad_port_is_refused_by_the_config(endpoint: str) -> None:
    with pytest.raises(ValueError, match="port"):
        NiaIdentityConfig(provider="nia", endpoint=endpoint, credential="env:T", agents={"a": REF})


def test_localhost_by_name_is_refused() -> None:
    # "localhost" resolves to 127.0.0.1 and ::1; with NIA on one, anything listening on the
    # other would get the token and give the answer. Only literal loopback addresses are allowed.
    with pytest.raises(ValueError, match="needs https"):
        mk("http://localhost:8080")
    mk("http://127.0.0.1:8080")
    mk("http://[::1]:8080")


async def test_literal_loopback_goes_where_it_says() -> None:
    # NIA on 127.0.0.1:P, a squatter on [::1]:P: an endpoint naming 127.0.0.1 reaches NIA only
    legit_hits: list[bytes] = []

    def legit(req: bytes, conn: socket.socket) -> None:
        legit_hits.append(req)
        conn.sendall(http_response(200, body(effective_state="killed")))

    with (
        raw_server(legit) as (port, _),
        raw_server(fixed(200, body()), family=socket.AF_INET6, host="::1", port=port) as (
            _,
            squatter,
        ),
    ):
        state = await state_of(mk(f"http://127.0.0.1:{port}"))
    stolen = any(TOKEN.encode() in r for r in squatter)
    assert not stolen and state is KillState.KILLED, (
        f"squatter got token={stolen}, legit got {len(legit_hits)} requests, state={state}"
    )


async def test_known_limitation_httpcore_debug_logs_response_headers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The outgoing Authorization header is never logged, but httpcore's DEBUG trace logs every
    # response header; if NIA or a proxy in front of it reflects the token in a header, it lands
    # in Legion's logs at DEBUG
    caplog.set_level(logging.DEBUG)
    with raw_server(echo_auth(500, in_header=True)) as (port, _), pytest.raises(LegionError):
        await state_of(mk(f"http://127.0.0.1:{port}"))
    assert TOKEN in caplog.text


async def test_observe_utf8_bom_accepted() -> None:
    with raw_server(fixed(200, b"\xef\xbb\xbf" + body())) as (port, _):
        assert await state_of(mk(f"http://127.0.0.1:{port}")) is KillState.ACTIVE
