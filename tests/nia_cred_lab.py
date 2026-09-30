# A stand-in for NIA's control plane with both the identity endpoint (GET /agents/{ref}) and the
# scoped credential endpoints, written from NIA's handlers (cmd/api/scoped.go at 40891e3): the
# same request and answer shapes, the same authority rule (every permission a held tool grant,
# the resource a held data grant), the same status order, and kill and restore moving a
# generation. It can be told to misbehave per endpoint. Real HTTP on 127.0.0.1.
#
# Security test infrastructure, not an identity service.

from __future__ import annotations

import gzip
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote

VIEWER = "nia-viewer-CANARY-7d1e3a9b5c"
ISSUER = "nia-issuer-CANARY-2f8c6e0a4d"
EVIDENCE_FORMAT = "nia.scoped-credential.evidence/v1"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
_HASH = re.compile(r"[0-9a-f]{64}")
_CALL = re.compile(r"[A-Za-z0-9._:@+=/_-]{1,128}")
_ISSUE_FIELDS = {
    "permissions",
    "resource",
    "audience",
    "action_hash",
    "call_id",
    "grant_fingerprint",
    "ttl_seconds",
}


@dataclass
class Agent:
    state: str = "active"
    tools: set[str] = field(default_factory=set)
    data: set[str] = field(default_factory=set)
    generation: int = 0


@dataclass
class Scoped:
    ref: str
    secret: str
    principal: str
    permissions: list[str]
    resource: str
    audience: str
    action_hash: str
    call_id: str
    grant: str
    issued_at: datetime
    expires_at: datetime
    generation: int
    revoked_at: datetime | None = None


def _ts(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class FakeNiaCredentials:
    agents: dict[str, Agent] = field(default_factory=dict)
    creds: dict[str, Scoped] = field(default_factory=dict)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    max_ttl: int = 900
    # misbehaviour, per endpoint: "identity", "issue", "status", "revoke"
    modes: dict[str, str] = field(default_factory=dict)
    # from the nth request to that endpoint on (1-based)
    mode_from: dict[str, int] = field(default_factory=dict)
    # overrides for the issuance evidence, the issuance answer, and the status answer
    evidence_mods: dict[str, Any] = field(default_factory=dict)
    answer_mods: dict[str, Any] = field(default_factory=dict)
    status_mods: dict[str, Any] = field(default_factory=dict)
    # called with (endpoint, count) before answering; tests change state here
    on_request: Callable[[str, int], None] | None = None
    requests: list[tuple[str, str, str, dict[str, Any] | None]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    port: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    # operator side, as an admin would do it through NIA's API

    def register(self, ref: str, tools: tuple[str, ...] = (), data: tuple[str, ...] = ()) -> None:
        self.agents[ref] = Agent(tools=set(tools), data=set(data))

    def grant(self, ref: str, tools: tuple[str, ...] = (), data: tuple[str, ...] = ()) -> None:
        self.agents[ref].tools |= set(tools)
        self.agents[ref].data |= set(data)

    def ungrant(self, ref: str, tools: tuple[str, ...] = (), data: tuple[str, ...] = ()) -> None:
        self.agents[ref].tools -= set(tools)
        self.agents[ref].data -= set(data)

    def kill(self, ref: str) -> None:
        a = self.agents[ref]
        a.state = "killed"
        a.tools, a.data = set(), set()
        self._move(ref)

    def restore(self, ref: str) -> None:
        self._move(ref)
        self.agents[ref].state = "active"

    def revoke(self, cred_ref: str) -> None:
        c = self.creds[cred_ref]
        if c.revoked_at is None:
            c.revoked_at = self.clock()

    def _move(self, ref: str) -> None:
        self.agents[ref].generation += 1
        for c in self.creds.values():
            if c.principal == ref and c.revoked_at is None:
                c.revoked_at = self.clock()

    def status_of(self, c: Scoped) -> tuple[str, str]:
        a = self.agents.get(c.principal)
        if c.revoked_at is not None:
            return "revoked", "revoked"
        if a is None or c.generation != a.generation:
            return "revoked", "principal was killed or restored after issuance"
        if a.state != "active":
            return "revoked", "principal is killed"
        if not set(c.permissions) <= a.tools or c.resource not in a.data:
            c.revoked_at = self.clock()
            return "revoked", "authority withdrawn"
        if self.clock() >= c.expires_at:
            return "expired", "expired"
        return "active", ""

    def issued_secrets(self) -> list[str]:
        return [c.secret for c in self.creds.values()]

    # the HTTP side

    def _count(self, endpoint: str) -> int:
        self.counts[endpoint] = self.counts.get(endpoint, 0) + 1
        return self.counts[endpoint]

    def mode(self, endpoint: str, n: int) -> str | None:
        m = self.modes.get(endpoint)
        return m if m is not None and n >= self.mode_from.get(endpoint, 1) else None

    def handle(
        self, method: str, path: str, auth: str, body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        parts = [unquote(p) for p in path.split("?")[0].strip("/").split("/")]
        endpoint = _endpoint(method, parts)
        n = self._count(endpoint)
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        self.requests.append((endpoint, path, auth, parsed if isinstance(parsed, dict) else None))
        if self.on_request is not None:
            self.on_request(endpoint, n)
        mode = self.mode(endpoint, n)
        if mode is not None and mode.isdigit():
            code = int(mode)
            return code, {}, json.dumps({"error": f"refused {code} {ISSUER} {VIEWER}"}).encode()
        if mode == "redirect":
            return 302, {"Location": f"http://127.0.0.1:{self.port}/steal"}, b""
        token = auth.removeprefix("Bearer ")
        roles = {VIEWER: {"read"}, ISSUER: {"read", "issue"}}.get(token)
        if roles is None:
            return 401, {}, b'{"error":"invalid operator token"}'
        with self.lock:
            if endpoint == "identity":
                status, answer = self._identity(parts[1], roles)
            elif endpoint == "issue":
                status, answer = self._issue(parts[1], parsed, roles, body)
            elif endpoint == "status":
                status, answer = self._status(parts[1], parts[3], roles)
            elif endpoint == "revoke":
                status, answer = self._revoke(parts[1], parts[3], roles)
            else:
                status, answer = 404, {"error": "not found"}
        return self._shape(mode, status, answer)

    def _shape(
        self, mode: str | None, status: int, answer: Any
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        raw = json.dumps(answer).encode()
        if mode == "malformed":
            raw = raw[: len(raw) // 2]
        elif mode == "not_object":
            raw = b'["active"]'
        elif mode == "nan":
            raw = b'{"status": NaN}'
        elif mode == "huge":
            raw = raw[:-1] + b', "padding": "' + b"x" * 200_000 + b'"}'
        elif mode == "gzip":
            raw = gzip.compress(raw)
            headers["Content-Encoding"] = "gzip"
        elif mode == "empty":
            raw = b""
        elif mode == "secret_in_error":
            status = 500
            leaked = " ".join(self.issued_secrets())
            raw = json.dumps({"error": f"boom {leaked} {ISSUER} {VIEWER}"}).encode()
        return status, headers, raw

    def _identity(self, ref: str, roles: set[str]) -> tuple[int, Any]:
        a = self.agents.get(ref)
        if a is None:
            return 404, {"error": "agent not registered"}
        return 200, {
            "Ref": ref,
            "State": a.state,
            "effective_state": a.state,
            "kill_sentinel_checked": True,
        }

    def _issue(self, ref: str, body: Any, roles: set[str], raw: bytes) -> tuple[int, Any]:
        if "issue" not in roles:
            return 403, {"error": "operator lacks the issue permission"}
        if not isinstance(body, dict) or set(body) - _ISSUE_FIELDS:
            return 400, {"error": "refused: body: unknown field"}
        perms = body.get("permissions")
        ttl = body.get("ttl_seconds", 60)
        checks = [
            isinstance(perms, list)
            and perms
            and all(isinstance(p, str) and _NAME.fullmatch(p) for p in perms),
            isinstance(body.get("resource"), str) and _NAME.fullmatch(body["resource"]),
            isinstance(body.get("audience"), str) and _NAME.fullmatch(body["audience"]),
            isinstance(body.get("action_hash"), str) and _HASH.fullmatch(body["action_hash"]),
            isinstance(body.get("call_id"), str) and _CALL.fullmatch(body["call_id"]),
            isinstance(body.get("grant_fingerprint"), str)
            and _HASH.fullmatch(body["grant_fingerprint"]),
            isinstance(ttl, int) and 1 <= ttl <= self.max_ttl,
        ]
        if not all(checks):
            return 400, {"error": "refused: request is malformed"}
        a = self.agents.get(ref)
        if a is None:
            return 404, {"error": "refused: principal: not registered"}
        if a.state != "active":
            return 403, {"error": "refused: principal: killed"}
        if not set(perms) <= a.tools:
            return 403, {
                "error": "refused: authority: principal does not hold the requested permission"
            }
        if body["resource"] not in a.data:
            return 403, {
                "error": "refused: authority: principal does not hold the requested resource"
            }
        now = self.clock().astimezone(UTC).replace(microsecond=0)
        c = Scoped(
            ref="nia-sc-" + secrets.token_hex(16),
            secret=secrets.token_urlsafe(32),
            principal=ref,
            permissions=sorted(perms),
            resource=body["resource"],
            audience=body["audience"],
            action_hash=body["action_hash"],
            call_id=body["call_id"],
            grant=body["grant_fingerprint"],
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl),
            generation=a.generation,
        )
        self.creds[c.ref] = c
        base = f"/agents/{ref}/scoped-credentials/{c.ref}"
        evidence: dict[str, Any] = {
            "format": EVIDENCE_FORMAT,
            "issuer": "nia",
            "credential_ref": c.ref,
            "principal": ref,
            "audience": c.audience,
            "permissions": c.permissions,
            "resource": c.resource,
            "action_hash": c.action_hash,
            "call_id": c.call_id,
            "grant_fingerprint": c.grant,
            "issued_at": _ts(c.issued_at),
            "expires_at": _ts(c.expires_at),
            "status_endpoint": base,
            "revoke_endpoint": base + "/revoke",
            "semantics": "bearer credential ...",
        }
        evidence.update(self.evidence_mods)
        answer: dict[str, Any] = {
            "credential_ref": c.ref,
            "token": f"{c.ref}.{c.secret}",
            "evidence": evidence,
        }
        answer.update(self.answer_mods)
        return 201, answer

    def _lookup(self, principal: str, cred_ref: str) -> Scoped | None:
        c = self.creds.get(cred_ref)
        return c if c is not None and c.principal == principal else None

    def _status(self, principal: str, cred_ref: str, roles: set[str]) -> tuple[int, Any]:
        if not re.fullmatch(r"nia-sc-[0-9a-f]{32}", cred_ref):
            return 400, {"error": "malformed principal or credential reference"}
        c = self._lookup(principal, cred_ref)
        if c is None:
            return 404, {
                "credential_ref": cred_ref,
                "status": "unknown",
                "checked_at": _ts(self.clock()),
            }
        status, reason = self.status_of(c)
        answer: dict[str, Any] = {
            "credential_ref": c.ref,
            "principal": c.principal,
            "status": status,
            "reason": reason,
            "checked_at": _ts(self.clock()),
            "issued_at": _ts(c.issued_at),
            "expires_at": _ts(c.expires_at),
        }
        answer.update(self.status_mods)
        return 200, answer

    def _revoke(self, principal: str, cred_ref: str, roles: set[str]) -> tuple[int, Any]:
        if "issue" not in roles:
            return 403, {"error": "operator lacks the issue permission"}
        c = self._lookup(principal, cred_ref)
        if c is None:
            return 404, {"credential_ref": cred_ref, "status": "unknown"}
        already = c.revoked_at is not None
        if not already:
            c.revoked_at = self.clock()
        return 200, {
            "credential_ref": c.ref,
            "principal": c.principal,
            "status": "revoked",
            "revoked_at": _ts(c.revoked_at),  # type: ignore[arg-type]
            "revoked_by": "legion",
            "already_revoked": already,
        }


def _endpoint(method: str, parts: list[str]) -> str:
    if len(parts) == 2 and parts[0] == "agents" and method == "GET":
        return "identity"
    if len(parts) == 3 and parts[2] == "scoped-credentials" and method == "POST":
        return "issue"
    if len(parts) == 4 and parts[2] == "scoped-credentials" and method == "GET":
        return "status"
    if len(parts) == 5 and parts[4] == "revoke" and method == "POST":
        return "revoke"
    return "other"


class _Handler(BaseHTTPRequestHandler):
    def _serve(self, method: str) -> None:
        nia: FakeNiaCredentials = self.server.nia  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parts = [unquote(p) for p in self.path.split("?")[0].strip("/").split("/")]
        endpoint = _endpoint(method, parts)
        mode = nia.mode(endpoint, nia.counts.get(endpoint, 0) + 1)
        if mode == "slow":
            nia._count(endpoint)
            time.sleep(3)
            return
        if mode == "drop":
            nia._count(endpoint)
            self.close_connection = True
            self.connection.shutdown(2)
            return
        status, headers, raw = nia.handle(
            method, self.path, self.headers.get("Authorization", ""), body
        )
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextmanager
def serve(nia: FakeNiaCredentials) -> Iterator[str]:
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
