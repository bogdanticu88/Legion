"""Canonical JSON and hashing shared by events and actions.

Same form as MIA's audit chain: sorted keys, compact separators, UTC ISO timestamps. Floats are
allowed because tool arguments contain them, but Python's float repr is the only guarantee of
stability here, so a verifier in another language should compare hashes, not re-serialize.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

GENESIS_HASH = "0" * 64


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime in canonical JSON")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, frozenset | set):
        return sorted(value, key=lambda item: canonical_json(item))
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"not canonicalizable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_default,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def chain_hash(prev_hash: str, body: dict[str, Any]) -> str:
    return sha256_hex(prev_hash + canonical_json(body))
