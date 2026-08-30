from __future__ import annotations

import unittest
from datetime import date

from angela_orchestration.eligibility import (
    EligibilityGuard,
    EligibilityState,
    TrialDisposition,
    filter_guarded_results,
)
from angela_orchestration.models import SearchParameters
from angela_orchestration.patient_facts import PatientFactsBuilder


def trial(ctr: str, inclusion: str, exclusion: str = "") -> dict:
    return {
        "registration_no": ctr,
        "_version_content_hash": f"hash-{ctr}",
        "indication_raw": "系统性红斑狼疮",
        "public_title": "SLE临床研究",
        "professional_title": "系统性红斑狼疮研究",
        "inclusion_criteria": [{"sequence_no": 1, "raw_text": inclusion}],
        "exclusion_criteria": [{"sequence_no": 1, "raw_text": exclusion}] if exclusion else [],
    }


class EligibilityGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        search = SearchParameters(
            disease="SLE",
            raw_disease="SLE",
            canonical_disease="系统性红斑狼疮",
            sex="female",
            birth_year=2019,
        )
        self.facts = PatientFactsBuilder().build(
            "2019年出生，女，SLE两年",
            search,
            reference_date=date(2026, 8, 21),
        )
        self.guard = EligibilityGuard()

    def finding(self, evaluation, category: str):
        return next(item for item in evaluation.findings if item.category == category)

    def test_adult_trial_is_an_explicit_conflict(self) -> None:
        evaluation = self.guard.evaluate(self.facts, trial("CTR20260001", "年龄18至65岁，男女均可"))
        self.assertEqual(evaluation.disposition, TrialDisposition.EXPLICIT_CONFLICT)
        self.assertEqual(self.finding(evaluation, "age").state, EligibilityState.FAIL)

    def test_chained_age_interval_is_an_explicit_child_conflict(self) -> None:
        evaluation = self.guard.evaluate(
            self.facts,
            trial("CTR20261167", "18岁≤年龄≤75岁（以签署知情同意书日期计算）"),
        )
        self.assertEqual(evaluation.disposition, TrialDisposition.EXPLICIT_CONFLICT)
        self.assertEqual(self.finding(evaluation, "age").state, EligibilityState.FAIL)

    def test_age_with_upper_bound_wording_is_an_explicit_child_conflict(self) -> None:
        evaluation = self.guard.evaluate(
            self.facts,
            trial("CTR20260012", "年龄18周岁且≤69周岁"),
        )
        self.assertEqual(evaluation.disposition, TrialDisposition.EXPLICIT_CONFLICT)
        self.assertEqual(self.finding(evaluation, "age").state, EligibilityState.FAIL)

    def test_explicit_adult_title_blocks_child_even_when_criteria_omit_age(self) -> None:
        adult = trial("CTR20260010", "签署知情同意书")
        adult["public_title"] = "一项在系统性红斑狼疮成人患者中的研究"
        evaluation = self.guard.evaluate(self.facts, adult)
        age = self.finding(evaluation, "age")
        self.assertEqual(age.state, EligibilityState.FAIL)
        self.assertEqual(age.source, "public_trial_fields")
        self.assertEqual(evaluation.disposition, TrialDisposition.EXPLICIT_CONFLICT)

    def test_child_trial_is_a_potential_match_when_known_conditions_pass(self) -> None:
        evaluation = self.guard.evaluate(self.facts, trial("CTR20260002", "年龄6至12岁，女性受试者"))
        self.assertEqual(self.finding(evaluation, "age").state, EligibilityState.PASS)
        self.assertEqual(self.finding(evaluation, "sex").state, EligibilityState.PASS)
        self.assertNotEqual(evaluation.disposition, TrialDisposition.EXPLICIT_CONFLICT)

    def test_partial_age_overlap_is_unknown_not_pass(self) -> None:
        evaluation = self.guard.evaluate(self.facts, trial("CTR20260003", "年龄7至12岁，男女不限"))
        self.assertEqual(self.finding(evaluation, "age").state, EligibilityState.UNKNOWN)
        self.assertEqual(evaluation.disposition, TrialDisposition.NEEDS_CONFIRMATION)

    def test_explicit_sex_conflict_is_hidden(self) -> None:
        male_search = SearchParameters(
            disease="SLE", raw_disease="SLE", canonical_disease="系统性红斑狼疮",
            age=30, sex="male",
        )
        male = PatientFactsBuilder().build("30岁男性SLE", male_search)
        evaluation = self.guard.evaluate(male, trial("CTR20260004", "年龄18至65岁，仅限女性"))
        self.assertEqual(self.finding(evaluation, "sex").state, EligibilityState.FAIL)
        self.assertEqual(evaluation.disposition, TrialDisposition.EXPLICIT_CONFLICT)

    def test_explicit_female_title_blocks_male_when_criteria_omit_sex(self) -> None:
        male_search = SearchParameters(
            disease="SLE", raw_disease="SLE", canonical_disease="系统性红斑狼疮",
            age=30, sex="male",
        )
        male = PatientFactsBuilder().build("30岁男性SLE", male_search)
        female_trial = trial("CTR20260011", "签署知情同意书")
        female_trial["public_title"] = "在女性系统性红斑狼疮患者中的研究"
        evaluation = self.guard.evaluate(male, female_trial)
        self.assertEqual(self.finding(evaluation, "sex").state, EligibilityState.FAIL)
        self.assertEqual(evaluation.disposition, TrialDisposition.EXPLICIT_CONFLICT)

    def test_reproductive_requirements_for_both_sexes_are_not_female_only(self) -> None:
        male_search = SearchParameters(
            disease="SLE", raw_disease="SLE", canonical_disease="系统性红斑狼疮",
            age=58, sex="male",
        )
        male = PatientFactsBuilder().build("58岁男性SLE", male_search)
        evaluation = self.guard.evaluate(male, trial(
            "CTR20262877",
            "年龄18岁以上；有生育能力的女性受试者必须避孕；男性受试者必须采取避孕措施",
        ))
        self.assertNotEqual(self.finding(evaluation, "sex").state, EligibilityState.FAIL)
        self.assertNotEqual(evaluation.disposition, TrialDisposition.EXPLICIT_CONFLICT)

    def test_healthy_participant_trial_is_hidden_from_patient_search(self) -> None:
        healthy = trial("CTR20263098", "年龄18至45岁，健康受试者")
        healthy["indication_raw"] = "非小细胞肺癌"
        healthy["public_title"] = "评估CT-3505对中国健康参与者QTc间期影响的研究"
        evaluation = self.guard.evaluate(self.facts, healthy)
        participant_type = self.finding(evaluation, "participant_type")
        self.assertEqual(participant_type.state, EligibilityState.FAIL)
        self.assertEqual(
            participant_type.reason_code,
            "PATIENT_QUERY_CONFLICTS_WITH_HEALTHY_PARTICIPANTS_ONLY",
        )
        self.assertEqual(evaluation.disposition, TrialDisposition.EXPLICIT_CONFLICT)

    def test_mixed_healthy_and_patient_study_is_not_hidden_as_healthy_only(self) -> None:
        mixed = trial("CTR20262122", "健康参与者以及系统性红斑狼疮患者均可参加")
        mixed["public_title"] = "在健康参与者及系统性红斑狼疮患者中的研究"
        evaluation = self.guard.evaluate(self.facts, mixed)
        self.assertFalse(any(item.category == "participant_type" for item in evaluation.findings))
        self.assertNotEqual(evaluation.disposition, TrialDisposition.EXPLICIT_CONFLICT)

    def test_missing_score_is_unknown_when_public_criteria_requires_it(self) -> None:
        evaluation = self.guard.evaluate(
            self.facts,
            trial("CTR20260005", "年龄6至12岁，SLEDAI-2K评分大于等于6分"),
        )
        self.assertIn("score", evaluation.missing_categories)
        self.assertEqual(evaluation.disposition, TrialDisposition.NEEDS_CONFIRMATION)

    def test_supplied_score_is_preserved_but_not_auto_passed(self) -> None:
        search = SearchParameters(
            disease="SLE",
            raw_disease="SLE",
            canonical_disease="系统性红斑狼疮",
            age=52,
            sex="male",
            scores=("SLEDAI得分大于12分",),
            precise_eligibility=True,
        )
        facts = PatientFactsBuilder().build(
            "52岁男性SLE，SLEDAI得分大于12分",
            search,
        )
        evaluation = self.guard.evaluate(
            facts,
            trial("CTR20260009", "年龄18至65岁，SLEDAI-2K评分不低于6分"),
        )
        self.assertNotIn("score", evaluation.missing_categories)
        self.assertEqual(self.finding(evaluation, "score").state, EligibilityState.UNKNOWN)

    def test_filter_removes_explicit_conflicts_but_keeps_unknown(self) -> None:
        failed = self.guard.evaluate(self.facts, trial("CTR20260006", "年龄18至65岁"))
        unknown = self.guard.evaluate(self.facts, trial("CTR20260007", "SLEDAI评分大于6分"))
        results, counts = filter_guarded_results(
            [{"registration_no": "CTR20260006"}, {"registration_no": "CTR20260007"}],
            {"CTR20260006": failed, "CTR20260007": unknown},
            limit=5,
        )
        self.assertEqual([item["registration_no"] for item in results], ["CTR20260007"])
        self.assertEqual(counts["EXPLICIT_CONFLICT"], 1)

    def test_guard_records_immutable_trial_version_cache_key(self) -> None:
        evaluation = self.guard.evaluate(
            self.facts,
            trial("CTR20260008", "年龄6至12岁，男女不限"),
        )
        self.assertEqual(evaluation.metadata["rule_cache_key"], "hash-CTR20260008")


if __name__ == "__main__":
    unittest.main()
