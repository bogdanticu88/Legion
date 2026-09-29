from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from legion.domain.errors import ConfigError, CredentialUnavailable

_REF = re.compile(r"^(env):([A-Za-z_][A-Za-z0-9_]*)$")


class SecretRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scheme: Literal["env"]
    name: str

    @classmethod
    def parse(cls, text: str) -> SecretRef:
        match = _REF.match(text)
        if not match:
            raise ConfigError(f"not a secret reference (expected env:NAME): {text!r}")
        return cls(scheme="env", name=match.group(2))

    def __str__(self) -> str:
        return f"{self.scheme}:{self.name}"


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("empty secret")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(****)"

    __str__ = __repr__

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("secrets cannot be pickled")


class CredentialResolver(Protocol):
    async def resolve(self, ref: SecretRef) -> Secret: ...


class EnvResolver:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    async def resolve(self, ref: SecretRef) -> Secret:
        value = self._environ.get(ref.name)
        if not value:
            raise CredentialUnavailable(f"{ref} is not set")
        return Secret(value)

    def available(self, ref: SecretRef) -> bool:
        return bool(self._environ.get(ref.name))
