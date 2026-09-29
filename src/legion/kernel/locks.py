from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import IO, Protocol

from legion.domain.errors import RunLocked

_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class RunLocks(Protocol):
    def hold(self, run_id: str) -> AbstractContextManager[None]: ...


class InProcessRunLocks:
    """For the in-memory store: one holder per run inside this process."""

    def __init__(self) -> None:
        self._held: set[str] = set()

    @contextmanager
    def hold(self, run_id: str) -> Iterator[None]:
        if run_id in self._held:
            raise RunLocked(f"run {run_id} is already active")
        self._held.add(run_id)
        try:
            yield
        finally:
            self._held.discard(run_id)


class FileRunLocks:
    # An OS lock on a file per run. If the process dies the OS drops the lock, so a crashed run
    # can be resumed, but two live processes can never drive the same run and run its tools
    # twice. Single machine only; that's all Legion supports today.

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @contextmanager
    def hold(self, run_id: str) -> Iterator[None]:
        if not _RUN_ID.match(run_id):
            raise RunLocked(f"not a valid run id: {run_id!r}")
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = open(self.directory / f"{run_id}.lock", "a+b")  # noqa: SIM115
        try:
            if not _try_lock(handle):
                raise RunLocked(f"run {run_id} is active in another process")
            try:
                yield
            finally:
                _unlock(handle)
        finally:
            handle.close()


if sys.platform == "win32":  # pragma: no cover - Windows only
    import msvcrt

    def _try_lock(handle: IO[bytes]) -> bool:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: IO[bytes]) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
