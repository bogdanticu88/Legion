"""Content-addressed storage for tool outputs too large to keep in an event."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol


class ArtifactStore(Protocol):
    def put(self, data: bytes) -> str: ...

    def get(self, digest: str) -> bytes: ...


class FileArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, digest: str) -> Path:
        if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
            raise ValueError("not a sha256 digest")
        return self.root / digest[:2] / digest

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return digest

    def get(self, digest: str) -> bytes:
        return self._path(digest).read_bytes()


class MemoryArtifactStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self.blobs[digest] = data
        return digest

    def get(self, digest: str) -> bytes:
        return self.blobs[digest]
