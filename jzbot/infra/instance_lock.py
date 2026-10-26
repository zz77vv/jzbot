"""Cross-platform single-process lock for jzbot.

Linux production uses ``fcntl.flock``.  The Windows branch exists so local
imports and test runs are safe without adding a third-party dependency.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

try:  # pragma: no cover - availability depends on the operating system
    import fcntl as _fcntl
except ImportError:  # Windows
    _fcntl = None

try:  # pragma: no cover - availability depends on the operating system
    import msvcrt as _msvcrt
except ImportError:  # Linux
    _msvcrt = None


class AlreadyRunningError(RuntimeError):
    """Raised when another process already owns the instance lock."""


class SingleInstanceLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._file: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._file is not None

    def acquire(self) -> None:
        if self._file is not None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        try:
            if _fcntl is not None:
                _fcntl.flock(
                    lock_file.fileno(),
                    _fcntl.LOCK_EX | _fcntl.LOCK_NB,
                )
            elif _msvcrt is not None:
                # msvcrt.locking requires a byte-sized region to exist.
                if lock_file.seek(0, os.SEEK_END) == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_NBLCK, 1)
            else:  # Defensive: supported production and development OSes are covered.
                raise RuntimeError("No supported operating-system file lock available")
        except (BlockingIOError, OSError) as exc:
            lock_file.close()
            raise AlreadyRunningError(
                "jzbot already running; refusing duplicate instance"
            ) from exc

        self._file = lock_file

    def release(self) -> None:
        lock_file = self._file
        if lock_file is None:
            return

        self._file = None
        try:
            if _fcntl is not None:
                _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
            elif _msvcrt is not None:
                lock_file.seek(0)
                _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_UNLCK, 1)
        finally:
            lock_file.close()

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
