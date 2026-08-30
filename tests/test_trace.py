from __future__ import annotations

import concurrent.futures
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from angela_orchestration.trace import TraceStore


class TraceTests(unittest.TestCase):
    def test_queue_wait_uses_actual_execution_start(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "trace.sqlite3"
            store = TraceStore(path)
            request_id = "queued-request"
            received = time.time()
            store.start(request_id, "session", "api_server", "cde_search", True)
            execution_started = received + 0.125
            store.finish(
                request_id,
                started_at=execution_started,
                received_at=received,
                status="fast_path_success",
            )
            connection = sqlite3.connect(path)
            try:
                queue_wait_ms = connection.execute(
                    "SELECT queue_wait_ms FROM request_traces WHERE request_id=?",
                    (request_id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertGreaterEqual(queue_wait_ms, 124)
            self.assertLessEqual(queue_wait_ms, 126)

    def test_concurrent_writes_are_private_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "trace.sqlite3"
            store = TraceStore(path)

            def write(index: int) -> None:
                request_id = f"request-{index}"
                received = time.time()
                store.start(request_id, f"wecom:user-secret-{index}", "wecom", "cde_search", True)
                store.finish(
                    request_id,
                    started_at=received,
                    received_at=received,
                    status="fast_path_success",
                    total_count=10,
                    returned_count=5,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(write, range(100)))

            connection = sqlite3.connect(path)
            try:
                rows = connection.execute(
                    "SELECT session_hash,status,total_count,returned_count FROM request_traces"
                ).fetchall()
                columns = {row[1] for row in connection.execute("PRAGMA table_info(request_traces)")}
            finally:
                connection.close()
            self.assertEqual(len(rows), 100)
            self.assertTrue(all(len(row[0]) == 20 for row in rows))
            self.assertNotIn("user_text", columns)
            self.assertNotIn("user_id", columns)
            self.assertTrue(all(row[1:] == ("fast_path_success", 10, 5) for row in rows))


if __name__ == "__main__":
    unittest.main()
