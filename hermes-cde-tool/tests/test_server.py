from datetime import date
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import (
    FAST_CANDIDATE_LIMIT,
    FAST_MAX_RESULTS,
    FAST_SEARCH_PAGES,
    _build_exploration_context,
    _compact_search_response,
    _normalize_local_statuses,
    _parse_iso_date,
    _parse_trial_reference,
    get_cde_trial_questionnaire_source,
    calendar_months_ago,
    normalize_optional_text,
    normalize_terms,
)
from cde_demo.models import PatientProfile


class ContractTests(unittest.TestCase):
    @patch("server.get_cde_trial_questionnaire_source_live")
    @patch("server.get_cde_trial_criteria_local")
    def test_local_questionnaire_source_uses_verified_canonical_contract(
        self,
        local_lookup,
        live_lookup,
    ):
        local_lookup.return_value = {
            "lookup_status": "found",
            "data_source": "local_sqlite",
            "ctr_number": "CTR20261056",
            "title": "GARDENIA-SLE",
            "indication": "系统性红斑狼疮",
            "recruitment_status": "招募中",
            "last_verified_at": "2026-08-20T00:00:00Z",
            "criteria_available": True,
            "inclusion_criteria": [{"raw_text": "年龄18至75岁"}],
            "exclusion_criteria": [{"raw_text": "严重感染"}],
            "requires_live_verification": False,
        }

        result = get_cde_trial_questionnaire_source("CTR20261056")

        self.assertEqual(result["lookup_status"], "verified")
        self.assertEqual(result["lookup_route"], "local_primary")
        self.assertEqual(result["verification_scope"], "local_public_snapshot")
        self.assertEqual(result["trial"]["registration_no"], "CTR20261056")
        self.assertEqual(
            result["criteria_for_questionnaire"]["inclusion_criteria"],
            [{"raw_text": "年龄18至75岁"}],
        )
        self.assertNotIn("inclusion_criteria", result)
        self.assertNotIn("exclusion_criteria", result)
        live_lookup.assert_not_called()

    def test_compact_search_response_drops_criteria_and_restricted_fields(self):
        result = _compact_search_response({
            "total_count": 1,
            "returned_count": 1,
            "results": [{
                "registration_no": "CTR20261056",
                "public_title": "示例研究",
                "indication_raw": "系统性红斑狼疮",
                "match_evidence": [
                    {"term": "SLE", "field": "indication_raw", "score": 100},
                    {"term": "SLE", "field": "indication_raw", "score": 100},
                ],
                "inclusion_criteria_raw": "不应进入普通搜索响应",
                "contacts": [{"phone_raw": "不应返回"}],
            }],
            "query": {"expanded_terms": list(map(str, range(30)))},
            "warnings": [],
            "data_source": "local_sqlite",
        })
        item = result["results"][0]
        self.assertEqual(item["registration_no"], "CTR20261056")
        self.assertNotIn("inclusion_criteria_raw", item)
        self.assertNotIn("contacts", item)
        self.assertEqual(len(item["match_evidence"]), 1)
        self.assertEqual(len(result["query"]["expanded_terms"]), 12)

    def test_calendar_months_ago_clamps_end_of_month(self):
        self.assertEqual(calendar_months_ago(date(2026, 8, 31), 6), date(2026, 2, 28))

    def test_calendar_months_ago_rejects_unbounded_lookback(self):
        with self.assertRaises(ValueError):
            calendar_months_ago(date(2026, 8, 6), 0)

    def test_normalize_terms_removes_duplicates_and_whitespace(self):
        self.assertEqual(normalize_terms([" 肺癌 ", "肺癌", ""], "condition_terms"), ("肺癌",))

    def test_empty_optional_region_is_treated_as_omitted(self):
        self.assertIsNone(normalize_optional_text("   ", "region"))

    def test_fast_mode_has_a_small_bounded_budget(self):
        self.assertEqual((FAST_SEARCH_PAGES, FAST_CANDIDATE_LIMIT, FAST_MAX_RESULTS), (1, 10, 5))

    def test_exploration_context_explains_score_and_missing_non_identity_fields(self):
        context = _build_exploration_context(PatientProfile(), "fast")
        self.assertEqual(context["search_mode"], "fast")
        self.assertEqual(len(context["demo_follow_up_options"]), 3)
        self.assertGreaterEqual(len(context["suggested_information_to_refine"]), 3)

    def test_trial_reference_accepts_registration_number_case_insensitively(self):
        self.assertEqual(_parse_trial_reference("ctr20261056"), ("CTR20261056", None))

    def test_trial_reference_accepts_only_official_cde_detail_urls(self):
        self.assertEqual(
            _parse_trial_reference(
                "https://www.chinadrugtrials.org.cn/clinicaltrials.searchlistdetail.dhtml?id=8f9910139a404b9aad6812850049d012"
            ),
            (None, "8f9910139a404b9aad6812850049d012"),
        )

    def test_trial_reference_rejects_an_arbitrary_url(self):
        with self.assertRaises(ValueError):
            _parse_trial_reference("https://example.com/?id=8f9910139a404b9aad6812850049d012")

    def test_local_date_uses_explicit_iso_format(self):
        self.assertEqual(_parse_iso_date("2026-05-13", "date_from"), date(2026, 5, 13))
        with self.assertRaises(ValueError):
            _parse_iso_date("近三个月", "date_from")

    def test_local_status_defaults_to_patient_visible_recruitment(self):
        self.assertEqual(
            [str(value) for value in _normalize_local_statuses(None)],
            ["recruiting", "not_yet_recruiting"],
        )

    def test_local_status_rejects_unknown_value(self):
        with self.assertRaises(ValueError):
            _normalize_local_statuses(["whatever"])

    def test_local_status_accepts_chinese_agent_values(self):
        self.assertEqual(
            [str(value) for value in _normalize_local_statuses(["招募中", "尚未招募"])],
            ["recruiting", "not_yet_recruiting"],
        )


if __name__ == "__main__":
    unittest.main()
