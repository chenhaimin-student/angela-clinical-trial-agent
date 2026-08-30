"""Small adapter imported by the Hermes gateway patch.

Failures are intentionally non-fatal: message delivery must never depend on the
optional background compactor.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from session_compactor_store import CoordinationStore


_STORE: CoordinationStore | None = None
_ACTIVE: dict[str, tuple[str | None, int]] = {}
_LOCK = threading.Lock()
_HEARTBEAT_STARTED = False


def _coordination_path() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    return Path(os.environ.get("ANGELA_COMPACTOR_DB", str(hermes_home / "session-compactor.db")))


def _store() -> CoordinationStore:
    global _STORE
    if _STORE is None:
        _STORE = CoordinationStore(_coordination_path())
    return _STORE


def _heartbeat_loop() -> None:
    # Own connection: SQLite connections must not cross threads.
    heartbeat_store = CoordinationStore(_coordination_path())
    while True:
        time.sleep(30)
        with _LOCK:
            snapshot = dict(_ACTIVE)
        for session_key, (_session_id, generation) in snapshot.items():
            try:
                heartbeat_store.heartbeat(session_key, generation, ttl_seconds=180)
            except Exception:
                pass


def _ensure_heartbeat() -> None:
    global _HEARTBEAT_STARTED
    with _LOCK:
        if _HEARTBEAT_STARTED:
            return
        _HEARTBEAT_STARTED = True
    threading.Thread(
        target=_heartbeat_loop,
        name="angela-compactor-lease-heartbeat",
        daemon=True,
    ).start()


def mark_active(session_key: str, session_id: str | None, generation: int) -> None:
    try:
        _store().mark_active(session_key, session_id, generation)
        with _LOCK:
            _ACTIVE[session_key] = (session_id, generation)
        _ensure_heartbeat()
    except Exception:
        pass


def clear_active(session_key: str, generation: int) -> None:
    try:
        _store().clear_active(session_key, generation)
        with _LOCK:
            current = _ACTIVE.get(session_key)
            if current and current[1] == generation:
                _ACTIVE.pop(session_key, None)
    except Exception:
        pass
