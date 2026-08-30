from __future__ import annotations

import unittest

from quality_eval.evaluation_outcomes import EvaluationOutcome, classify_outcome


class EvaluationOutcomeTests(unittest.TestCase):
    def test_valid_fast_answer_is_pass_fast(self) -> None:
        result = classify_outcome(route="local_cde_query", answer="找到 CTR20260001")
        self.assertEqual(result.outcome, EvaluationOutcome.PASS_FAST)

    def test_grounded_complex_answer_is_pass_complex(self) -> None:
        result = classify_outcome(route="main_agent", answer="根据公开标准，CTR20260001需要进一步筛查。")
        self.assertEqual(result.outcome, EvaluationOutcome.PASS_COMPLEX)

    def test_grounded_complex_answer_can_preserve_condition_level_unknown(self) -> None:
        result = classify_outcome(
            route="main_agent",
            answer="CTR20260001与疾病匹配，但最终年龄条件需要研究医生确认。",
        )
        self.assertEqual(result.outcome, EvaluationOutcome.PASS_COMPLEX)

    def test_explicit_evidence_gap_is_unknown_not_failure(self) -> None:
        result = classify_outcome(
            route="main_agent",
            answer="公开资料不足，暂时不能确认治疗线数是否满足。",
        )
        self.assertEqual(result.outcome, EvaluationOutcome.UNKNOWN_EVIDENCE)

    def test_soft_oracle_gap_is_unknown_for_complex_route(self) -> None:
        result = classify_outcome(
            route="main_agent",
            answer="已给出疾病相关项目，但精确条件还需核对。",
            issues=("复杂附加条件无法仅凭疾病级Oracle确认是否漏召回",),
        )
        self.assertEqual(result.outcome, EvaluationOutcome.UNKNOWN_EVIDENCE)

    def test_false_empty_is_failure(self) -> None:
        result = classify_outcome(
            route="main_agent",
            answer="暂未找到项目。",
            issues=("false_empty_answer",),
        )
        self.assertEqual(result.outcome, EvaluationOutcome.FAIL)

    def test_conflict_leakage_is_failure(self) -> None:
        result = classify_outcome(
            route="local_cde_query",
            answer="CTR20260001",
            issues=("explicit_conflict_leakage:CTR20260001",),
        )
        self.assertEqual(result.outcome, EvaluationOutcome.FAIL)

    def test_blank_response_is_failure(self) -> None:
        result = classify_outcome(route="main_agent", answer="")
        self.assertEqual(result.outcome, EvaluationOutcome.FAIL)

    def test_unknown_route_is_failure(self) -> None:
        result = classify_outcome(route=None, answer="有回答")
        self.assertEqual(result.outcome, EvaluationOutcome.FAIL)


if __name__ == "__main__":
    unittest.main()
