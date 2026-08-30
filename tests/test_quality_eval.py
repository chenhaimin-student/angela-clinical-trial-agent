from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quality_eval.contracts import load_annotations, load_suite
from quality_eval.database import connect_read_only, snapshot_database
from quality_eval.metrics import retrieval_metrics
from quality_eval.oracle import _age_interval_excluded, _phase_values, _sex_excluded
from quality_eval.runner import QualityEvaluator


class QualityEvaluationTests(unittest.TestCase):
    def test_reference_filter_handles_common_age_and_sex_wording(self) -> None:
        self.assertTrue(_age_interval_excluded(6, 7, "18岁≤年龄≤75岁", ""))
        self.assertTrue(_age_interval_excluded(6, 7, "年龄18周岁且≤69周岁", ""))
        self.assertFalse(_age_interval_excluded(55, 55, "年龄18-75周岁", ""))
        self.assertFalse(_age_interval_excluded(
            44,
            44,
            "女性体重≥45 kg，筛选时年龄≥40岁且≤85岁的男性或女性",
            "",
        ))
        self.assertFalse(_sex_excluded(
            "male",
            "所有男性试验参与者或有生育能力的女性试验参与者须避孕",
        ))
        self.assertTrue(_sex_excluded("male", "本研究仅限女性受试者"))

    def test_reference_phase_parser_accepts_subphases(self) -> None:
        self.assertEqual(_phase_values("Ib/IIa期"), {"I", "II"})

    def test_suite_and_annotation_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            suite_path = Path(root) / "suite.json"
            suite_path.write_text(json.dumps({
                "schema_version": 1,
                "suite_id": "pilot",
                "reference_date": "2026-08-21",
                "thresholds": {"retrieval_precision": 0.9},
                "cases": [{
                    "case_id": "sle",
                    "prompt": "55岁男性SLE",
                    "search": {"disease": "SLE"},
                    "expected_facts": {"age": 55},
                }],
            }, ensure_ascii=False), encoding="utf-8")
            annotation_path = Path(root) / "annotations.json"
            annotation_path.write_text(json.dumps({
                "schema_version": 1,
                "suite_id": "pilot",
                "cases": [{
                    "case_id": "sle",
                    "review_status": "complete",
                    "candidates": [
                        {"registration_no": "CTR20260001", "relevant": True},
                        {
                            "registration_no": "CTR20260002",
                            "relevant": False,
                            "expected_disposition": "EXPLICIT_CONFLICT",
                            "expected_findings": {"age": "FAIL"},
                        },
                    ],
                }],
            }, ensure_ascii=False), encoding="utf-8")

            suite = load_suite(suite_path)
            annotations = load_annotations(annotation_path)

            self.assertEqual(suite.cases[0].case_id, "sle")
            self.assertEqual(annotations["sle"]["labels"]["CTR20260001"], True)
            self.assertEqual(
                annotations["sle"]["guard_labels"]["CTR20260002"]["findings"]["age"],
                "FAIL",
            )

    def test_retrieval_metrics_separate_unjudged_results(self) -> None:
        result = retrieval_metrics(
            ["CTR20260001", "CTR20260002", "CTR20260003"],
            {"CTR20260001": True, "CTR20260002": False, "CTR20260004": True},
        )
        self.assertEqual(result.relevant_returned, 1)
        self.assertEqual(result.irrelevant_returned, 1)
        self.assertEqual(result.unjudged_returned, 1)
        self.assertEqual(result.precision, 0.5)
        self.assertEqual(result.recall, 0.5)
        self.assertAlmostEqual(result.label_coverage, 2 / 3)

    def test_excel_friendly_csv_annotations_are_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "review.csv"
            path.write_text(
                "suite_id,database_fingerprint,case_id,review_status,registration_no,relevant,"
                "expected_disposition,expected_findings_json,reviewer,reviewed_at\n"
                "pilot,abc,sle,complete,CTR20260001,true,NEEDS_CONFIRMATION,"
                '"{""age"":""UNKNOWN""}",reviewer,2026-08-21\n',
                encoding="utf-8-sig",
            )
            annotations = load_annotations(path)
            self.assertTrue(annotations["sle"]["labels"]["CTR20260001"])
            self.assertEqual(
                annotations["sle"]["guard_labels"]["CTR20260001"]["findings"]["age"],
                "UNKNOWN",
            )

    def test_database_snapshot_is_read_only_and_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "formal.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE schema_migrations(version INTEGER, applied_at TEXT);
                CREATE TABLE trials(
                    id INTEGER PRIMARY KEY,
                    registration_no TEXT,
                    is_active INTEGER,
                    recruitment_status_normalized TEXT,
                    updated_at TEXT,
                    current_version_id INTEGER
                );
                CREATE TABLE trial_criteria(
                    id INTEGER PRIMARY KEY,
                    trial_version_id INTEGER,
                    criterion_type TEXT
                );
                INSERT INTO schema_migrations VALUES(9,'2026-08-01');
                INSERT INTO trials VALUES(1,'CTR20260001',1,'recruiting','2026-08-21',11);
                INSERT INTO trial_criteria VALUES(1,11,'inclusion');
            """)
            connection.commit()
            connection.close()

            first = snapshot_database(path)
            second = snapshot_database(path)
            read_only = connect_read_only(path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    read_only.execute("DELETE FROM trials")
            finally:
                read_only.close()

            self.assertEqual(first.logical_fingerprint, second.logical_fingerprint)
            self.assertEqual(first.recruitable_trial_count, 1)
            self.assertEqual(first.criteria_trial_count, 1)

    def test_aggregate_uses_automatic_reference_checks_without_manual_labels(self) -> None:
        case = {
            "review_status": "pending",
            "returned_ids": ["CTR20260001"],
            "retrieval": {
                "relevant_returned": 1,
                "irrelevant_returned": 0,
                "oracle_top_k_recalled": 1,
                "oracle_top_k_total": 1,
            },
            "raw_total_count": 1,
            "oracle": {"raw_total_count": 1},
            "facts": {"checks": {"age": {"match": True}}},
            "source_field_checks": [{"match": True}],
            "guard_checks": [],
            "machine_checks": {
                "all_returned_ctr_exist": True,
                "all_returned_statuses_recruitable": True,
                "explicit_conflict_returned": 0,
            },
        }
        aggregate = QualityEvaluator._aggregate(case and [case], {
            "ctr_existence_rate": 1.0,
            "retrieval_precision": 1.0,
            "retrieval_recall_at_k": 0.85,
            "raw_total_count_accuracy": 1.0,
            "source_field_fidelity": 1.0,
        }, {"criteria_trial_count": 99, "active_trial_count": 100})
        self.assertEqual(aggregate["gates"]["ctr_existence_rate"]["status"], "PASS")
        self.assertEqual(aggregate["gates"]["retrieval_precision"]["status"], "PASS")
        self.assertEqual(aggregate["gates"]["retrieval_recall_at_k"]["status"], "PASS")
        self.assertEqual(aggregate["overall_status"], "PASS")


if __name__ == "__main__":
    unittest.main()
