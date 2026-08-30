from __future__ import annotations

import os
import sqlite3
import unittest
from pathlib import Path

from angela_orchestration.config import OrchestrationConfig
from angela_orchestration.fast_path import LocalCdeFastPath
from angela_orchestration.fast_path import _ascii_disease_retry_terms
from angela_orchestration.models import SearchParameters
from angela_orchestration.patient_facts import PatientFactsBuilder
from angela_orchestration.renderer import render_search


class FastPathTests(unittest.TestCase):
    def test_ascii_retry_terms_keep_disease_acronyms_not_biomarkers(self) -> None:
        search = SearchParameters(disease="能查到哪些公开")
        self.assertEqual(
            _ascii_disease_retry_terms(
                "50岁男性CIDP，KRAS G12C，能查到哪些公开试验？",
                search,
            ),
            ("CIDP",),
        )

    def test_empty_result_keeps_auditable_lexical_policy(self) -> None:
        response = render_search(
            {
                "total_count": 0,
                "results": [],
                "data_source": "local_sqlite",
                "query": {
                    "expanded_terms": [
                        {"term": "乳腺癌", "kind": "original", "weight": 100}
                    ]
                },
            },
            SearchParameters(disease="乳腺癌"),
        )

        self.assertEqual(
            response.metadata["retrieval_policy"],
            "lexical_evidence_only_no_vector",
        )
        self.assertEqual(
            response.metadata["query_terms"],
            ({"term": "乳腺癌", "kind": "original"},),
        )

    def test_real_isolated_database_is_read_only_and_rendered(self) -> None:
        database = Path(os.environ.get("ANGELA_TEST_DATABASE", ""))
        project = Path(os.environ.get("ANGELA_TEST_PROJECT_ROOT", ""))
        if not database.is_file() or not project.is_dir():
            self.skipTest("isolated CDE database was not supplied")
        before = database.stat()
        old = dict(os.environ)
        try:
            os.environ.update({
                "ANGELA_PROJECT_ROOT": str(project),
                "ANGELA_CDE_DATABASE": str(database),
                "ANGELA_DISEASE_CONCEPTS": str(project / "cde-data/config/disease_concepts.json"),
            })
            response = LocalCdeFastPath(OrchestrationConfig.from_env()).search(
                SearchParameters(disease="SLE", age=40, sex="unknown", blood_type="A型血", max_results=5)
            )
        finally:
            os.environ.clear()
            os.environ.update(old)
        after = database.stat()
        self.assertGreater(response.total_count, 0)
        self.assertLessEqual(response.returned_count, 5)
        self.assertIn("本地CDE数据库初步匹配", response.text)
        self.assertIn("A型血", response.text)
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            connection.close()

    def test_birth_year_age_interval_is_applied_before_candidate_limit(self) -> None:
        database = Path(os.environ.get("ANGELA_TEST_DATABASE", ""))
        project = Path(os.environ.get("ANGELA_TEST_PROJECT_ROOT", ""))
        if not database.is_file() or not project.is_dir():
            self.skipTest("isolated CDE database was not supplied")
        old = dict(os.environ)
        try:
            os.environ.update({
                "ANGELA_PROJECT_ROOT": str(project),
                "ANGELA_CDE_DATABASE": str(database),
                "ANGELA_DISEASE_CONCEPTS": str(project / "cde-data/config/disease_concepts.json"),
            })
            search = SearchParameters(
                disease="SLE",
                raw_disease="SLE",
                canonical_disease="系统性红斑狼疮",
                sex="female",
                birth_year=2019,
                disease_duration_months=24,
                max_results=5,
            )
            source = "2019年出生，女，SLE 2年病史，给我5条临床研究线索。"
            facts = PatientFactsBuilder().build(source, search)
            response = LocalCdeFastPath(OrchestrationConfig.from_env()).search(
                search,
                patient_facts=facts,
                source_text=source,
            )
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertGreater(response.returned_count, 0)
        self.assertNotIn("CTR20261167", response.result_ids)


if __name__ == "__main__":
    unittest.main()
