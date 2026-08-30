from __future__ import annotations

import unittest

from angela_orchestration.evidence_filter import (
    ConstraintEvidenceFilter,
    EvidenceState,
    apply_constraint_reviews,
)
from angela_orchestration.models import SearchParameters


def trial(ctr: str, inclusion: str, *, indication: str = "非小细胞肺癌") -> dict:
    return {
        "registration_no": ctr,
        "indication_raw": indication,
        "public_title": "公开研究",
        "professional_title": "",
        "inclusion_criteria_raw": inclusion,
        "inclusion_criteria": [{"raw_text": inclusion}],
    }


class EvidenceFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.filter = ConstraintEvidenceFilter()

    def test_first_line_public_evidence_is_pass(self) -> None:
        review = self.filter.evaluate(
            SearchParameters(disease="肺癌", treatment_line="初治"),
            trial("CTR20260001", "既往未接受过针对晚期疾病的系统治疗"),
        )
        self.assertEqual(review.findings[0].state, EvidenceState.PASS)
        self.assertEqual(review.pass_count, 1)

    def test_explicit_later_line_requirement_conflicts_with_untreated_request(self) -> None:
        review = self.filter.evaluate(
            SearchParameters(disease="肺癌", treatment_line="初治"),
            trial("CTR20260002", "既往二线系统治疗失败后进展的患者"),
        )
        self.assertTrue(review.has_conflict)
        self.assertEqual(review.findings[0].state, EvidenceState.FAIL)

    def test_prior_drug_and_progression_are_evidence_bound(self) -> None:
        review = self.filter.evaluate(
            SearchParameters(
                disease="肺癌",
                prior_treatments=("含铂化疗",),
                disease_statuses=("进展",),
            ),
            trial("CTR20260003", "含铂化疗后出现疾病进展的受试者"),
        )
        self.assertEqual([item.state for item in review.findings], [EvidenceState.PASS, EvidenceState.PASS])

    def test_unknown_is_retained_but_ranked_after_evidence_match(self) -> None:
        search = SearchParameters(disease="肺癌", treatment_line="初治")
        matched_trial = trial("CTR20260004", "一线治疗人群")
        unknown_trial = trial("CTR20260005", "肺癌患者")
        matched = self.filter.evaluate(search, matched_trial)
        unknown = self.filter.evaluate(search, unknown_trial)
        records, counts = apply_constraint_reviews(
            [
                {"registration_no": "CTR20260005", "match_score": 100},
                {"registration_no": "CTR20260004", "match_score": 80},
            ],
            {"CTR20260004": matched, "CTR20260005": unknown},
            limit=2,
        )
        self.assertEqual([item["registration_no"] for item in records], ["CTR20260004", "CTR20260005"])
        self.assertEqual(counts["PASS"], 1)
        self.assertEqual(counts["UNKNOWN"], 1)
