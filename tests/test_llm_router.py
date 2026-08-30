from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from angela_orchestration.config import OrchestrationConfig
from angela_orchestration.llm_router import LlmIntentRouter
from angela_orchestration.models import RouteDecision, RouteKind, SearchParameters


def _config(**changes) -> OrchestrationConfig:
    base = OrchestrationConfig.from_env()
    values = {
        "llm_router_enabled": True,
        "llm_router_model": "gpt-5.4-mini",
        "llm_router_reasoning_effort": "low",
        "llm_router_timeout_seconds": 10.0,
        "llm_router_confidence_threshold": 0.85,
        "llm_router_max_retries": 1,
        # Most tests below exercise the mini-model branch directly. Production
        # defaults to bypassing it only for complete deterministic fast searches.
        "llm_router_bypass_simple_fast": False,
    }
    values.update(changes)
    return replace(base, **values)


def _fallback(disease: str = "有个哮喘") -> RouteDecision:
    return RouteDecision(
        RouteKind.CDE_SEARCH,
        0.92,
        "structured_patient_search",
        fast_eligible=True,
        search=SearchParameters(disease=disease, age=37),
    )


def _general() -> RouteDecision:
    return RouteDecision(RouteKind.GENERAL, 0.90, "general_or_ambiguous")


def _payload(**changes):
    payload = {
        "intent": "cde_search",
        "disease": "哮喘",
        "complex": False,
        "confidence": 0.96,
    }
    payload.update(changes)
    return payload


class _StatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__("upstream status")
        self.status_code = status_code


class LlmIntentRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_resolved_site_filter_bypasses_model_without_being_dropped(self):
        router = LlmIntentRouter(_config(llm_router_bypass_simple_fast=True))
        router._request_json = AsyncMock(return_value=_payload())
        fallback = RouteDecision(
            RouteKind.CDE_SEARCH,
            0.99,
            "information_retrieval_search",
            fast_eligible=True,
            search=SearchParameters(
                disease="asthma",
                city="Shanghai",
                site_names=("Ruijin Hospital",),
            ),
        )

        decision = await router.resolve("Ruijin Hospital asthma trials", fallback)

        self.assertEqual(decision.reason, "deterministic_entity_fast")
        self.assertEqual(decision.search.site_names, ("Ruijin Hospital",))
        router._request_json.assert_not_awaited()

    async def test_complete_sponsor_variant_filters_cannot_be_rewritten_by_model(self):
        router = LlmIntentRouter(_config(llm_router_bypass_simple_fast=True))
        router._request_json = AsyncMock(return_value=_payload(disease="G12C项目"))
        fallback = RouteDecision(
            RouteKind.CDE_SEARCH,
            0.99,
            "explicit_cde_search",
            fast_eligible=True,
            search=SearchParameters(
                sponsors=("星河",),
                biomarkers=("G12C",),
            ),
        )

        decision = await router.resolve("星河有哪些G12C项目？", fallback)

        self.assertEqual(decision.reason, "deterministic_entity_fast")
        self.assertEqual(decision.search.disease, "")
        self.assertEqual(decision.search.sponsors, ("星河",))
        self.assertEqual(decision.search.biomarkers, ("G12C",))
        router._request_json.assert_not_awaited()

    async def test_complete_deterministic_simple_search_bypasses_small_model(self):
        router = LlmIntentRouter(_config(llm_router_bypass_simple_fast=True))
        router._request_json = AsyncMock(return_value=_payload())

        decision = await router.resolve("有个哮喘患者，37岁，想找临床试验", _fallback())

        self.assertTrue(decision.fast_eligible)
        self.assertEqual(decision.reason, "deterministic_fast_normalized")
        self.assertEqual(decision.search.disease, "有个哮喘")
        self.assertEqual(decision.search.age, 37)
        router._request_json.assert_not_awaited()

    async def test_deterministic_fast_normalization_keeps_basic_filters_for_unknown_concept(self):
        router = LlmIntentRouter(_config(llm_router_bypass_simple_fast=True))
        router._request_json = AsyncMock(return_value=_payload())
        fallback = RouteDecision(
            RouteKind.CDE_SEARCH,
            0.98,
            "explicit_cde_search",
            fast_eligible=True,
            search=SearchParameters(disease="乳腺癌"),
        )

        decision = await router.resolve(
            "45岁女性，乳腺癌，上海，查询最近一年的5条试验。",
            fallback,
        )

        self.assertEqual(decision.reason, "deterministic_fast_normalized")
        self.assertEqual(decision.search.age, 45)
        self.assertEqual(decision.search.sex, "female")
        self.assertEqual(decision.search.city, "上海")
        self.assertEqual(decision.search.lookback_months, 12)
        self.assertEqual(decision.search.max_results, 5)
        router._request_json.assert_not_awaited()

    async def test_deterministic_fast_keeps_location_out_of_disease_identity(self):
        router = LlmIntentRouter(_config(llm_router_bypass_simple_fast=True))
        router._request_json = AsyncMock(return_value=_payload())
        fallback = RouteDecision(
            RouteKind.CDE_SEARCH,
            0.98,
            "information_retrieval_search",
            fast_eligible=True,
            search=SearchParameters(
                disease="克罗恩病",
                raw_disease="克罗恩病",
                canonical_disease="克罗恩病",
                city="广州",
            ),
        )

        decision = await router.resolve(
            "广州地区克罗恩病研究，给我最近公开的5个。",
            fallback,
        )

        self.assertEqual(decision.search.disease, "克罗恩病")
        self.assertEqual(decision.search.city, "广州")
        router._request_json.assert_not_awaited()

    async def test_simple_fast_bypass_can_be_disabled_for_comparison(self):
        router = LlmIntentRouter(_config(llm_router_bypass_simple_fast=False))
        router._request_json = AsyncMock(return_value=_payload())

        decision = await router.resolve("有个哮喘患者，37岁，想找临床试验", _fallback())

        self.assertTrue(decision.fast_eligible)
        self.assertEqual(decision.reason, "route_validator_enriched_rule_fast_path")
        router._request_json.assert_awaited_once()

    async def test_complex_medical_request_goes_to_main_agent(self):
        router = LlmIntentRouter(_config())
        router._request_json = AsyncMock(
            return_value=_payload(complex=True, confidence=0.99)
        )

        decision = await router.resolve(
            "哮喘，初次诊断并有复杂既往治疗，判断能否入组", _fallback("哮喘")
        )

        self.assertFalse(decision.fast_eligible)
        self.assertEqual(decision.reason, "route_validator_complex_to_agent")

    async def test_confidence_below_threshold_goes_to_main_agent(self):
        router = LlmIntentRouter(_config())
        router._request_json = AsyncMock(return_value=_payload(confidence=0.84))

        decision = await router.resolve("帮我找哮喘临床试验", _fallback("哮喘"))

        self.assertFalse(decision.fast_eligible)
        self.assertEqual(decision.reason, "route_validator_low_confidence_to_agent")

    async def test_timeout_falls_back_to_existing_rule(self):
        router = LlmIntentRouter(_config(llm_router_timeout_seconds=0.01))

        async def slow(_text):
            await asyncio.sleep(0.1)
            return _payload()

        router._request_json = slow
        fallback = _fallback()

        decision = await router.resolve("有个哮喘患者，37岁", fallback)

        self.assertTrue(decision.fast_eligible)
        self.assertEqual(decision.search.disease, "有个哮喘")
        self.assertTrue(decision.reason.startswith("llm_timeout_rule_fallback:"))

    async def test_429_and_5xx_are_retried_once(self):
        for status in (429, 503):
            with self.subTest(status=status):
                router = LlmIntentRouter(_config())
                router._request_json = AsyncMock(
                    side_effect=[_StatusError(status), _payload()]
                )

                decision = await router.resolve("帮我找哮喘临床试验", _fallback("哮喘"))

                self.assertTrue(decision.fast_eligible)
                self.assertEqual(router._request_json.await_count, 2)

    async def test_non_retryable_error_falls_back_without_retry(self):
        router = LlmIntentRouter(_config())
        router._request_json = AsyncMock(side_effect=_StatusError(400))
        fallback = _fallback("哮喘")

        decision = await router.resolve("帮我找哮喘临床试验", fallback)

        self.assertTrue(decision.fast_eligible)
        self.assertEqual(router._request_json.await_count, 1)
        self.assertTrue(decision.reason.startswith("llm_error_rule_fallback:"))

    async def test_privacy_and_ctr_hard_routes_never_reach_small_model(self):
        router = LlmIntentRouter(_config())
        router._request_json = AsyncMock(return_value=_payload())
        hard_routes = (
            RouteDecision(RouteKind.PRIVACY_BLOCK, 1.0, "direct_identifier_detected"),
            RouteDecision(RouteKind.CDE_DETAIL, 0.99, "ctr_detail", ctr_number="CTR20261056"),
            RouteDecision(
                RouteKind.CDE_QUESTIONNAIRE,
                0.99,
                "ctr_questionnaire",
                ctr_number="CTR20261056",
            ),
        )

        for decision in hard_routes:
            self.assertIs(await router.resolve("sensitive-or-ctr", decision), decision)
        router._request_json.assert_not_awaited()

    async def test_general_known_disease_project_query_reaches_small_model(self):
        router = LlmIntentRouter(_config())
        router._request_json = AsyncMock(
            return_value=_payload(disease="SLE", complex=False, confidence=0.97)
        )

        decision = await router.resolve("目前 SLE 有什么项目？", _general())

        self.assertEqual(decision.kind, RouteKind.CDE_SEARCH)
        self.assertTrue(decision.fast_eligible)
        self.assertEqual(decision.search.disease, "SLE")
        router._request_json.assert_awaited_once()

    async def test_general_project_code_and_criteria_reaches_small_model(self):
        router = LlmIntentRouter(_config())
        router._request_json = AsyncMock(
            return_value=_payload(disease="ABC1234", complex=True, confidence=0.98)
        )

        decision = await router.resolve("ABC1234项目入组有什么要求吗？", _general())

        self.assertEqual(decision.kind, RouteKind.CDE_SEARCH)
        self.assertFalse(decision.fast_eligible)
        self.assertEqual(decision.reason, "route_validator_complex_to_agent")
        self.assertEqual(decision.search.disease, "ABC1234")
        router._request_json.assert_awaited_once()

    async def test_pure_greeting_does_not_reach_small_model(self):
        router = LlmIntentRouter(_config())
        router._request_json = AsyncMock(return_value=_payload())
        fallback = _general()

        decision = await router.resolve("你好", fallback)

        self.assertIs(decision, fallback)
        router._request_json.assert_not_awaited()

    async def test_invalid_payload_falls_back_to_rule(self):
        router = LlmIntentRouter(_config())
        router._request_json = AsyncMock(return_value=_payload(confidence="high"))

        decision = await router.resolve("有个哮喘患者，37岁", _fallback())

        self.assertEqual(decision.search.disease, "有个哮喘")
        self.assertTrue(decision.reason.startswith("llm_error_rule_fallback:"))

    async def test_extended_payload_keeps_all_explicit_conditions(self):
        router = LlmIntentRouter(_config())
        router._request_json = AsyncMock(return_value={
            "intent": "cde_search",
            "query_type": "disease",
            "raw_disease": "lupus",
            "canonical_disease": "系统性红斑狼疮",
            "preserve_specificity": False,
            "age": 44,
            "age_min": None,
            "age_max": None,
            "sex": "male",
            "city": "广州",
            "region": None,
            "blood_type": None,
            "lookback_months": 6,
            "trial_phases": [],
            "max_results": 5,
            "birth_year": None,
            "disease_duration_months": None,
            "disease_stage": None,
            "treatment_line": None,
            "prior_treatments": [],
            "biomarkers": [],
            "scores": [],
            "comorbidities": [],
            "disease_statuses": [],
            "organ_function_findings": [],
            "reproductive_statuses": [],
            "allergies": [],
            "precise_eligibility": False,
            "complex": False,
            "needs_clarification": False,
            "confidence": 0.97,
        })

        decision = await router.resolve(
            "Male, 44, lupus, Guangzhou, trials in the last 6 months?", _general()
        )

        self.assertTrue(decision.fast_eligible)
        self.assertEqual(decision.search.disease, "lupus")
        self.assertEqual(decision.search.age, 44)
        self.assertEqual(decision.search.sex, "male")
        self.assertEqual(decision.search.city, "广州")
        self.assertEqual(decision.search.lookback_months, 6)

    async def test_extended_patient_conditions_are_preserved(self):
        router = LlmIntentRouter(_config())
        router._request_json = AsyncMock(return_value={
            "intent": "cde_search",
            "query_type": "disease",
            "raw_disease": "肺腺癌",
            "canonical_disease": "肺腺癌",
            "preserve_specificity": True,
            "age": 55,
            "age_min": None,
            "age_max": None,
            "sex": "male",
            "city": None,
            "region": None,
            "blood_type": None,
            "lookback_months": None,
            "trial_phases": [],
            "max_results": 5,
            "birth_year": None,
            "disease_duration_months": 24,
            "disease_stage": "晚期",
            "treatment_line": "一线进展",
            "prior_treatments": ["铂类化疗"],
            "biomarkers": ["EGFR L858R"],
            "scores": ["ECOG 1分"],
            "comorbidities": ["高血压"],
            "disease_statuses": ["进展"],
            "organ_function_findings": ["肝肾功能正常"],
            "reproductive_statuses": [],
            "allergies": ["青霉素过敏"],
            "precise_eligibility": True,
            "complex": True,
            "needs_clarification": False,
            "confidence": 0.98,
        })

        decision = await router.resolve(
            "55岁男性晚期肺腺癌，EGFR L858R，铂类化疗后一线进展，精准判断",
            _general(),
        )

        self.assertFalse(decision.fast_eligible)
        self.assertEqual(decision.search.biomarkers, ("EGFR L858R",))
        self.assertEqual(decision.search.prior_treatments, ("铂类化疗后一线进展",))
        self.assertEqual(decision.search.treatment_line, "一线进展")
        self.assertEqual(decision.search.scores, ("ECOG 1分",))
        self.assertEqual(decision.search.organ_function_findings, ("肝肾功能正常",))
        self.assertEqual(decision.search.allergies, ("青霉素过敏",))
        self.assertTrue(decision.search.precise_eligibility)

    async def test_complex_model_decision_can_veto_rule_fast_candidate(self):
        router = LlmIntentRouter(_config())
        router._request_json = AsyncMock(return_value=_payload(complex=True, confidence=0.99))
        fallback = _fallback("哮喘")

        decision = await router.resolve("37岁哮喘患者，查临床研究", fallback)

        self.assertFalse(decision.fast_eligible)
        self.assertEqual(decision.reason, "route_validator_complex_to_agent")

    async def test_clarification_can_veto_rule_fast_candidate(self):
        router = LlmIntentRouter(_config())
        router._request_json = AsyncMock(
            return_value=_payload(
                complex=False,
                needs_clarification=True,
                confidence=0.98,
            )
        )

        decision = await router.resolve(
            "未成年十来岁小孩，抑郁症，推荐临床试验", _fallback("抑郁症")
        )

        self.assertFalse(decision.fast_eligible)
        self.assertEqual(decision.reason, "route_validator_clarification_to_agent")

    async def test_model_cannot_downgrade_deterministic_complex_request(self):
        router = LlmIntentRouter(_config())
        router._request_json = AsyncMock(
            return_value=_payload(complex=False, confidence=0.99)
        )
        fallback = RouteDecision(
            RouteKind.CDE_SEARCH,
            0.98,
            "complex_patient_search",
            fast_eligible=False,
            search=SearchParameters(disease="SLE", age=55),
        )

        decision = await router.resolve(
            "55岁男性SLE，既往治疗复杂，请判断是否可能适合临床试验",
            fallback,
        )

        self.assertFalse(decision.fast_eligible)
        self.assertEqual(decision.reason, "route_validator_rule_complex_to_agent")

    async def test_advanced_patient_facts_cannot_enter_fast_path(self):
        router = LlmIntentRouter(_config())
        payload = {
            "intent": "cde_search",
            "query_type": "disease",
            "raw_disease": "肺腺癌",
            "canonical_disease": "肺腺癌",
            "preserve_specificity": True,
            "age": 55,
            "age_min": None,
            "age_max": None,
            "sex": "male",
            "city": None,
            "region": None,
            "blood_type": None,
            "lookback_months": None,
            "trial_phases": [],
            "max_results": 5,
            "birth_year": None,
            "disease_duration_months": None,
            "disease_stage": None,
            "treatment_line": None,
            "prior_treatments": ["含铂化疗"],
            "biomarkers": ["KRAS G12C突变"],
            "scores": [],
            "comorbidities": [],
            "disease_statuses": ["进展"],
            "organ_function_findings": [],
            "reproductive_statuses": [],
            "allergies": [],
            "precise_eligibility": False,
            "complex": False,
            "needs_clarification": False,
            "confidence": 0.99,
        }
        router._request_json = AsyncMock(return_value=payload)

        decision = await router.resolve(
            "55岁男性肺腺癌，KRAS G12C突变，含铂化疗后进展，查临床试验",
            _general(),
        )

        self.assertFalse(decision.fast_eligible)
        self.assertEqual(decision.reason, "route_validator_advanced_facts_to_agent")


if __name__ == "__main__":
    unittest.main()
