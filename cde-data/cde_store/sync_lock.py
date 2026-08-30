from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt


def _acquire(stream: TextIO) -> None:
    if fcntl is not None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write("\0")
        stream.flush()
    stream.seek(0)
    try:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        raise BlockingIOError from exc


def _release(stream: TextIO) -> None:
    if fcntl is not None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return
    stream.seek(0)
    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


class SyncAlreadyRunning(RuntimeError):
    """Raised when another scheduled CDE writer owns the process lock."""


class ExclusiveSyncLock:
    """Non-blocking OS lock shared by daily, weekly, and monthly writers."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._stream: TextIO | None = None

    def __enter__(self) -> "ExclusiveSyncLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="utf-8")
        try:
            _acquire(stream)
        except BlockingIOError as exc:
            owner = "another process"
            try:
                stream.seek(0)
                owner = stream.read().strip() or owner
            except OSError:
                pass
            finally:
                stream.close()
            raise SyncAlreadyRunning(f"CDE sync is already running: {owner}") from exc
        stream.seek(0)
        stream.truncate()
        stream.write(
            f"pid={os.getpid()} started_at="
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        )
        stream.flush()
        self._stream = stream
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        if self._stream is None:
            return
        _release(self._stream)
        self._stream.close()
        self._stream = None
