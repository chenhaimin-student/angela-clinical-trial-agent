from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from .evidence_filter import SUPPORTED_EVIDENCE_FIELDS
from .models import RequestMode, RouteDecision, RouteKind, SearchParameters
from .request_plan import (
    ExecutionCapability,
    ExecutionPlan,
    QueryConstraint,
    RequestPlan,
    TaskKind,
)


_OPEN_ANALYSIS = re.compile(
    r"(?:竞争格局|优劣|优势|劣势|最好|哪个更好|哪家更好|研发关系|申办关系|为什么|原因分析)",
    re.IGNORECASE,
)
_QUESTIONNAIRE = re.compile(r"(?:问卷|一题一题|逐题)", re.IGNORECASE)
_CRITERIA = re.compile(
    r"(?:入排标准|入选标准|排除标准|入组要求|筛败率|筛选失败率|对照药)",
    re.IGNORECASE,
)
_ELIGIBILITY = re.compile(
    r"(?:我|本人|患者|病人).{0,30}(?:是否适合|能否入组|可以参加|适不适合)"
    r"|(?:是否适合|能否入组|适不适合).{0,20}(?:我|本人|患者|病人|这个项目)",
    re.IGNORECASE,
)
_COMPARE = re.compile(
    r"(?:多少|几个|数量|最多|排名|排行|分布|哪家|哪些公司|哪些医院|项目数)",
    re.IGNORECASE,
)
_BIOMARKER_ANCHOR_PATTERNS = (
    re.compile(
        r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9-]{1,11})\s*"
        r"(?:(?:exon|外显子)?\s*\d{1,3})?\s*"
        r"(?:插入|缺失|突变|融合|扩增|阳性|阴性|ins\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])([A-Za-z]{2,8})\s*"
        r"(?:G|V|L|T|E|Q|R)\d+[A-Za-z](?![A-Za-z0-9])",
        re.IGNORECASE,
    ),
)


class CapabilityRouter:
    """Build an auditable plan without changing the live route in shadow mode."""

    def __init__(self, mode: str = "shadow"):
        self.mode = mode if mode in {"off", "shadow", "partial", "on"} else "shadow"

    def plan(
        self,
        text: str,
        decision: RouteDecision,
        *,
        deterministic_decision: RouteDecision | None = None,
    ) -> RequestPlan:
        if self.mode == "off":
            return RequestPlan(
                task=TaskKind.GENERAL,
                execution=ExecutionPlan.CHANNEL_BYPASS,
                reasons=("capability_router_disabled",),
                legacy_fast_eligible=decision.fast_eligible,
                mode=self.mode,
            )
        if decision.kind == RouteKind.CDE_QUESTIONNAIRE or _QUESTIONNAIRE.search(text):
            return self._agent_plan(TaskKind.GENERATE_QUESTIONNAIRE, decision, "questionnaire_generation")
        if decision.kind == RouteKind.CDE_LIVE_VERIFY:
            return self._agent_plan(TaskKind.CHECK_CRITERIA, decision, "live_web_verification")
        if decision.kind == RouteKind.CDE_DETAIL:
            return self._agent_plan(TaskKind.CHECK_CRITERIA, decision, "ctr_detail_requires_evidence_answer")
        if decision.kind != RouteKind.CDE_SEARCH or decision.search is None:
            return RequestPlan(
                task=TaskKind.GENERAL,
                execution=ExecutionPlan.CHANNEL_BYPASS,
                reasons=("non_cde_search",),
                legacy_fast_eligible=decision.fast_eligible,
                mode=self.mode,
            )

        search = decision.search
        constraints = self._constraints(search)
        deterministic_reasons = self._deterministic_complexity_reasons(
            deterministic_decision
        )
        task = self._task(text, search, deterministic_reasons)
        if task in {
            TaskKind.GENERATE_QUESTIONNAIRE,
            TaskKind.CHECK_CRITERIA,
            TaskKind.OPEN_ANALYSIS,
        }:
            execution = ExecutionPlan.MAIN_AGENT
            reasons = ("open_ended_language_reasoning",)
        elif task == TaskKind.ASSESS_PATIENT and (
            search.precise_eligibility or _ELIGIBILITY.search(text)
        ):
            execution = ExecutionPlan.MAIN_AGENT
            reasons = ("patient_level_eligibility_explanation",)
        elif any(item.capability == ExecutionCapability.EVIDENCE_FILTER for item in constraints):
            execution = ExecutionPlan.LOCAL_EVIDENCE_FILTER
            reasons = ("requires_local_evidence_filter_implementation",)
        elif any(item.capability == ExecutionCapability.ELIGIBILITY_GUARD for item in constraints):
            execution = ExecutionPlan.LOCAL_GUARDED
            reasons = ("basic_patient_constraints_are_guardable",)
        else:
            execution = ExecutionPlan.LOCAL_SQL
            reasons = ("structured_public_record_lookup",)
        needs_aggregation = task == TaskKind.COMPARE_RECORDS
        evidence_fields = {
            item.field
            for item in constraints
            if item.capability == ExecutionCapability.EVIDENCE_FILTER
        }
        unsupported = sorted(evidence_fields - SUPPORTED_EVIDENCE_FIELDS)
        if needs_aggregation:
            unsupported.append("record_aggregation")
        missing_biomarker_anchors = self._missing_biomarker_anchors(
            text,
            search.biomarkers,
        )
        unsupported.extend(
            f"biomarker_anchor_lost:{value}"
            for value in missing_biomarker_anchors
        )
        locally_supported_deterministic_reasons = {
            "treatment_or_response_constraint",
            "treatment_line",
            "prior_treatment",
            "disease_stage",
            "disease_status",
        }
        unsupported.extend(
            f"deterministic:{reason}"
            for reason in deterministic_reasons
            if reason not in locally_supported_deterministic_reasons
        )
        if deterministic_reasons and not evidence_fields:
            unsupported.extend(
                f"deterministic_unrepresented:{reason}"
                for reason in deterministic_reasons
                if reason in locally_supported_deterministic_reasons
            )
        evidence_ready = bool(
            execution == ExecutionPlan.LOCAL_EVIDENCE_FILTER
            and task == TaskKind.FIND_TRIALS
            and not unsupported
            and deterministic_reasons
            and set(deterministic_reasons).issubset(
                locally_supported_deterministic_reasons
            )
        )
        ordinary_local_ready = bool(
            execution in {ExecutionPlan.LOCAL_SQL, ExecutionPlan.LOCAL_GUARDED}
            and not needs_aggregation
            and not deterministic_reasons
        )
        if execution == ExecutionPlan.LOCAL_EVIDENCE_FILTER:
            reasons = (
                ("local_public_evidence_filter",)
                if evidence_ready
                else ("requires_local_evidence_filter_implementation",)
            )
        return RequestPlan(
            task=task,
            execution=execution,
            constraints=constraints,
            unsupported_constraints=tuple(unsupported),
            reasons=reasons,
            legacy_fast_eligible=decision.fast_eligible,
            mode=self.mode,
            ready=evidence_ready or ordinary_local_ready,
            metadata={
                "legacy_reason": decision.reason,
                "deterministic_complexity_reasons": list(deterministic_reasons),
            },
        )

    def apply(self, decision: RouteDecision, plan: RequestPlan) -> RouteDecision:
        """Apply only proven local promotions in partial/on mode.

        Shadow mode never changes execution. Partial mode is intentionally
        monotonic: it may promote a legacy Agent request to an existing local
        path, but never demotes a current local request or activates a planned
        capability that is not ready.
        """
        if self.mode not in {"partial", "on"}:
            return decision
        if decision.kind != RouteKind.CDE_SEARCH or decision.search is None:
            return decision
        if not plan.ready:
            return decision
        if plan.execution not in {
            ExecutionPlan.LOCAL_SQL,
            ExecutionPlan.LOCAL_GUARDED,
            ExecutionPlan.LOCAL_EVIDENCE_FILTER,
        }:
            return decision
        if decision.fast_eligible:
            return decision
        return replace(
            decision,
            reason=(
                f"capability_{self.mode}_promotion:"
                f"{plan.task.value}:{plan.execution.value}"
            ),
            fast_eligible=True,
        )

    def _task(
        self,
        text: str,
        search: SearchParameters,
        deterministic_reasons: tuple[str, ...] = (),
    ) -> TaskKind:
        if _QUESTIONNAIRE.search(text):
            return TaskKind.GENERATE_QUESTIONNAIRE
        if _OPEN_ANALYSIS.search(text):
            return TaskKind.OPEN_ANALYSIS
        if _CRITERIA.search(text) or "criteria_request" in deterministic_reasons:
            return TaskKind.CHECK_CRITERIA
        if search.precise_eligibility or _ELIGIBILITY.search(text):
            return TaskKind.ASSESS_PATIENT
        if search.request_mode == RequestMode.CRITERIA_ANALYSIS:
            return TaskKind.CHECK_CRITERIA
        if "site_analysis" in deterministic_reasons:
            return TaskKind.COMPARE_RECORDS
        if "company_or_drug_relationship" in deterministic_reasons:
            return TaskKind.OPEN_ANALYSIS
        if _COMPARE.search(text):
            return TaskKind.COMPARE_RECORDS
        if search.request_mode == RequestMode.PATIENT_MATCHING:
            return TaskKind.ASSESS_PATIENT
        return TaskKind.FIND_TRIALS

    @staticmethod
    def _deterministic_complexity_reasons(
        decision: RouteDecision | None,
    ) -> tuple[str, ...]:
        if decision is None or decision.fast_eligible:
            return ()
        reason = str(decision.reason or "")
        if not reason.startswith("complex_") or ":" not in reason:
            return ()
        return tuple(
            dict.fromkeys(
                value.strip()
                for value in reason.split(":", 1)[1].split(",")
                if value.strip()
            )
        )

    @staticmethod
    def _missing_biomarker_anchors(
        text: str,
        biomarkers: tuple[str, ...],
    ) -> tuple[str, ...]:
        anchors: list[str] = []
        for pattern in _BIOMARKER_ANCHOR_PATTERNS:
            for match in pattern.finditer(text or ""):
                value = match.group(1).upper()
                if value not in anchors:
                    anchors.append(value)
        normalized_markers = re.sub(
            r"[^A-Za-z0-9]",
            "",
            " ".join(biomarkers),
        ).upper()
        return tuple(
            value for value in anchors
            if re.sub(r"[^A-Za-z0-9]", "", value).upper() not in normalized_markers
        )

    def _constraints(self, search: SearchParameters) -> tuple[QueryConstraint, ...]:
        values: list[QueryConstraint] = []
        self._add(values, "disease", search.disease, ExecutionCapability.SQL_FILTER)
        self._add(values, "biomarkers", search.biomarkers, ExecutionCapability.SQL_FILTER, "contains_any")
        self._add(values, "sponsors", search.sponsors, ExecutionCapability.SQL_FILTER, "contains_any")
        self._add(values, "site_names", search.site_names, ExecutionCapability.SQL_FILTER, "contains_any")
        self._add(values, "project_codes", search.project_codes, ExecutionCapability.SQL_FILTER, "contains_any")
        self._add(values, "city", search.city, ExecutionCapability.SQL_FILTER)
        self._add(values, "province", search.province, ExecutionCapability.SQL_FILTER)
        self._add(values, "region", search.region, ExecutionCapability.SQL_FILTER)
        self._add(values, "lookback_months", search.lookback_months, ExecutionCapability.SQL_FILTER, "within")
        self._add(values, "trial_phases", search.trial_phases, ExecutionCapability.SQL_FILTER, "contains_any")
        self._add(values, "age", search.age, ExecutionCapability.ELIGIBILITY_GUARD)
        self._add(values, "age_min", search.age_min, ExecutionCapability.ELIGIBILITY_GUARD, "gte")
        self._add(values, "age_max", search.age_max, ExecutionCapability.ELIGIBILITY_GUARD, "lte")
        if search.sex not in {"", "unknown"}:
            self._add(values, "sex", search.sex, ExecutionCapability.ELIGIBILITY_GUARD)
        for field, value in (
            ("scores", search.scores),
            ("treatment_line", search.treatment_line),
            ("prior_treatments", search.prior_treatments),
            ("disease_stage", search.disease_stage),
            ("disease_statuses", search.disease_statuses),
            ("comorbidities", search.comorbidities),
            ("organ_function_findings", search.organ_function_findings),
            ("reproductive_statuses", search.reproductive_statuses),
            ("allergies", search.allergies),
        ):
            self._add(values, field, value, ExecutionCapability.EVIDENCE_FILTER, "evidence_match")
        return tuple(values)

    @staticmethod
    def _add(
        target: list[QueryConstraint],
        field: str,
        value: Any,
        capability: ExecutionCapability,
        operator: str = "equals",
    ) -> None:
        if value is None or value == "" or value == ():
            return
        target.append(QueryConstraint(field, operator, value, capability))

    def _agent_plan(self, task: TaskKind, decision: RouteDecision, reason: str) -> RequestPlan:
        return RequestPlan(
            task=task,
            execution=ExecutionPlan.MAIN_AGENT,
            reasons=(reason,),
            legacy_fast_eligible=decision.fast_eligible,
            mode=self.mode,
        )
