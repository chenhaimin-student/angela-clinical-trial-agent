"""SQLite coordination and audit storage for the idle session compactor."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from session_compactor_models import Candidate, TranscriptFingerprint


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS active_turns (
    session_key TEXT PRIMARY KEY,
    session_id TEXT,
    owner_pid INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    started_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_active_turns_session ON active_turns(session_id, expires_at);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    source TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,
    reason TEXT,
    keep_recent_exchanges INTEGER,
    timeout_seconds INTEGER,
    before_messages INTEGER,
    before_tokens INTEGER,
    after_messages INTEGER,
    after_tokens INTEGER,
    retry_number INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_attempts_session ON attempts(session_id, started_at DESC);

CREATE TABLE IF NOT EXISTS cooldowns (
    session_id TEXT PRIMARY KEY,
    until_at REAL NOT NULL,
    reason TEXT,
    failures INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    session_id TEXT PRIMARY KEY,
    max_message_id INTEGER NOT NULL,
    completed_at REAL NOT NULL
);
"""


class CoordinationStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        self.db.close()

    def mark_active(
        self,
        session_key: str,
        session_id: Optional[str],
        generation: int,
        ttl_seconds: int = 180,
        now: Optional[float] = None,
    ) -> None:
        now = time.time() if now is None else now
        self.db.execute(
            """
            INSERT INTO active_turns
                (session_key, session_id, owner_pid, generation, started_at, heartbeat_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_key) DO UPDATE SET
                session_id=excluded.session_id,
                owner_pid=excluded.owner_pid,
                generation=excluded.generation,
                started_at=excluded.started_at,
                heartbeat_at=excluded.heartbeat_at,
                expires_at=excluded.expires_at
            """,
            (session_key, session_id, os.getpid(), generation, now, now, now + ttl_seconds),
        )

    def heartbeat(self, session_key: str, generation: int, ttl_seconds: int = 180) -> bool:
        now = time.time()
        row = self.db.execute(
            """
            UPDATE active_turns SET heartbeat_at=?, expires_at=?
            WHERE session_key=? AND generation=? AND owner_pid=?
            """,
            (now, now + ttl_seconds, session_key, generation, os.getpid()),
        )
        return row.rowcount > 0

    def clear_active(self, session_key: str, generation: int) -> None:
        self.db.execute(
            "DELETE FROM active_turns WHERE session_key=? AND generation=? AND owner_pid=?",
            (session_key, generation, os.getpid()),
        )

    def session_is_active(
        self,
        session_id: str,
        session_key: str = "",
        now: Optional[float] = None,
    ) -> bool:
        now = time.time() if now is None else now
        rows = self.db.execute(
            "SELECT owner_pid, expires_at FROM active_turns "
            "WHERE session_id=? OR (? <> '' AND session_key=?)",
            (session_id, session_key, session_key),
        ).fetchall()
        for row in rows:
            if float(row["expires_at"]) >= now and self._pid_alive(int(row["owner_pid"])):
                return True
        return False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except (OSError, ValueError):
            return False
        return True

    def cleanup_stale_activity(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        rows = self.db.execute("SELECT session_key, owner_pid, expires_at FROM active_turns").fetchall()
        stale = [
            row["session_key"]
            for row in rows
            if float(row["expires_at"]) < now or not self._pid_alive(int(row["owner_pid"]))
        ]
        for key in stale:
            self.db.execute("DELETE FROM active_turns WHERE session_key=?", (key,))
        return len(stale)

    def in_cooldown(self, session_id: str, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        row = self.db.execute(
            "SELECT until_at FROM cooldowns WHERE session_id=?", (session_id,)
        ).fetchone()
        return bool(row and float(row["until_at"]) > now)

    def set_cooldown(self, session_id: str, seconds: int, reason: str) -> None:
        now = time.time()
        self.db.execute(
            """
            INSERT INTO cooldowns(session_id, until_at, reason, failures, updated_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                until_at=excluded.until_at,
                reason=excluded.reason,
                failures=cooldowns.failures+1,
                updated_at=excluded.updated_at
            """,
            (session_id, now + seconds, reason[:500], now),
        )

    def clear_cooldown(self, session_id: str) -> None:
        self.db.execute("DELETE FROM cooldowns WHERE session_id=?", (session_id,))

    def checkpoint_matches(self, session_id: str, max_message_id: int) -> bool:
        row = self.db.execute(
            "SELECT max_message_id FROM checkpoints WHERE session_id=?", (session_id,)
        ).fetchone()
        return bool(row and int(row["max_message_id"]) == int(max_message_id))

    def set_checkpoint(self, session_id: str, max_message_id: int) -> None:
        self.db.execute(
            """
            INSERT INTO checkpoints(session_id,max_message_id,completed_at)
            VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET
                max_message_id=excluded.max_message_id,
                completed_at=excluded.completed_at
            """,
            (session_id, int(max_message_id), time.time()),
        )

    def start_attempt(
        self,
        candidate: Candidate,
        keep: int,
        timeout: int,
        retry: int,
    ) -> int:
        row = self.db.execute(
            """
            INSERT INTO attempts(
                session_id, source, started_at, status, keep_recent_exchanges,
                timeout_seconds, before_messages, before_tokens, retry_number
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)
            """,
            (
                candidate.session_id,
                candidate.source,
                time.time(),
                keep,
                timeout,
                candidate.fingerprint.count,
                candidate.approx_tokens,
                retry,
            ),
        )
        return int(row.lastrowid)

    def finish_attempt(
        self,
        attempt_id: int,
        status: str,
        reason: str = "",
        after_messages: Optional[int] = None,
        after_tokens: Optional[int] = None,
    ) -> None:
        self.db.execute(
            """
            UPDATE attempts SET finished_at=?, status=?, reason=?,
                after_messages=?, after_tokens=? WHERE id=?
            """,
            (time.time(), status, reason[:1000], after_messages, after_tokens, attempt_id),
        )


def transcript_fingerprint(conn: sqlite3.Connection, session_id: str) -> TranscriptFingerprint:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS max_id,
               COALESCE(MAX(timestamp), 0) AS last_ts
        FROM messages WHERE session_id=? AND active=1
        """,
        (session_id,),
    ).fetchone()
    return TranscriptFingerprint(int(row[0]), int(row[1]), float(row[2]))


def guarded_archive(
    session_db: Any,
    session_id: str,
    expected: TranscriptFingerprint,
    compacted: list[dict[str, Any]],
) -> Optional[int]:
    """Atomically compare the transcript and soft-archive it if unchanged."""

    def write(conn: sqlite3.Connection) -> Optional[int]:
        current = transcript_fingerprint(conn, session_id)
        if current != expected:
            return None
        conn.execute(
            "UPDATE messages SET active=0, compacted=1 WHERE session_id=? AND active=1",
            (session_id,),
        )
        inserted, tools = session_db._insert_message_rows(conn, session_id, compacted)
        conn.execute(
            "UPDATE sessions SET message_count=?, tool_call_count=? WHERE id=?",
            (inserted, tools, session_id),
        )
        return inserted

    return session_db._execute_write(write)
