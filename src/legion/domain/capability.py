"""Capabilities: `name[:resource]`, where the resource is a glob.

`*` matches within one path segment, `**` matches across segments. A grant without a resource
covers every resource for that name. A requirement without a resource is only covered by a grant
without one, so forgetting to extract a resource fails closed.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*(\.\*)?$")


class Capability(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    resource: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not _NAME.match(self.name):
            raise ValueError(f"invalid capability name: {self.name!r}")
        if self.resource is not None and (self.resource == "" or "\x00" in self.resource):
            raise ValueError("capability resource must be a non-empty string")
        return self

    @classmethod
    def parse(cls, text: str) -> Capability:
        name, sep, resource = text.partition(":")
        return cls(name=name, resource=resource if sep else None)

    def __str__(self) -> str:
        return self.name if self.resource is None else f"{self.name}:{self.resource}"

    def covers(self, required: Capability) -> bool:
        if required.name.endswith(".*"):
            # Requirements are concrete. A wildcard requirement is a tool declaration bug.
            return False
        if not _name_matches(self.name, required.name):
            return False
        if self.resource is None:
            return True
        if required.resource is None:
            return False
        return resource_matches(self.resource, required.resource)

    def is_within(self, others: frozenset[Capability]) -> bool:
        """True if some capability in `others` is at least as broad as this one."""
        return any(_at_least_as_broad(other, self) for other in others)


def _name_matches(granted: str, required: str) -> bool:
    if granted.endswith(".*"):
        return required.startswith(granted[:-1])
    return granted == required


def _at_least_as_broad(wide: Capability, narrow: Capability) -> bool:
    if wide.name.endswith(".*"):
        prefix = wide.name[:-1]
        name_ok = narrow.name.startswith(prefix) or narrow.name == wide.name
    else:
        name_ok = wide.name == narrow.name
    if not name_ok:
        return False
    if wide.resource is None:
        return True
    if narrow.resource is None:
        return False
    return glob_contains(wide.resource, narrow.resource)


def glob_contains(wide: str, narrow: str) -> bool:
    """True only when every resource `narrow` matches is also matched by `wide`.

    General glob containment is not attempted. Three cases are decided, anything else is refused,
    which errs towards less authority: identical patterns; a literal that `wide` matches; and
    `wide` being a literal prefix followed by `**` while `narrow`'s literal prefix extends it.
    """
    if wide == narrow:
        return True
    if not _has_glob(narrow):
        return resource_matches(wide, narrow)
    if wide.endswith("**") and not _has_glob(wide[:-2]):
        return _literal_prefix(narrow).startswith(wide[:-2])
    return False


def _has_glob(pattern: str) -> bool:
    return "*" in pattern or "?" in pattern


def _literal_prefix(pattern: str) -> str:
    cut = min((i for i, c in enumerate(pattern) if c in "*?"), default=len(pattern))
    return pattern[:cut]


def resource_matches(pattern: str, resource: str) -> bool:
    if ".." in resource.split("/") or "\x00" in resource:
        return False
    return _compile(pattern).fullmatch(resource) is not None


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out), re.DOTALL)
