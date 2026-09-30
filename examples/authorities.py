# A minimal credential authority, to show the contract. It runs in the Legion process and hands
# out random tokens it keeps in memory, for exactly what Legion asks: the permissions, resource,
# call, Action and Grant in the request, for at most a minute. There's no downstream system that
# accepts these tokens; a real authority would ask one (a secrets manager, a token exchange) for
# a credential scoped the same way.
#
# Legion loads it from legion.yaml:
#
#   credential_authorities:
#     local: {module: authorities.py, trusted: true}
#   credentials:
#     github:
#       authority: local
#       provider: github
#       permissions: {repo.read: [contents:read]}
#
# The file has to define AUTHORITY, an object whose `name` matches the key under
# credential_authorities. tests/conformance/test_credential_authority_contract.py is the
# executable contract every authority, this one included, is held to.

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from legion.access.secrets import Secret
from legion.ports.credentials import (
    CredentialEvidence,
    CredentialRequest,
    CredentialStatus,
    IssuedCredential,
    UnknownCredential,
)

MAX_LIFETIME = timedelta(seconds=60)


@dataclass
class LocalAuthority:
    name: str = "local"
    # credential ref -> when it expires; revoked refs are kept so they stay revoked
    expires: dict[str, datetime] = field(default_factory=dict)
    revoked: set[str] = field(default_factory=set)

    async def issue(self, request: CredentialRequest) -> IssuedCredential:
        now = datetime.now(UTC)
        ref = f"local-{secrets.token_hex(12)}"
        lifetime = min(MAX_LIFETIME, timedelta(seconds=request.max_lifetime_s))
        self.expires[ref] = now + lifetime
        # Evidence says what the credential is for. Legion compares every field with its own
        # request; saying more than was asked makes Legion refuse the credential.
        evidence = CredentialEvidence(
            authority=self.name,
            credential_ref=ref,
            provider=request.provider,
            principal=request.principal,
            subject=request.subject,
            permissions=request.permissions,
            resource=request.resource,
            issued_at=now,
            expires_at=now + lifetime,
            action_hash=request.action_hash,
            call_id=request.call_id,
            grant_fingerprint=request.grant_fingerprint,
            revocation_ref=ref,
        )
        return IssuedCredential(
            secret=Secret(secrets.token_urlsafe(32)), evidence=evidence, credential_ref=ref
        )

    async def status(self, credential_ref: str) -> CredentialStatus:
        if credential_ref in self.revoked:
            return CredentialStatus.REVOKED
        if credential_ref not in self.expires:
            raise UnknownCredential(credential_ref)
        if datetime.now(UTC) >= self.expires[credential_ref]:
            return CredentialStatus.EXPIRED
        return CredentialStatus.ACTIVE

    async def revoke(self, credential_ref: str) -> None:
        if credential_ref not in self.expires:
            raise UnknownCredential(credential_ref)
        self.revoked.add(credential_ref)


AUTHORITY = LocalAuthority()
