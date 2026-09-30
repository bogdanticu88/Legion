# A raw TCP "NIA" for attack tests: each connection's request is read and handed to a function
# that writes whatever bytes it likes back (or sleeps, or trickles, or hangs up).

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

Handler = Callable[[bytes, socket.socket], None]


def read_request(conn: socket.socket) -> bytes:
    conn.settimeout(5)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


@contextmanager
def raw_server(
    handler: Handler, family: int = socket.AF_INET, host: str = "127.0.0.1", port: int = 0
) -> Iterator[tuple[int, list[bytes]]]:
    requests: list[bytes] = []
    srv = socket.socket(family, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(16)
    stop = threading.Event()

    def serve_one(conn: socket.socket) -> None:
        try:
            req = read_request(conn)
            requests.append(req)
            handler(req, conn)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def loop() -> None:
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=serve_one, args=(conn,), daemon=True).start()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        yield srv.getsockname()[1], requests
    finally:
        stop.set()
        t.join(2)
        srv.close()


def http_response(status: int, body: bytes, headers: dict[str, str] | None = None) -> bytes:
    lines = [f"HTTP/1.1 {status} X"]
    hdrs = {"Content-Length": str(len(body)), "Connection": "close", **(headers or {})}
    lines += [f"{k}: {v}" for k, v in hdrs.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def fixed(status: int, body: bytes, headers: dict[str, str] | None = None) -> Handler:
    def h(req: bytes, conn: socket.socket) -> None:
        conn.sendall(http_response(status, body, headers))

    return h


def record(ref: str, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "Ref": ref,
        "State": "active",
        "effective_state": "active",
        "kill_sentinel_checked": True,
    }
    body.update(over)
    return body
