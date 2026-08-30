from __future__ import annotations

import unittest
from datetime import date

from angela_orchestration.models import SearchParameters
from angela_orchestration.patient_facts import FactStatus, PatientFactsBuilder


class PatientFactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = PatientFactsBuilder()
        self.reference = date(2026, 8, 21)

    def search(self, **values) -> SearchParameters:
        defaults = {
            "disease": "SLE",
            "raw_disease": "SLE",
            "canonical_disease": "系统性红斑狼疮",
            "sex": "female",
        }
        defaults.update(values)
        return SearchParameters(**defaults)

    def test_birth_year_becomes_a_safe_age_interval(self) -> None:
        facts = self.builder.build(
            "2019年出生，女，SLE两年",
            self.search(disease_duration_months=24),
            reference_date=self.reference,
        )
        self.assertEqual(facts.birth_year.value, 2019)
        self.assertEqual((facts.age.minimum_years, facts.age.maximum_years), (6, 7))
        self.assertEqual(facts.age.status, FactStatus.DERIVED)
        self.assertFalse(facts.requires_clarification)

    def test_diagnosis_year_is_not_a_birth_year(self) -> None:
        facts = self.builder.build(
            "患者2019年确诊SLE",
            self.search(),
            reference_date=self.reference,
        )
        self.assertIsNone(facts.birth_year.value)

    def test_birth_year_and_exact_age_conflict_blocks_search(self) -> None:
        facts = self.builder.build(
            "2019年出生，今年45岁，女性，SLE",
            self.search(age=45),
            reference_date=self.reference,
        )
        self.assertTrue(facts.requires_clarification)
        self.assertIn("AGE_BIRTH_YEAR_CONFLICT", {issue.code for issue in facts.conflicts})
        self.assertIn("不一致", facts.clarification_message())

    def test_disease_duration_longer_than_age_blocks_search(self) -> None:
        facts = self.builder.build(
            "10岁，SLE病史20年",
            self.search(age=10, disease_duration_months=240),
            reference_date=self.reference,
        )
        self.assertTrue(facts.requires_clarification)
        self.assertIn("DISEASE_DURATION_EXCEEDS_AGE", {issue.code for issue in facts.conflicts})

    def test_missing_age_does_not_block_broad_search(self) -> None:
        facts = self.builder.build("SLE有什么项目", self.search(), reference_date=self.reference)
        self.assertFalse(facts.requires_clarification)
        self.assertIn("AGE_MISSING", {issue.code for issue in facts.missing})

    def test_conflicting_sex_descriptions_block_search(self) -> None:
        facts = self.builder.build(
            "男性，后面资料写女性，SLE",
            self.search(sex="male"),
            reference_date=self.reference,
        )
        self.assertTrue(facts.requires_clarification)
        self.assertIn("SEX_CONFLICT", {issue.code for issue in facts.conflicts})


if __name__ == "__main__":
    unittest.main()
