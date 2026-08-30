from __future__ import annotations

import unittest

from angela_orchestration.capability_router import CapabilityRouter
from angela_orchestration.models import RequestMode, RouteDecision, RouteKind, SearchParameters
from angela_orchestration.request_plan import ExecutionPlan, TaskKind


class CapabilityRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = CapabilityRouter("shadow")

    def _decision(self, search: SearchParameters, *, fast: bool = False) -> RouteDecision:
        return RouteDecision(
            RouteKind.CDE_SEARCH,
            0.95,
            "legacy_test_route",
            fast_eligible=fast,
            search=search,
        )

    def test_company_marker_disease_lookup_is_local_sql(self) -> None:
        decision = self._decision(SearchParameters(
            disease="肺癌",
            sponsors=("星河",),
            biomarkers=("KRAS G12C",),
            request_mode=RequestMode.INFORMATION_RETRIEVAL,
        ))
        plan = self.router.plan("星河有哪些KRAS G12C肺癌项目", decision)
        self.assertEqual(plan.task, TaskKind.FIND_TRIALS)
        self.assertEqual(plan.execution, ExecutionPlan.LOCAL_SQL)
        self.assertTrue(plan.differs_from_legacy)

    def test_basic_patient_search_uses_local_guard(self) -> None:
        decision = self._decision(SearchParameters(
            disease="系统性红斑狼疮",
            age=55,
            sex="male",
            request_mode=RequestMode.PATIENT_MATCHING,
        ))
        plan = self.router.plan("男性55岁SLE，有什么项目", decision)
        self.assertEqual(plan.task, TaskKind.ASSESS_PATIENT)
        self.assertEqual(plan.execution, ExecutionPlan.LOCAL_GUARDED)

    def test_treatment_line_search_uses_evidence_filter_not_full_agent(self) -> None:
        decision = self._decision(SearchParameters(
            disease="非小细胞肺癌",
            treatment_line="一线",
            request_mode=RequestMode.INFORMATION_RETRIEVAL,
        ))
        plan = self.router.plan("未经系统治疗的肺癌有哪些项目", decision)
        self.assertEqual(plan.task, TaskKind.FIND_TRIALS)
        self.assertEqual(plan.execution, ExecutionPlan.LOCAL_EVIDENCE_FILTER)
        self.assertFalse(plan.ready)
        self.assertEqual(plan.unsupported_constraints, ())

    def test_personal_eligibility_stays_on_main_agent(self) -> None:
        decision = self._decision(SearchParameters(
            disease="系统性红斑狼疮",
            age=52,
            scores=("SLEDAI>12",),
            precise_eligibility=True,
            request_mode=RequestMode.CRITERIA_ANALYSIS,
        ))
        plan = self.router.plan("我SLEDAI 13分，是否适合入组？", decision)
        self.assertEqual(plan.task, TaskKind.ASSESS_PATIENT)
        self.assertEqual(plan.execution, ExecutionPlan.MAIN_AGENT)

    def test_questionnaire_stays_on_main_agent(self) -> None:
        decision = RouteDecision(
            RouteKind.CDE_QUESTIONNAIRE,
            0.99,
            "ctr_questionnaire",
            ctr_number="CTR20261056",
        )
        plan = self.router.plan("为CTR20261056生成问卷", decision)
        self.assertEqual(plan.task, TaskKind.GENERATE_QUESTIONNAIRE)
        self.assertEqual(plan.execution, ExecutionPlan.MAIN_AGENT)

    def test_general_criteria_analysis_stays_on_main_agent(self) -> None:
        decision = self._decision(SearchParameters(
            disease="COPD",
            request_mode=RequestMode.CRITERIA_ANALYSIS,
        ))
        plan = self.router.plan("COPD项目筛败率高怎么规避", decision)
        self.assertEqual(plan.task, TaskKind.CHECK_CRITERIA)
        self.assertEqual(plan.execution, ExecutionPlan.MAIN_AGENT)

    def test_open_company_analysis_stays_on_main_agent(self) -> None:
        decision = self._decision(SearchParameters(
            disease="肺癌",
            sponsors=("甲公司", "乙公司"),
            request_mode=RequestMode.INFORMATION_RETRIEVAL,
        ))
        plan = self.router.plan("比较甲乙公司的肺癌项目竞争格局和优劣", decision)
        self.assertEqual(plan.task, TaskKind.OPEN_ANALYSIS)
        self.assertEqual(plan.execution, ExecutionPlan.MAIN_AGENT)

    def test_shadow_plan_is_serializable(self) -> None:
        plan = self.router.plan(
            "上海肺癌项目",
            self._decision(SearchParameters(
                disease="肺癌", city="上海", request_mode=RequestMode.INFORMATION_RETRIEVAL,
            ), fast=True),
        )
        payload = plan.as_dict()
        self.assertEqual(payload["mode"], "shadow")
        self.assertEqual(payload["execution"], "local_sql")
        self.assertTrue(payload["ready"])
        self.assertFalse(payload["differs_from_legacy"])

    def test_shadow_mode_never_changes_the_live_decision(self) -> None:
        decision = self._decision(SearchParameters(
            disease="肺癌", request_mode=RequestMode.INFORMATION_RETRIEVAL,
        ))
        plan = self.router.plan("肺癌有哪些项目", decision)
        self.assertIs(self.router.apply(decision, plan), decision)

    def test_partial_mode_promotes_ready_sql_lookup(self) -> None:
        router = CapabilityRouter("partial")
        decision = self._decision(SearchParameters(
            disease="肺癌", request_mode=RequestMode.INFORMATION_RETRIEVAL,
        ))
        promoted = router.apply(decision, router.plan("肺癌有哪些项目", decision))
        self.assertTrue(promoted.fast_eligible)
        self.assertIn("capability_partial_promotion", promoted.reason)

    def test_partial_mode_does_not_activate_planned_evidence_filter(self) -> None:
        router = CapabilityRouter("partial")
        decision = self._decision(SearchParameters(
            disease="肺癌",
            treatment_line="一线",
            request_mode=RequestMode.INFORMATION_RETRIEVAL,
        ))
        plan = router.plan("初治肺癌有哪些项目", decision)
        self.assertFalse(plan.ready)
        self.assertIs(router.apply(decision, plan), decision)

    def test_partial_mode_does_not_activate_unimplemented_aggregation(self) -> None:
        router = CapabilityRouter("partial")
        decision = self._decision(SearchParameters(
            disease="肺癌", province="广东省",
            request_mode=RequestMode.INFORMATION_RETRIEVAL,
        ))
        plan = router.plan("广东肺癌项目哪家医院最多", decision)
        self.assertEqual(plan.task, TaskKind.COMPARE_RECORDS)
        self.assertFalse(plan.ready)
        self.assertIn("record_aggregation", plan.unsupported_constraints)
        self.assertIs(router.apply(decision, plan), decision)

    def test_mini_cannot_erase_deterministic_treatment_risk(self) -> None:
        router = CapabilityRouter("partial")
        deterministic = RouteDecision(
            RouteKind.CDE_SEARCH,
            0.98,
            "complex_information_retrieval:treatment_or_response_constraint",
            fast_eligible=False,
            search=SearchParameters(
                disease="乳腺癌",
                request_mode=RequestMode.INFORMATION_RETRIEVAL,
            ),
        )
        model_result = self._decision(SearchParameters(
            disease="乳腺癌",
            biomarkers=("HER2",),
            request_mode=RequestMode.INFORMATION_RETRIEVAL,
        ))
        plan = router.plan(
            "HER2乳腺癌术后治疗后进展，有项目吗",
            model_result,
            deterministic_decision=deterministic,
        )
        self.assertFalse(plan.ready)
        self.assertIn(
            "deterministic_unrepresented:treatment_or_response_constraint",
            plan.unsupported_constraints,
        )
        self.assertIs(router.apply(model_result, plan), model_result)

    def test_supported_evidence_filter_can_be_promoted_after_safety_signal(self) -> None:
        router = CapabilityRouter("partial")
        deterministic = RouteDecision(
            RouteKind.CDE_SEARCH,
            0.98,
            "complex_information_retrieval:treatment_or_response_constraint",
            fast_eligible=False,
            search=SearchParameters(
                disease="肺癌",
                treatment_line="初治",
                request_mode=RequestMode.INFORMATION_RETRIEVAL,
            ),
        )
        model_result = self._decision(SearchParameters(
            disease="肺癌",
            treatment_line="初治",
            request_mode=RequestMode.INFORMATION_RETRIEVAL,
        ))
        plan = router.plan(
            "初治肺癌有哪些项目",
            model_result,
            deterministic_decision=deterministic,
        )
        self.assertTrue(plan.ready)
        self.assertEqual(plan.execution, ExecutionPlan.LOCAL_EVIDENCE_FILTER)
        self.assertTrue(router.apply(model_result, plan).fast_eligible)

    def test_best_hospital_is_open_analysis_not_plain_search(self) -> None:
        decision = self._decision(SearchParameters(
            disease="肺癌", province="广东省",
            request_mode=RequestMode.INFORMATION_RETRIEVAL,
        ))
        plan = self.router.plan("广东肺癌项目哪家医院最好", decision)
        self.assertEqual(plan.task, TaskKind.OPEN_ANALYSIS)
        self.assertEqual(plan.execution, ExecutionPlan.MAIN_AGENT)

    def test_control_drug_question_remains_criteria_analysis(self) -> None:
        decision = self._decision(SearchParameters(
            disease="慢性淋巴细胞白血病",
            request_mode=RequestMode.INFORMATION_RETRIEVAL,
        ))
        plan = self.router.plan("对照药是匹妥布单抗的项目是哪一个", decision)
        self.assertEqual(plan.task, TaskKind.CHECK_CRITERIA)
        self.assertEqual(plan.execution, ExecutionPlan.MAIN_AGENT)

    def test_lost_biomarker_gene_anchor_blocks_partial_promotion(self) -> None:
        router = CapabilityRouter("partial")
        deterministic = RouteDecision(
            RouteKind.CDE_SEARCH,
            0.98,
            "complex_information_retrieval:treatment_or_response_constraint",
            fast_eligible=False,
            search=SearchParameters(
                disease="非小细胞肺癌",
                treatment_line="一线",
                biomarkers=("EXON20 INS",),
                request_mode=RequestMode.INFORMATION_RETRIEVAL,
            ),
        )
        model_result = self._decision(deterministic.search)
        plan = router.plan(
            "ERBB 20插入突变的非小细胞肺癌一线有什么试验",
            model_result,
            deterministic_decision=deterministic,
        )
        self.assertFalse(plan.ready)
        self.assertIn("biomarker_anchor_lost:ERBB", plan.unsupported_constraints)
        self.assertIs(router.apply(model_result, plan), model_result)
