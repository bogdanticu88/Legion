# Only no-auth and API keys for now. Gateways, workload identity and OAuth come later.

from __future__ import annotations

from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

from legion.access.secrets import CredentialResolver, SecretRef
from legion.domain.errors import CredentialUnavailable


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


# Literal addresses only. The name "localhost" resolves to 127.0.0.1 and ::1, and whatever listens
# on the one the client picks gets the credential, which needn't be the service you meant.
_LOOPBACK = ("127.0.0.1", "::1")


def check_endpoint(url: str, *, carries_credentials: bool) -> None:
    """Raise ValueError for a URL Legion shouldn't send requests (or credentials) to."""
    # The URL isn't repeated in errors: it may have a password or token in it.
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("URLs can't carry a username or password; use a secret reference")
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("the URL has to be http or https, with a host")
    if carries_credentials and parts.scheme.lower() != "https" and parts.hostname not in _LOOPBACK:
        raise ValueError(
            f"{parts.hostname} gets credentials, so it needs https (plain http only to 127.0.0.1 "
            "or [::1])"
        )


class ApiKeyAccess:
    kind = "api_key"

    def __init__(
        self, ref: SecretRef, resolver: CredentialResolver, provider: str | None = None
    ) -> None:
        self.ref = ref
        self._resolver = resolver
        # the legion.yaml provider this key belongs to, for a message that says how to fix it
        self._provider = provider

    async def headers(self, scheme: AuthScheme) -> dict[str, str]:
        try:
            secret = await self._resolver.resolve(self.ref)
        except CredentialUnavailable:
            if self._provider is None:
                raise
            raise CredentialUnavailable(
                f"model provider {self._provider!r} needs {self.ref}, which isn't set. Set "
                f"{self.ref.name} in the environment, or bind the agent's model profile to "
                f"another provider in legion.yaml (`legion providers` lists them)"
            ) from None
        if scheme is AuthScheme.X_API_KEY:
            return {"x-api-key": secret.reveal()}
        return {"authorization": f"Bearer {secret.reveal()}"}

    def describe(self) -> dict[str, str]:
        return {"kind": self.kind, "secret": str(self.ref)}
