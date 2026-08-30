from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from quality_eval.e2e_validator import (
    extract_returned_trial_ids,
    has_no_result_statement,
    request_stage_timings,
    search_parameters,
)


class EndToEndValidatorTests(unittest.TestCase):
    def test_search_parameter_lists_become_tuples(self) -> None:
        search = search_parameters({
            "disease": "SLE",
            "biomarkers": ["ANA阳性"],
            "prior_treatments": ["激素"],
        })
        self.assertEqual(search.biomarkers, ("ANA阳性",))
        self.assertEqual(search.prior_treatments, ("激素",))

    def test_repeated_ctr_citations_count_as_one_returned_trial(self) -> None:
        answer = (
            "1. CTR20263140 | trial summary\n"
            "link: CTR20263140\n"
            "2. ctr20263178 | trial summary\n"
            "public link: CTR20263178"
        )
        self.assertEqual(
            extract_returned_trial_ids(answer),
            ("CTR20263140", "CTR20263178"),
        )

    def test_common_no_result_phrases_are_recognized(self) -> None:
        self.assertTrue(has_no_result_statement("目前没有检索到符合条件的项目。"))
        self.assertTrue(has_no_result_statement("本地数据库未检索到对应记录。"))
        self.assertFalse(has_no_result_statement("以下是检索到的三个项目。"))

    def test_request_stage_timings_are_derived_from_durable_events(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "requests.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE requests(
                    request_id TEXT PRIMARY KEY, session_key TEXT, state TEXT,
                    stage TEXT, reason_code TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE request_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT,
                    to_state TEXT, stage TEXT, reason_code TEXT,
                    created_at TEXT, metadata_json TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO requests VALUES(?,?,?,?,?,?,?)",
                ("r1", "synthetic-session", "COMPLETED", "completed", None,
                 "2026-08-25T00:00:00+00:00", "2026-08-25T00:00:04+00:00"),
            )
            events = (
                ("RECEIVED", None, "2026-08-25T00:00:00+00:00"),
                ("PARSING", "routing_and_patient_facts", "2026-08-25T00:00:00.100000+00:00"),
                ("READY", "route_decided", "2026-08-25T00:00:01+00:00"),
                ("QUEUED", "agent_admission", "2026-08-25T00:00:01.100000+00:00"),
                ("RUNNING", "main_agent", "2026-08-25T00:00:02+00:00"),
                ("DELIVERING", "channel_delivery", "2026-08-25T00:00:03.500000+00:00"),
                ("COMPLETED", "completed", "2026-08-25T00:00:04+00:00"),
            )
            connection.executemany(
                "INSERT INTO request_events(request_id,to_state,stage,created_at,metadata_json) VALUES('r1',?,?,?,'{}')",
                events,
            )
            connection.commit()
            connection.close()

            timing = request_stage_timings(path, "synthetic-session")
            self.assertIsNotNone(timing)
            self.assertEqual(timing["execution_route"], "main_agent")
            self.assertEqual(timing["routing_ms"], 1000)
            self.assertEqual(timing["queue_ms"], 900)
            self.assertEqual(timing["execution_ms"], 1500)
            self.assertEqual(timing["delivery_ms"], 500)
            self.assertEqual(timing["total_ms"], 4000)


if __name__ == "__main__":
    unittest.main()
