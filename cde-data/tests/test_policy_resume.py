from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cde_store.database import initialize_database
from scripts.resume_policy_collection import reopen_policy_collection


class PolicyResumeTests(unittest.TestCase):
    def test_reopens_only_reversible_policy_items_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tempdir:
            connection = initialize_database(Path(tempdir) / "resume.sqlite3")
            checkpoint = {
                "mode": "full_catalog",
                "phase": "policy_complete",
                "completion_policy": {
                    "type": "first_publication_date_cutoff",
                    "reversible": True,
                    "skipped_items": 2,
                },
            }
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO sync_runs(
                        run_type,started_at,finished_at,status,records_skipped,checkpoint_json
                    ) VALUES ('initial','2026-08-01','2026-08-02','policy_complete',2,?)
                    """,
                    (json.dumps(checkpoint),),
                )
                run_id = int(cursor.lastrowid)
                for index in range(2):
                    connection.execute(
                        """
                        INSERT INTO sync_items(sync_run_id,registration_no,source_url,status)
                        VALUES (?,?,?,'skipped_policy')
                        """,
                        (run_id, f"CTR2017000{index}", f"https://example.test/{index}"),
                    )
            result = reopen_policy_collection(
                connection, run_id=run_id, expected_skipped=2
            )
            self.assertEqual(result["restored_pending"], 2)
            run = connection.execute(
                "SELECT status,records_skipped,checkpoint_json FROM sync_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            self.assertEqual((run["status"], run["records_skipped"]), ("partial", 0))
            restored = json.loads(run["checkpoint_json"])
            self.assertEqual(restored["phase"], "details")
            self.assertIn("resumed_completion_policy", restored)
            self.assertNotIn("completion_policy", restored)
            statuses = connection.execute(
                "SELECT DISTINCT status FROM sync_items WHERE sync_run_id=?", (run_id,)
            ).fetchall()
            self.assertEqual([row[0] for row in statuses], ["pending"])

            again = reopen_policy_collection(
                connection, run_id=run_id, expected_skipped=2
            )
            self.assertEqual(again["event"], "already_resumed_or_complete")
            connection.close()


if __name__ == "__main__":
    unittest.main()
