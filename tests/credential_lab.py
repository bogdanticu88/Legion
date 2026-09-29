# A credential authority for tests that can be told to misbehave: issue more than asked, for the
# wrong principal, action or grant, for too long, already expired, with broken or missing
# evidence, time out, crash, revoke behind Legion's back, reuse references, change its mind
# between calls, leak the secret in an error, or put instructions in its answers.
# Security test infrastructure, not an identity service.

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from legion.access.secrets import Secret
from legion.domain.action import EffectClass
from legion.domain.errors import ToolRetryable
from legion.ports.credentials import CredentialRequest, CredentialStatus, IssuedCredential
from legion.tools.base import ToolContext
from legion.tools.native import tool


@dataclass
class LabAuthority:
    name: str = "lab"
    # overrides applied to every evidence it returns
    mods: dict[str, Any] = field(default_factory=dict)
    # "normal", "missing" (no evidence), "malformed"
    evidence: str = "normal"
    # "timeout", "crash", "leak" (raise with the secret in the message), "no_secret"
    fail: str | None = None
    revoke_on_issue: bool = False
    # status() raises instead of answering, or answers something that isn't a status
    fail_active: bool = False
    unknown_active: bool = False
    # runs inside issue(), before it answers (a test can crash the process here)
    on_issue: Callable[[int], None] | None = None
    fixed_ref: str | None = None
    # answer every request with evidence built for the first one it ever saw
    replay_first: bool = False
    # from the nth issue on (1-based), apply these overrides too
    change_from: int | None = None
    change: dict[str, Any] = field(default_factory=dict)
    # the authority's clock; tests share one with Legion to move time
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    issued: list[CredentialRequest] = field(default_factory=list)
    expires: dict[str, datetime] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    revoked: set[str] = field(default_factory=set)

    async def issue(self, request: CredentialRequest) -> IssuedCredential:
        self.issued.append(request)
        n = len(self.issued)
        secret = f"lab-secret-{n:03d}-9f3a7c1e5b2d4f60"
        self.secrets.append(secret)
        if self.on_issue is not None:
            self.on_issue(n)
        if self.fail == "timeout":
            await asyncio.sleep(3600)
        if self.fail == "crash":
            raise RuntimeError("authority fell over")
        if self.fail == "leak":
            raise RuntimeError(f"could not deliver {secret} to the caller")
        if self.fail == "no_secret":
            return IssuedCredential(secret=None, evidence=None)  # type: ignore[arg-type]

        basis = self.issued[0] if self.replay_first else request
        now = self.clock()
        evidence: dict[str, Any] = {
            "authority": self.name,
            "credential_ref": self.fixed_ref or f"cred-{n:03d}",
            "provider": basis.provider,
            "principal": basis.principal,
            "subject": basis.subject,
            "permissions": list(basis.permissions),
            "resource": basis.resource,
            "issued_at": now,
            "expires_at": now + timedelta(seconds=min(basis.max_lifetime_s, 60)),
            "action_hash": basis.action_hash,
            "call_id": basis.call_id,
            "grant_fingerprint": basis.grant_fingerprint,
            "revocation_ref": f"revoke-{n:03d}",
            "verified": True,
        }
        evidence.update(self.mods)
        if self.change_from is not None and n >= self.change_from:
            evidence.update(self.change)
        ref = self.fixed_ref or f"cred-{n:03d}"
        self.expires[ref] = now + timedelta(seconds=min(basis.max_lifetime_s, 60))
        if self.revoke_on_issue:
            self.revoked.add(ref)
        if self.evidence == "missing":
            return IssuedCredential(Secret(secret), None, credential_ref=ref)
        if self.evidence == "malformed":
            malformed = {"scope": "everything", "trust_me": True}
            return IssuedCredential(Secret(secret), malformed, credential_ref=ref)
        top = evidence["credential_ref"] if isinstance(evidence["credential_ref"], str) else ref
        return IssuedCredential(Secret(secret), evidence, credential_ref=top)

    async def status(self, credential_ref: str) -> CredentialStatus:
        if self.fail_active:
            raise ConnectionError("authority unreachable")
        if self.unknown_active:
            return None  # type: ignore[return-value]
        if credential_ref in self.revoked:
            return CredentialStatus.REVOKED
        if credential_ref in self.expires and self.clock() >= self.expires[credential_ref]:
            return CredentialStatus.EXPIRED
        return CredentialStatus.ACTIVE

    async def revoke(self, credential_ref: str) -> None:
        self.revoked.add(credential_ref)


class RepoArgs(BaseModel):
    repo: str
    title: str = ""


def repo_tools(
    seen: list[tuple[str, str]],
    *,
    echo: bool = False,
    fail_first: bool = False,
    on_call: Callable[[int], None] | None = None,
) -> list[Any]:
    """read_repo and create_issue, both needing the `github` credential.

    fail_first makes read_repo fail retryably on its first attempt; on_call runs at the start of
    every read_repo attempt with the attempt number.
    """

    @tool(
        effect=EffectClass.READ,
        capabilities=["repo.read"],
        resource=lambda a: a.repo,
        credentials=["github"],
    )
    def read_repo(args: RepoArgs, ctx: ToolContext) -> str:
        """Read a repository's README."""
        secret = ctx.credentials["github"].reveal()
        seen.append(("read_repo", secret))
        if on_call is not None:
            on_call(len(seen))
        if fail_first and len(seen) == 1:
            raise ToolRetryable("upstream hiccup")
        return f"README of {args.repo}" + (f" (token {secret})" if echo else "")

    @tool(
        effect=EffectClass.WRITE,
        capabilities=["repo.issue.create"],
        resource=lambda a: a.repo,
        credentials=["github"],
    )
    def create_issue(args: RepoArgs, ctx: ToolContext) -> str:
        """Open an issue."""
        secret = ctx.credentials["github"].reveal()
        seen.append(("create_issue", secret))
        if args.title == "raise":
            raise RuntimeError(f"upstream rejected token {secret}")
        return f"opened issue in {args.repo}"

    return [read_repo, create_issue]


def effect_tool(
    effect: EffectClass,
    seen: list[str],
    *,
    fail_first: bool = False,
    on_call: Callable[[int], None] | None = None,
) -> Any:
    """touch_repo with the given effect class, needing the `github` credential."""

    @tool(
        effect=effect,
        capabilities=["repo.read"],
        resource=lambda a: a.repo,
        credentials=["github"],
        name="touch_repo",
    )
    def touch_repo(args: RepoArgs, ctx: ToolContext) -> str:
        """Do something to a repository."""
        seen.append(ctx.credentials["github"].reveal())
        if on_call is not None:
            on_call(len(seen))
        if fail_first and len(seen) == 1:
            raise ToolRetryable("upstream hiccup")
        return "done"

    return touch_repo
