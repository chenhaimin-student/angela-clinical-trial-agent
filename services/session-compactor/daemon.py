#!/usr/bin/env python3
"""Idle, preemptible, single-worker session compaction scheduler."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from session_compactor_models import Candidate, CHANNEL_SOURCES, TranscriptFingerprint, keep_schedule
from session_compactor_store import CoordinationStore, transcript_fingerprint


LOG = logging.getLogger("session-compactor")
STOP = False


def on_signal(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def estimate_from_db(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN token_count IS NOT NULL AND token_count > 0
            THEN token_count ELSE (LENGTH(COALESCE(content,''))+3)/4 END), 0)
        FROM messages WHERE session_id=? AND active=1
        """,
        (session_id,),
    ).fetchone()
    return int(row[0])


def user_turn_count(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id=? AND active=1 AND role='user'",
        (session_id,),
    ).fetchone()
    return int(row[0])


def find_candidates(
    state_db: Path,
    coordination: CoordinationStore,
    idle_seconds: int,
    token_threshold: int,
) -> list[Candidate]:
    now = time.time()
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in CHANNEL_SOURCES)
    rows = conn.execute(
        f"""
        SELECT s.id, COALESCE(s.session_key,'') AS session_key,
               lower(s.source) AS source, COUNT(m.id) AS n,
               COALESCE(MAX(m.id),0) AS max_id, COALESCE(MAX(m.timestamp),0) AS last_ts,
               SUM(CASE WHEN m.role='user' THEN 1 ELSE 0 END) AS user_turns,
               COALESCE(SUM(CASE WHEN m.token_count IS NOT NULL AND m.token_count > 0
                   THEN m.token_count ELSE (LENGTH(COALESCE(m.content,''))+3)/4 END),0) AS tokens,
               MAX(CASE WHEN m.id=(SELECT MAX(m2.id) FROM messages m2
                    WHERE m2.session_id=s.id AND m2.active=1) THEN m.role END) AS last_role
        FROM sessions s JOIN messages m ON m.session_id=s.id AND m.active=1
        WHERE lower(s.source) IN ({placeholders}) AND s.ended_at IS NULL
        GROUP BY s.id
        HAVING last_ts <= ? AND tokens >= ? AND user_turns >= 4
        ORDER BY tokens DESC
        """,
        (*CHANNEL_SOURCES, now - idle_seconds, token_threshold),
    ).fetchall()
    result: list[Candidate] = []
    for row in rows:
        sid = str(row["id"])
        # A trailing user/tool row means a turn may still be incomplete.
        if str(row["last_role"]) not in {"assistant", "session_meta"}:
            continue
        if (
            coordination.session_is_active(sid, str(row["session_key"]), now)
            or coordination.in_cooldown(sid, now)
            or coordination.checkpoint_matches(sid, int(row["max_id"]))
        ):
            continue
        result.append(
            Candidate(
                session_id=sid,
                session_key=str(row["session_key"]),
                source=str(row["source"]),
                fingerprint=TranscriptFingerprint(
                    int(row["n"]), int(row["max_id"]), float(row["last_ts"])
                ),
                approx_tokens=int(row["tokens"]),
                user_turns=int(row["user_turns"]),
            )
        )
    conn.close()
    return result


def parse_worker_result(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "status" in data:
            return data
    return {"status": "invalid_output"}


def transcript_changed(state_db: Path, candidate: Candidate) -> bool:
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
    try:
        return transcript_fingerprint(conn, candidate.session_id) != candidate.fingerprint
    finally:
        conn.close()


def clear_dead_worker_lock(state_db: Path, session_id: str, worker_pid: int) -> None:
    """Remove only the lock owned by the worker we just proved is dead."""
    prefix = f"session-compactor:{worker_pid}:%"
    conn = sqlite3.connect(state_db, timeout=10)
    try:
        with conn:
            conn.execute(
                "DELETE FROM compression_locks WHERE session_id=? AND holder LIKE ?",
                (session_id, prefix),
            )
    finally:
        conn.close()


def terminate_worker(
    process: subprocess.Popen[str], state_db: Path, session_id: str
) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        clear_dead_worker_lock(state_db, session_id, process.pid)


def run_attempt(
    candidate: Candidate,
    keep: int,
    timeout: int,
    retry: int,
    state_db: Path,
    coordination: CoordinationStore,
    worker: Path,
    python: Path,
    poll_seconds: float,
) -> dict:
    attempt_id = coordination.start_attempt(candidate, keep, timeout, retry)
    process = subprocess.Popen(
        [str(python), str(worker), candidate.session_id, "--db", str(state_db), "--keep", str(keep)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    reason = ""
    while process.poll() is None:
        if STOP:
            reason = "service_stopping"
        elif coordination.session_is_active(candidate.session_id, candidate.session_key):
            reason = "preempted_by_active_turn"
        elif transcript_changed(state_db, candidate):
            # A successful worker changes the fingerprint immediately before
            # emitting its result and exiting. Give that commit path a tiny
            # grace window; a genuinely in-flight worker means an external
            # writer (normally a new user turn) changed the transcript.
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                reason = "preempted_by_new_message"
        elif time.monotonic() >= deadline:
            reason = "timeout"
        if reason:
            terminate_worker(process, state_db, candidate.session_id)
            coordination.finish_attempt(attempt_id, "preempted" if reason.startswith("preempted") else reason, reason)
            return {"status": reason}
        time.sleep(poll_seconds)
    stdout, stderr = process.communicate()
    result = parse_worker_result(stdout)
    status = str(result.get("status", "invalid_output"))
    if process.returncode == 0 and status == "success":
        coordination.finish_attempt(
            attempt_id,
            "success",
            after_messages=int(result.get("after_messages", 0)),
            after_tokens=int(result.get("after_tokens", 0)),
        )
    else:
        # Logs keep only error class/status, never raw stderr which may contain secrets.
        coordination.finish_attempt(attempt_id, status, f"worker_exit_{process.returncode}")
    return result


def handle_candidate(
    candidate: Candidate,
    args: argparse.Namespace,
    coordination: CoordinationStore,
) -> None:
    timeouts = tuple(int(x) for x in args.timeouts.split(",") if x.strip())
    policies = keep_schedule(candidate.user_turns)
    if not policies:
        coordination.set_cooldown(candidate.session_id, args.cooldown_seconds, "insufficient_user_turns")
        return
    retry = 0
    for keep in policies:
        for timeout in timeouts:
            retry += 1
            result = run_attempt(
                candidate, keep, timeout, retry, args.state_db, coordination,
                args.worker, args.python, args.poll_seconds,
            )
            status = str(result.get("status"))
            LOG.info(
                "session=%s source=%s keep=%s timeout=%s status=%s",
                candidate.session_id, candidate.source, keep, timeout, status,
            )
            if status == "success":
                coordination.clear_cooldown(candidate.session_id)
                # The worker inserts a compacted active set with fresh ids.
                conn = sqlite3.connect(f"file:{args.state_db}?mode=ro", uri=True)
                try:
                    completed = transcript_fingerprint(conn, candidate.session_id)
                finally:
                    conn.close()
                coordination.set_checkpoint(candidate.session_id, completed.max_id)
                return
            if status.startswith("preempted") or status == "service_stopping":
                return
            if status in {"not_smaller", "no_boundary", "no_progress"}:
                break
            if status not in {"timeout", "summary_failed", "invalid_output", "locked"}:
                break
    coordination.set_cooldown(candidate.session_id, args.cooldown_seconds, "all_attempts_failed")


def configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()],
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    parser.add_argument("--state-db", type=Path, default=hermes_home / "state.db")
    parser.add_argument("--coord-db", type=Path, default=hermes_home / "session-compactor.db")
    parser.add_argument("--worker", type=Path, default=Path(__file__).with_name("worker.py"))
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--log", type=Path, default=hermes_home / "logs/session-compactor.log")
    parser.add_argument("--idle-seconds", type=int, default=60)
    parser.add_argument("--token-threshold", type=int, default=60000)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--scan-seconds", type=float, default=15.0)
    parser.add_argument("--cooldown-seconds", type=int, default=1800)
    parser.add_argument("--timeouts", default="120,240,360")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    configure_logging(args.log)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    coordination = CoordinationStore(args.coord_db)
    LOG.info("idle session compactor started pid=%s", os.getpid())
    try:
        while not STOP:
            coordination.cleanup_stale_activity()
            candidates = find_candidates(
                args.state_db, coordination, args.idle_seconds, args.token_threshold
            )
            if candidates:
                handle_candidate(candidates[0], args, coordination)
            if args.once:
                break
            time.sleep(args.scan_seconds)
    finally:
        coordination.close()
        LOG.info("idle session compactor stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
