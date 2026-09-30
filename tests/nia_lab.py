# A stand-in for NIA's control plane that can be told to misbehave: wrong agents, broken or
# huge or deeply nested JSON, slow answers, dropped connections, error codes, redirects, answers
# full of secrets or terminal escapes, and states that change between two checks. It's a real
# HTTP server on 127.0.0.1, so Legion's adapter talks to it over a socket like it would to NIA.
#
# The response shape copies NIA's GET /agents/{ref}: the agent record with Go field names, plus
# effective_state and kill_sentinel_checked.

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote

TOKEN = "nia-op-token-CANARY-5e1f9a2b7c3d"


@dataclass
class FakeNia:
    # ref -> effective state ("active", "killed", "suspended"); missing means 404
    agents: dict[str, str] = field(default_factory=dict)
    # misbehaviour for every answer, or only from the nth request on
    mode: str | None = None
    mode_from: int = 1
    # called with the request count before answering; tests change state here
    on_request: Callable[[int, str], None] | None = None
    requests: list[tuple[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    port: int = 0

    def kill(self, ref: str) -> None:
        self.agents[ref] = "killed"

    def restore(self, ref: str) -> None:
        self.agents[ref] = "active"

    def answer(self, path: str, auth: str) -> tuple[int, dict[str, str], bytes]:
        self.requests.append((path, auth))
        n = len(self.requests)
        if self.on_request is not None:
            self.on_request(n, path)
        if auth != f"Bearer {TOKEN}":
            return 401, {}, b'{"error": "unauthenticated"}'
        if not path.startswith("/agents/"):
            return 404, {}, b'{"error": "not found"}'
        ref = unquote(path[len("/agents/") :])
        mode = self.mode if n >= self.mode_from else None
        if mode in ("401", "403", "404", "500", "502", "503"):
            return int(mode), {}, json.dumps({"error": f"{mode} {TOKEN}"}).encode()
        if mode == "redirect":
            return 302, {"Location": f"http://127.0.0.1:{self.port}/steal"}, b""
        if ref not in self.agents:
            return 404, {}, b'{"error": "agent not registered"}'
        body: Any = {
            "Ref": ref,
            "DisplayName": ref,
            "Owner": "team",
            "BusinessUnit": "",
            "Assurance": 1,
            "State": self.agents[ref],
            "Purpose": "",
            "RegisteredAt": "2026-09-30T10:00:00Z",
            "KillIncident": "",
            "KilledAt": None,
            "KilledBy": "",
            "effective_state": self.agents[ref],
            "kill_sentinel_checked": True,
            **self.extra,
        }
        if mode == "wrong_agent":
            body["Ref"] = "agent:someone-else"
        elif mode == "unchecked":
            body["kill_sentinel_checked"] = False
        elif mode == "odd_state":
            body["effective_state"] = "sort-of-active"
        elif mode == "wrong_types":
            body["effective_state"] = 1
            body["kill_sentinel_checked"] = "true"
        elif mode == "not_object":
            body = ["active"]
        elif mode == "secrets":
            body["KillIncident"] = TOKEN + "\x1b[2J\x1b]0;owned\x07"
            body["Owner"] = "\u202e" + TOKEN
        elif mode == "escape_ref":
            body["Ref"] = ref + "\x1b[31m"
        raw = json.dumps(body).encode()
        if mode == "malformed":
            raw = raw[: len(raw) // 2]
        elif mode == "nan":
            raw = raw.replace(b'"Assurance": 1', b'"Assurance": NaN')
        elif mode == "huge":
            raw = raw[:-1] + b', "padding": "' + b"x" * 200_000 + b'"}'
        elif mode == "deep":
            # deep enough to break a recursive parser, small enough to pass the size check
            raw = b"[" * 20_000 + b"]" * 20_000
        return 200, {"Content-Type": "application/json"}, raw


class _Handler(BaseHTTPRequestHandler):
    nia: FakeNia

    def do_GET(self) -> None:
        nia = self.server.nia  # type: ignore[attr-defined]
        n = len(nia.requests) + 1
        mode = nia.mode if n >= nia.mode_from else None
        if mode == "slow":
            nia.requests.append((self.path, self.headers.get("Authorization", "")))
            time.sleep(3)
            return
        if mode == "drop":
            nia.requests.append((self.path, self.headers.get("Authorization", "")))
            self.close_connection = True
            self.connection.shutdown(2)
            return
        status, headers, body = nia.answer(self.path, self.headers.get("Authorization", ""))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextmanager
def serve(nia: FakeNia) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.nia = nia  # type: ignore[attr-defined]
    server.daemon_threads = True
    nia.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{nia.port}"
    finally:
        server.shutdown()
        server.server_close()
