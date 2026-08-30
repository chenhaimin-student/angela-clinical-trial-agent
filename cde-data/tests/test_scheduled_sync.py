from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_scheduled_sync import main


class ScheduledSyncExitTests(unittest.TestCase):
    def test_item_level_partial_completion_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "test.sqlite3"
            progress = root / "progress.jsonl"

            class Worker:
                def health(self):
                    return {"status": "ready"}

            class Planner:
                def __init__(self, connection, *_args, **_kwargs):
                    self.connection = connection

                def start_or_resume(self, _schedule_date):
                    with self.connection:
                        cursor = self.connection.execute(
                            "INSERT INTO sync_runs(run_type,started_at,status) "
                            "VALUES ('weekly_review','2026-08-17T00:00:00+00:00','running')"
                        )
                    return cursor.lastrowid

                def summary(self, run_id):
                    return {"run_id": run_id}

                def manual_intervention_reason(self, _run_id):
                    return None

                def discover(self, run_id):
                    return {"run_id": run_id, "phase": "details"}

            class Detail:
                def __init__(self, *_args, **_kwargs):
                    pass

                def process(self, _run_id, **_kwargs):
                    return {"success": 122, "failed": 3, "pending": 0,
                            "processed_this_call": 0}

            arguments = [
                "run_scheduled_sync.py", "--run-type", "weekly_review",
                "--database", str(database), "--raw-root", str(root / "raw"),
                "--lock-file", str(root / "sync.lock"),
                "--progress-file", str(progress),
            ]
            with (
                patch.object(sys, "argv", arguments),
                patch("scripts.run_scheduled_sync.BrowserWorkerClient", return_value=Worker()),
                patch("scripts.run_scheduled_sync.WeeklyCatalogReview", Planner),
                patch("scripts.run_scheduled_sync.CdeSyncService", Detail),
            ):
                self.assertEqual(main(), 0)

            events = [json.loads(line)["event"] for line in progress.read_text().splitlines()]
            self.assertIn("completed_with_item_failures", events)


if __name__ == "__main__":
    unittest.main()
