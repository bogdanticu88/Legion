# JSON Schema checks that never leave the schema document. Left to itself, jsonschema resolves a
# `$ref` it doesn't know by fetching it, over http or from a file:// path. A schema written by an
# MCP server could then make Legion request an internal URL, or read a local file into a
# validation error that goes back to the model.

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from jsonschema.exceptions import best_match
from jsonschema.validators import validator_for
from referencing import Registry

# nothing to retrieve from: only refs inside the schema itself resolve
_NO_RETRIEVAL: Registry[Any] = Registry()
_REF_KEYS = ("$ref", "$dynamicRef")


def check(schema: dict[str, Any]) -> None:
    """Raise ValueError if the schema is invalid or points outside itself."""
    try:
        validator_for(schema, default=Draft202012Validator).check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"invalid schema: {exc.message}") from exc
    for ref in _refs(schema):
        if not ref.startswith("#"):
            raise ValueError(f"schema refers outside itself: {ref[:80]!r}")


def validate(instance: Any, schema: dict[str, Any]) -> None:
    """Like jsonschema.validate, without retrieving anything. Raises ValidationError."""
    cls = validator_for(schema, default=Draft202012Validator)
    error = best_match(cls(schema, registry=_NO_RETRIEVAL).iter_errors(instance))
    if error is not None:
        raise error


def _refs(node: Any) -> list[str]:
    found: list[str] = []
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key in _REF_KEYS and isinstance(value, str):
                    found.append(value)
                else:
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
    return found


__all__ = ["ValidationError", "check", "validate"]
