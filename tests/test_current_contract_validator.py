import unittest

from quality_eval.current_contract_validator import (
    Mode,
    OPERATIONAL_ERROR_RE,
    NO_RESULT_RE,
    UNKNOWN_RE,
    _affirmative_ctrs,
    _marker_match,
    independent_contract,
)
from angela_orchestration.normalization import extract_project_codes


class CurrentContractExtractionTests(unittest.TestCase):
    def test_birth_year_child_request_is_patient_matching(self):
        result = independent_contract(
            "2014年出生的女孩，诊断为major depressive disorder，请找临床研究。"
        )
        self.assertEqual(result.mode, Mode.PATIENT_MATCHING)

    def test_explicit_request_for_missing_details_is_unknown_evidence(self):
        self.assertIsNotNone(UNKNOWN_RE.search("请补充癌种和所在城市"))
        self.assertIsNotNone(UNKNOWN_RE.search("请先确认具体适应症"))
        self.assertIsNotNone(NO_RESULT_RE.search("数据库返回 0 个可匹配项目"))

    def test_negative_trial_reference_is_not_counted_as_returned_candidate(self):
        answer = (
            "CTR20251038实际是MET项目，不应作为候选。"
            "可核验的候选是CTR20260001。"
        )
        self.assertEqual(_affirmative_ctrs(answer), ("CTR20260001",))

    def test_normal_trial_caution_does_not_remove_candidate(self):
        answer = "CTR20260001可能相关，但不代表最终符合入组条件。"
        self.assertEqual(_affirmative_ctrs(answer), ("CTR20260001",))

    def test_project_code_contract_matches_runtime_extraction(self):
        for prompt in (
            "female, 41-year-old, diagnosed with Alzheimer disease",
            "non-small cell lung cancer trials",
            "SLEDAI-2K greater than 10",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(extract_project_codes(prompt), ())
                self.assertEqual(independent_contract(prompt).project_codes, ())
        self.assertEqual(extract_project_codes("EGFR-PACC projects"), ("EGFR-PACC",))
        self.assertEqual(
            independent_contract("EGFR-PACC projects").project_codes,
            ("EGFR-PACC",),
        )
    def test_specific_small_cell_lung_cancer_is_preserved(self):
        result = independent_contract("局限期小细胞肺癌目前有什么临床研究正在入组")
        self.assertEqual(result.disease_terms[0], "局限期小细胞肺癌")
        self.assertEqual(result.mode, Mode.INFORMATION_RETRIEVAL)

    def test_patient_and_criteria_modes_are_independent(self):
        self.assertEqual(
            independent_contract("男性20岁抑郁症，推荐项目").mode,
            Mode.PATIENT_MATCHING,
        )
        self.assertEqual(
            independent_contract("青岚EGFR-PACC项目的入排是怎么样的").mode,
            Mode.CRITERIA_ANALYSIS,
        )

    def test_egfr_does_not_match_renal_egfr(self):
        self.assertFalse(_marker_match("eGFR持续下降", "EGFR"))
        self.assertTrue(_marker_match("EGFR L858R突变", "EGFR"))

    def test_ascii_disease_can_touch_chinese_text(self):
        result = independent_contract("呼吸哮喘COPD单抗项目筛败率怎么规避")
        self.assertIn("COPD", result.disease_terms)

    def test_ascii_marker_can_touch_chinese_text(self):
        result = independent_contract("krasg12c一线化疗后的靶向药项目")
        self.assertIn("KRAS G12C", result.biomarkers)

    def test_model_auth_error_is_operational_failure(self):
        self.assertIsNotNone(OPERATIONAL_ERROR_RE.search("HTTP 401 auth_unavailable"))


if __name__ == "__main__":
    unittest.main()
