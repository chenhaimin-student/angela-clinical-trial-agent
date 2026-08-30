from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator


class RequestState(str, Enum):
    RECEIVED = "RECEIVED"
    PARSING = "PARSING"
    WAITING_USER = "WAITING_USER"
    READY = "READY"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DELIVERING = "DELIVERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = {RequestState.COMPLETED, RequestState.FAILED, RequestState.CANCELLED}
ALLOWED_TRANSITIONS: dict[RequestState, set[RequestState]] = {
    RequestState.RECEIVED: {RequestState.PARSING, RequestState.CANCELLED, RequestState.FAILED},
    RequestState.PARSING: {
        RequestState.WAITING_USER,
        RequestState.READY,
        RequestState.FAILED,
        RequestState.CANCELLED,
    },
    RequestState.WAITING_USER: {RequestState.PARSING, RequestState.CANCELLED, RequestState.FAILED},
    RequestState.READY: {RequestState.QUEUED, RequestState.RUNNING, RequestState.CANCELLED, RequestState.FAILED},
    RequestState.QUEUED: {RequestState.RUNNING, RequestState.CANCELLED, RequestState.FAILED},
    RequestState.RUNNING: {
        RequestState.QUEUED,
        RequestState.DELIVERING,
        RequestState.COMPLETED,
        RequestState.FAILED,
    },
    RequestState.DELIVERING: {RequestState.DELIVERING, RequestState.COMPLETED, RequestState.FAILED},
    RequestState.COMPLETED: set(),
    RequestState.FAILED: {RequestState.QUEUED},
    RequestState.CANCELLED: set(),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    idempotency_key: str
    session_key: str
    channel: str
    state: RequestState
    stage: str | None
    reason_code: str | None
    queue_position: int | None
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    version: int
    created_at: str
    updated_at: str


class InvalidTransition(ValueError):
    pass


class RequestStateStore:
    """Small durable request journal for the single-Mac service phase.

    It intentionally lives in a separate SQLite file from the read-only CDE
    database. WAL mode allows concurrent readers while state transitions remain
    serialized and versioned.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    session_key TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    state TEXT NOT NULL,
                    stage TEXT,
                    reason_code TEXT,
                    queue_position INTEGER,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error_json TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_requests_session_state
                    ON requests(session_key,state,updated_at);
                CREATE TABLE IF NOT EXISTS request_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    stage TEXT,
                    reason_code TEXT,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT,
                    FOREIGN KEY(request_id) REFERENCES requests(request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_request_events_request
                    ON request_events(request_id,id);
                """
            )

    def create_or_get(
        self,
        *,
        request_id: str,
        idempotency_key: str,
        session_key: str,
        channel: str,
        payload: dict[str, Any],
    ) -> tuple[RequestRecord, bool]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM requests WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                return self._record(existing), False
            connection.execute(
                "INSERT INTO requests(request_id,idempotency_key,session_key,channel,state,"
                "payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    idempotency_key,
                    session_key,
                    channel,
                    RequestState.RECEIVED.value,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO request_events(request_id,to_state,created_at,metadata_json) "
                "VALUES(?,?,?,?)",
                (request_id, RequestState.RECEIVED.value, now, "{}"),
            )
            row = connection.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            assert row is not None
            return self._record(row), True

    def transition(
        self,
        request_id: str,
        to_state: RequestState,
        *,
        stage: str | None = None,
        reason_code: str | None = None,
        queue_position: int | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RequestRecord:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise KeyError(request_id)
            current = RequestState(row["state"])
            if to_state not in ALLOWED_TRANSITIONS[current]:
                raise InvalidTransition(f"{current.value} cannot transition to {to_state.value}")
            now = utc_now()
            version = int(row["version"]) + 1
            connection.execute(
                "UPDATE requests SET state=?,stage=?,reason_code=?,queue_position=?,result_json=?,"
                "error_json=?,version=?,updated_at=? WHERE request_id=? AND version=?",
                (
                    to_state.value,
                    stage,
                    reason_code,
                    queue_position,
                    json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else row["result_json"],
                    json.dumps(error, ensure_ascii=False, sort_keys=True) if error is not None else row["error_json"],
                    version,
                    now,
                    request_id,
                    row["version"],
                ),
            )
            if connection.total_changes != 1:
                raise RuntimeError("concurrent request state update")
            connection.execute(
                "INSERT INTO request_events(request_id,from_state,to_state,stage,reason_code,created_at,metadata_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    request_id,
                    current.value,
                    to_state.value,
                    stage,
                    reason_code,
                    now,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            updated = connection.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            assert updated is not None
            return self._record(updated)

    def get(self, request_id: str) -> RequestRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            return self._record(row) if row is not None else None

    def get_by_idempotency_key(self, idempotency_key: str) -> RequestRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            return self._record(row) if row is not None else None

    def update_queue_position(self, request_id: str, queue_position: int) -> RequestRecord:
        if queue_position < 1:
            raise ValueError("queue_position must be positive")
        now = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            if row["state"] != RequestState.QUEUED.value:
                raise InvalidTransition("queue position is only valid while QUEUED")
            version = int(row["version"]) + 1
            connection.execute(
                "UPDATE requests SET queue_position=?,version=?,updated_at=? "
                "WHERE request_id=? AND version=?",
                (queue_position, version, now, request_id, row["version"]),
            )
            connection.execute(
                "INSERT INTO request_events(request_id,from_state,to_state,stage,reason_code,"
                "created_at,metadata_json) VALUES(?,?,?,?,?,?,?)",
                (
                    request_id,
                    RequestState.QUEUED.value,
                    RequestState.QUEUED.value,
                    "queue_position_updated",
                    None,
                    now,
                    json.dumps({"queue_position": queue_position}, sort_keys=True),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM requests WHERE request_id=?", (request_id,)
            ).fetchone()
            assert updated is not None
            return self._record(updated)

    def prepare_interrupted_for_replay(self) -> int:
        """Move interrupted work to READY without pretending it was executed.

        The channel adapter may redeliver the original platform message after a
        gateway restart. That replay claims the same request id and continues;
        it never creates a second logical request or a duplicate answer.
        """
        states = (
            RequestState.RECEIVED.value,
            RequestState.PARSING.value,
            RequestState.READY.value,
            RequestState.QUEUED.value,
            RequestState.RUNNING.value,
            RequestState.DELIVERING.value,
        )
        marks = ",".join("?" for _ in states)
        now = utc_now()
        recovered = 0
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT request_id,state,version FROM requests WHERE state IN ({marks}) ",
                states,
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE requests SET state=?,stage=?,reason_code=?,queue_position=NULL,"
                    "version=?,updated_at=? WHERE request_id=? AND version=?",
                    (
                        RequestState.READY.value,
                        "awaiting_channel_replay",
                        "PROCESS_RESTART_RECOVERY_PENDING",
                        int(row["version"]) + 1,
                        now,
                        row["request_id"],
                        row["version"],
                    ),
                )
                connection.execute(
                    "INSERT INTO request_events(request_id,from_state,to_state,stage,reason_code,"
                    "created_at,metadata_json) VALUES(?,?,?,?,?,?,?)",
                    (
                        row["request_id"],
                        row["state"],
                        RequestState.READY.value,
                        "awaiting_channel_replay",
                        "PROCESS_RESTART_RECOVERY_PENDING",
                        now,
                        '{"recovered_after_restart": true}',
                    ),
                )
                recovered += 1
        return recovered

    def claim_replayed(self, idempotency_key: str) -> tuple[RequestRecord | None, bool]:
        """Atomically claim a restart-recovered request for one redelivery."""
        now = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM requests WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if row is None:
                return None, False
            if not (
                row["state"] == RequestState.READY.value
                and row["reason_code"] == "PROCESS_RESTART_RECOVERY_PENDING"
            ):
                return self._record(row), False
            version = int(row["version"]) + 1
            cursor = connection.execute(
                "UPDATE requests SET state=?,stage=?,reason_code=NULL,version=?,updated_at=? "
                "WHERE request_id=? AND version=?",
                (
                    RequestState.PARSING.value,
                    "replayed_after_restart",
                    version,
                    now,
                    row["request_id"],
                    row["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("concurrent replay claim")
            connection.execute(
                "INSERT INTO request_events(request_id,from_state,to_state,stage,reason_code,"
                "created_at,metadata_json) VALUES(?,?,?,?,?,?,?)",
                (
                    row["request_id"],
                    RequestState.READY.value,
                    RequestState.PARSING.value,
                    "replayed_after_restart",
                    "CHANNEL_REDELIVERY_CLAIMED",
                    now,
                    '{"replayed_after_restart": true}',
                ),
            )
            updated = connection.execute(
                "SELECT * FROM requests WHERE request_id=?", (row["request_id"],)
            ).fetchone()
            assert updated is not None
            return self._record(updated), True

    def recoverable(self) -> list[RequestRecord]:
        states = (
            RequestState.RECEIVED.value,
            RequestState.PARSING.value,
            RequestState.READY.value,
            RequestState.QUEUED.value,
            RequestState.RUNNING.value,
            RequestState.DELIVERING.value,
        )
        marks = ",".join("?" for _ in states)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM requests WHERE state IN ({marks}) ORDER BY created_at,request_id",
                states,
            ).fetchall()
            return [self._record(row) for row in rows]

    def cancel_if_queued(self, request_id: str) -> RequestRecord:
        record = self.get(request_id)
        if record is None:
            raise KeyError(request_id)
        if record.state not in {RequestState.RECEIVED, RequestState.PARSING, RequestState.READY, RequestState.QUEUED}:
            raise InvalidTransition("only a request that has not started may be cancelled")
        return self.transition(request_id, RequestState.CANCELLED, reason_code="USER_CANCELLED")

    @staticmethod
    def _record(row: sqlite3.Row) -> RequestRecord:
        return RequestRecord(
            request_id=row["request_id"],
            idempotency_key=row["idempotency_key"],
            session_key=row["session_key"],
            channel=row["channel"],
            state=RequestState(row["state"]),
            stage=row["stage"],
            reason_code=row["reason_code"],
            queue_position=row["queue_position"],
            payload=json.loads(row["payload_json"] or "{}"),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            version=int(row["version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
