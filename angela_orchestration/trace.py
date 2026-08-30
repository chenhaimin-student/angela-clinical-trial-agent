from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS request_traces (
    request_id TEXT PRIMARY KEY,
    session_hash TEXT NOT NULL,
    platform TEXT NOT NULL,
    route TEXT NOT NULL,
    fast_path INTEGER NOT NULL DEFAULT 0,
    received_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    queue_wait_ms INTEGER,
    total_ms INTEGER,
    status TEXT NOT NULL,
    total_count INTEGER,
    returned_count INTEGER,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_request_traces_received ON request_traces(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_request_traces_status ON request_traces(status, route);
"""


class TraceStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._initialized = False

    @staticmethod
    def hash_session(session_key: str) -> str:
        return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:20]

    def start(self, request_id: str, session_key: str, platform: str, route: str, fast_path: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            connection = sqlite3.connect(self.path, timeout=10)
            try:
                connection.execute("PRAGMA busy_timeout=10000")
                if not self._initialized:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.executescript(SCHEMA)
                    self._initialized = True
                connection.execute(
                    "INSERT INTO request_traces(request_id,session_hash,platform,route,fast_path,received_at,status) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (request_id, self.hash_session(session_key), platform, route, int(fast_path), time.time(), "received"),
                )
                connection.commit()
            finally:
                connection.close()

    def finish(
        self,
        request_id: str,
        *,
        started_at: float,
        received_at: float,
        status: str,
        total_count: int | None = None,
        returned_count: int | None = None,
        error_code: str | None = None,
    ) -> None:
        finished = time.time()
        with self._lock:
            connection = sqlite3.connect(self.path, timeout=10)
            try:
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute(
                    "UPDATE request_traces SET started_at=?,finished_at=?,queue_wait_ms=?,total_ms=?,"
                    "status=?,total_count=?,returned_count=?,error_code=? WHERE request_id=?",
                    (
                        started_at,
                        finished,
                        max(0, int((started_at - received_at) * 1000)),
                        max(0, int((finished - received_at) * 1000)),
                        status,
                        total_count,
                        returned_count,
                        error_code,
                        request_id,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
