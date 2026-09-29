# Only no-auth and API keys for now. Gateways, workload identity and OAuth come later.

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from legion.access.secrets import CredentialResolver, SecretRef


class AuthScheme(StrEnum):
    BEARER = "bearer"
    X_API_KEY = "x-api-key"


class AccessProvider(Protocol):
    kind: str

    async def headers(self, scheme: AuthScheme) -> dict[str, str]: ...

    def describe(self) -> dict[str, str]: ...


class NoAuth:
    kind = "none"

    async def headers(self, scheme: AuthScheme) -> dict[str, str]:
        return {}

    def describe(self) -> dict[str, str]:
        return {"kind": self.kind}


class ApiKeyAccess:
    kind = "api_key"

    def __init__(self, ref: SecretRef, resolver: CredentialResolver) -> None:
        self.ref = ref
        self._resolver = resolver

    async def headers(self, scheme: AuthScheme) -> dict[str, str]:
        secret = await self._resolver.resolve(self.ref)
        if scheme is AuthScheme.X_API_KEY:
            return {"x-api-key": secret.reveal()}
        return {"authorization": f"Bearer {secret.reveal()}"}

    def describe(self) -> dict[str, str]:
        return {"kind": self.kind, "secret": str(self.ref)}
