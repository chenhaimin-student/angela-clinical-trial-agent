from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from session_compactor_models import Candidate, TranscriptFingerprint, keep_schedule
from session_compactor_store import CoordinationStore, guarded_archive, transcript_fingerprint


class FakeSessionDB:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _execute_write(self, callback):
        with self._conn:
            return callback(self._conn)

    def _insert_message_rows(self, conn, session_id, messages):
        for index, message in enumerate(messages):
            conn.execute(
                "INSERT INTO messages(session_id,role,content,timestamp,active,compacted) "
                "VALUES(?,?,?,?,1,0)",
                (session_id, message["role"], message.get("content"), 1000 + index),
            )
        return len(messages), 0


def create_state_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions(
            id TEXT PRIMARY KEY, source TEXT, ended_at REAL,
            message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0
        );
        CREATE TABLE messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
            content TEXT, timestamp REAL, token_count INTEGER, active INTEGER DEFAULT 1,
            compacted INTEGER DEFAULT 0
        );
        CREATE TABLE compression_locks(
            session_id TEXT PRIMARY KEY, holder TEXT, acquired_at REAL, expires_at REAL
        );
        """
    )
    conn.execute("INSERT INTO sessions(id,source) VALUES('s1','dingtalk')")
    for i, role in enumerate(["user", "assistant", "user", "assistant"]):
        conn.execute(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES('s1',?,?,?)",
            (role, f"m{i}", 100 + i),
        )
    conn.commit()
    return conn


class PolicyTests(unittest.TestCase):
    def test_keep_schedule(self):
        self.assertEqual(keep_schedule(13), (10, 5, 3))
        self.assertEqual(keep_schedule(8), (5, 3))
        self.assertEqual(keep_schedule(5), (3,))
        self.assertEqual(keep_schedule(3), ())


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CoordinationStore(Path(self.tmp.name) / "coord.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_active_lease_and_generation_guard(self):
        with patch("session_compactor_store.os.getpid", return_value=os.getpid()):
            self.store.mark_active("key", "s1", 7, ttl_seconds=60)
            self.assertTrue(self.store.session_is_active("s1"))
            self.store.clear_active("key", 6)
            self.assertTrue(self.store.session_is_active("s1"))
            self.store.clear_active("key", 7)
            self.assertFalse(self.store.session_is_active("s1"))

    def test_session_key_blocks_before_session_id_is_known(self):
        with patch("session_compactor_store.os.getpid", return_value=os.getpid()):
            self.store.mark_active("route-key", None, 1, ttl_seconds=60)
            self.assertTrue(self.store.session_is_active("s1", "route-key"))

    def test_expired_activity_is_not_active(self):
        now = time.time()
        with patch("session_compactor_store.os.getpid", return_value=os.getpid()):
            self.store.mark_active("key", "s1", 1, ttl_seconds=1, now=now - 10)
        self.assertFalse(self.store.session_is_active("s1", now=now))
        self.assertEqual(self.store.cleanup_stale_activity(now=now), 1)

    def test_cooldown(self):
        self.store.set_cooldown("s1", 1800, "failed")
        self.assertTrue(self.store.in_cooldown("s1"))
        self.store.clear_cooldown("s1")
        self.assertFalse(self.store.in_cooldown("s1"))

    def test_checkpoint_skips_only_unchanged_transcript(self):
        self.store.set_checkpoint("s1", 42)
        self.assertTrue(self.store.checkpoint_matches("s1", 42))
        self.assertFalse(self.store.checkpoint_matches("s1", 43))


class GuardedArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = create_state_db(Path(self.tmp.name) / "state.db")
        self.db = FakeSessionDB(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_soft_archives_and_preserves_original(self):
        before = transcript_fingerprint(self.conn, "s1")
        inserted = guarded_archive(
            self.db,
            "s1",
            before,
            [{"role": "user", "content": "summary"}, {"role": "assistant", "content": "tail"}],
        )
        self.assertEqual(inserted, 2)
        active = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id='s1' AND active=1"
        ).fetchone()[0]
        archived = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id='s1' AND active=0 AND compacted=1"
        ).fetchone()[0]
        self.assertEqual(active, 2)
        self.assertEqual(archived, 4)

    def test_changed_transcript_aborts_without_writes(self):
        before = transcript_fingerprint(self.conn, "s1")
        self.conn.execute(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES('s1','user','new',999)"
        )
        self.conn.commit()
        inserted = guarded_archive(
            self.db, "s1", before, [{"role": "assistant", "content": "summary"}]
        )
        self.assertIsNone(inserted)
        archived = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id='s1' AND compacted=1"
        ).fetchone()[0]
        self.assertEqual(archived, 0)


class DaemonPreemptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = create_state_db(self.root / "state.db")
        self.conn.close()
        self.store = CoordinationStore(self.root / "coord.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_transcript_change_is_detected(self):
        from daemon import transcript_changed

        candidate = Candidate(
            "s1", "route-key", "dingtalk", TranscriptFingerprint(4, 4, 103.0), 100, 2
        )
        self.assertFalse(transcript_changed(self.root / "state.db", candidate))
        conn = sqlite3.connect(self.root / "state.db")
        conn.execute(
            "INSERT INTO messages(session_id,role,content,timestamp) "
            "VALUES('s1','user','new',200)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(transcript_changed(self.root / "state.db", candidate))

    def test_completed_worker_change_is_not_misclassified_as_preemption(self):
        from daemon import run_attempt

        worker = self.root / "fake_worker.py"
        worker.write_text(
            "import json, sqlite3, sys\n"
            "p=sys.argv[sys.argv.index('--db')+1]\n"
            "c=sqlite3.connect(p)\n"
            "c.execute(\"INSERT INTO messages(session_id,role,content,timestamp) "
            "VALUES('s1','assistant','summary',300)\")\n"
            "c.commit(); c.close()\n"
            "print(json.dumps({'status':'success','after_messages':5,'after_tokens':80}))\n",
            encoding="utf-8",
        )
        candidate = Candidate(
            "s1", "route-key", "dingtalk", TranscriptFingerprint(4, 4, 103.0), 100, 4
        )
        result = run_attempt(
            candidate,
            3,
            10,
            1,
            self.root / "state.db",
            self.store,
            worker,
            Path(sys.executable),
            0.01,
        )
        self.assertEqual(result["status"], "success")

    def test_dead_worker_cleanup_cannot_remove_another_holders_lock(self):
        from daemon import clear_dead_worker_lock

        conn = sqlite3.connect(self.root / "state.db")
        conn.execute(
            "INSERT INTO compression_locks(session_id,holder,acquired_at,expires_at) "
            "VALUES('s1','session-compactor:123:abc',1,9999999999)"
        )
        conn.commit()
        conn.close()
        clear_dead_worker_lock(self.root / "state.db", "s1", 456)
        conn = sqlite3.connect(self.root / "state.db")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM compression_locks").fetchone()[0], 1
        )
        conn.close()
        clear_dead_worker_lock(self.root / "state.db", "s1", 123)
        conn = sqlite3.connect(self.root / "state.db")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM compression_locks").fetchone()[0], 0
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
