from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

from angela_orchestration.eligibility import EligibilityGuard, filter_guarded_results
from angela_orchestration.fast_path import _months_ago
from angela_orchestration.models import SearchParameters
from angela_orchestration.patient_facts import PatientFacts, PatientFactsBuilder

from .contracts import EvaluationSuite
from .database import connect_read_only, snapshot_database
from .metrics import safe_ratio, threshold_status
from .oracle import IndependentRetrievalOracle


TUPLE_SEARCH_FIELDS = {
    "trial_phases", "prior_treatments", "biomarkers", "scores", "comorbidities",
    "disease_statuses", "organ_function_findings", "reproductive_statuses", "allergies",
}
RECRUITABLE_STATUSES = {"recruiting", "not_yet_recruiting"}
SOURCE_FIELDS = (
    "title", "indication", "phase", "status", "first_publication_date", "detail_url",
)


def _search_parameters(payload: dict[str, Any]) -> SearchParameters:
    values = dict(payload)
    for field in TUPLE_SEARCH_FIELDS:
        if field in values:
            values[field] = tuple(values[field] or ())
    return SearchParameters(**values)


def _facts_projection(facts: PatientFacts) -> dict[str, Any]:
    return {
        "birth_year": facts.birth_year.value,
        "age": facts.age.exact,
        "age_min": facts.age.minimum_years,
        "age_max": facts.age.maximum_years,
        "sex": facts.sex.value,
        "diseases": list(facts.diseases),
        "canonical_diseases": list(facts.canonical_diseases),
        "disease_duration_months": facts.disease_duration_months.value,
        "disease_stage": facts.disease_stage.value,
        "treatment_line": facts.treatment_line.value,
        "prior_treatments": list(facts.prior_treatments),
        "biomarkers": list(facts.biomarkers),
        "scores": list(facts.scores),
        "comorbidities": list(facts.comorbidities),
        "disease_statuses": list(facts.disease_statuses),
        "organ_function_findings": list(facts.organ_function_findings),
        "reproductive_statuses": list(facts.reproductive_statuses),
        "allergies": list(facts.allergies),
        "city": facts.city,
        "region": facts.region,
        "lookback_months": facts.lookback_months,
        "requires_clarification": facts.requires_clarification,
        "conflict_codes": [item.code for item in facts.conflicts],
    }


def _fact_value_matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, list):
        actual_values = actual if isinstance(actual, list) else []
        folded = [str(value).casefold() for value in actual_values]
        return all(
            any(str(value).casefold() in item or item in str(value).casefold() for item in folded)
            for value in expected
        )
    return expected == actual


def _criterion_excerpt(trial: dict[str, Any], kind: str, limit: int = 1200) -> str:
    values = trial.get(f"{kind}_criteria") or []
    text = "\n".join(
        str(item.get("raw_text") or "") if isinstance(item, dict) else str(item)
        for item in values
    ).strip()
    return text[:limit]


class QualityEvaluator:
    """Run deterministic CDE quality checks against a read-only formal database."""

    def __init__(self, *, project_root: str | Path, database_path: str | Path):
        self.project_root = Path(project_root).expanduser().resolve()
        self.database_path = Path(database_path).expanduser().resolve()
        self.concepts_path = self.project_root / "cde-data/config/disease_concepts.json"
        data_root = self.project_root / "cde-data"
        if str(data_root) not in sys.path:
            sys.path.insert(0, str(data_root))

    def run(
        self,
        suite: EvaluationSuite,
        *,
        annotations: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        annotations = annotations or {}
        snapshot = snapshot_database(self.database_path)
        reference_date = date.fromisoformat(suite.reference_date) if suite.reference_date else date.today()
        cases = [
            self._run_case(case, annotations.get(case.case_id) or {}, reference_date)
            for case in suite.cases
        ]
        aggregate = self._aggregate(cases, suite.thresholds, snapshot.as_dict())
        return {
            "schema_version": 1,
            "suite_id": suite.suite_id,
            "description": suite.description,
            "reference_date": reference_date.isoformat(),
            "database_snapshot": snapshot.as_dict(),
            "thresholds": suite.thresholds,
            "aggregate": aggregate,
            "cases": cases,
        }

    def _run_case(
        self, case: Any, annotation: dict[str, Any], reference_date: date
    ) -> dict[str, Any]:
        from cde_store.contracts import PATIENT_DEFAULT_STATUSES
        from cde_store.local_query import DiseaseConcepts, LocalTrialQuery, LocalTrialQueryService

        search = _search_parameters(case.search)
        facts = PatientFactsBuilder().build(case.prompt, search, reference_date=reference_date)
        connection = connect_read_only(self.database_path)
        try:
            service = LocalTrialQueryService(connection, DiseaseConcepts(self.concepts_path))
            date_from = _months_ago(reference_date, search.lookback_months) if search.lookback_months else None
            query_age = search.age if search.age is not None else facts.age.exact
            query_age_min = search.age_min
            query_age_max = search.age_max
            if query_age is None:
                if query_age_min is None:
                    query_age_min = facts.age.minimum_years
                if query_age_max is None:
                    query_age_max = facts.age.maximum_years
            raw = service.search(LocalTrialQuery(
                disease=search.disease,
                age=query_age,
                age_min=query_age_min,
                age_max=query_age_max,
                sex=search.sex,
                city=search.city,
                region=search.region,
                date_from=date_from,
                recruitment_statuses=PATIENT_DEFAULT_STATUSES,
                limit=20,
                blood_type=search.blood_type,
                trial_phases=search.trial_phases,
                agent_canonical_disease=search.canonical_disease,
                preserve_specificity=search.preserve_specificity,
            ))
            oracle = IndependentRetrievalOracle(connection, self.concepts_path).search(
                search,
                reference_date=reference_date,
                patient_age_min=facts.age.minimum_years,
                patient_age_max=facts.age.maximum_years,
                patient_sex=str(facts.sex.value or "unknown"),
            )
            guard = EligibilityGuard()
            evaluations: dict[str, Any] = {}
            candidates: list[dict[str, Any]] = []
            for item in raw.get("results") or []:
                ctr = str(item.get("registration_no") or "").upper()
                trial = service.get_trial(ctr)
                evaluation = guard.evaluate(facts, trial) if trial is not None else None
                if evaluation is not None:
                    evaluations[ctr] = evaluation
                candidates.append({
                    "registration_no": ctr,
                    "title": item.get("public_title") or item.get("professional_title"),
                    "indication": item.get("indication_raw"),
                    "phase": item.get("trial_phase_raw"),
                    "status": item.get("recruitment_status_raw") or item.get("overall_status_raw"),
                    "first_publication_date": item.get("first_publication_date"),
                    "detail_url": item.get("detail_url"),
                    "match_evidence": item.get("match_evidence") or [],
                    "guard": evaluation.as_dict() if evaluation is not None else None,
                    "inclusion_excerpt": _criterion_excerpt(trial or {}, "inclusion"),
                    "exclusion_excerpt": _criterion_excerpt(trial or {}, "exclusion"),
                })
            guarded, guard_counts = filter_guarded_results(
                list(raw.get("results") or []), evaluations, limit=search.max_results
            )
            returned_ids = [str(item.get("registration_no") or "").upper() for item in guarded]
            returned_explicit_conflicts = sum(
                bool(
                    evaluations.get(ctr)
                    and evaluations[ctr].disposition.value == "EXPLICIT_CONFLICT"
                )
                for ctr in returned_ids
            )
            status_rows = connection.execute(
                "SELECT registration_no,recruitment_status_normalized FROM trials WHERE registration_no IN ("
                + ",".join("?" for _ in returned_ids) + ")",
                returned_ids,
            ).fetchall() if returned_ids else []
        finally:
            connection.close()

        statuses = {str(row["registration_no"]): str(row["recruitment_status_normalized"]) for row in status_rows}
        facts_actual = _facts_projection(facts)
        fact_checks = {
            key: {
                "expected": expected,
                "actual": facts_actual.get(key),
                "match": _fact_value_matches(expected, facts_actual.get(key)),
            }
            for key, expected in case.expected_facts.items()
        }
        oracle_limit = min(search.max_results, len(oracle.final_ranked_ids))
        oracle_top_ids = list(oracle.final_ranked_ids[:oracle_limit])
        returned_set = set(returned_ids)
        oracle_final_set = set(oracle.final_ranked_ids)
        relevant_returned = sum(ctr in oracle_final_set for ctr in returned_ids)
        recalled_at_k = sum(ctr in returned_set for ctr in oracle_top_ids)

        candidate_by_ctr = {item["registration_no"]: item for item in candidates}
        source_field_checks: list[dict[str, Any]] = []
        for ctr in returned_ids:
            candidate = candidate_by_ctr.get(ctr) or {}
            source = oracle.records.get(ctr) or {}
            expected_values = {
                "title": source.get("public_title") or source.get("professional_title"),
                "indication": source.get("indication_raw"),
                "phase": source.get("trial_phase_raw"),
                "status": source.get("recruitment_status_raw") or source.get("overall_status_raw"),
                "first_publication_date": source.get("first_publication_date"),
                "detail_url": source.get("detail_url"),
            }
            for field in SOURCE_FIELDS:
                actual = candidate.get(field)
                expected = expected_values[field]
                source_field_checks.append({
                    "registration_no": ctr,
                    "field": field,
                    "expected": expected,
                    "actual": actual,
                    "match": bool(source) and actual == expected,
                })
        guard_checks: list[dict[str, Any]] = []
        for ctr, expected in dict(annotation.get("guard_labels") or {}).items():
            predicted = evaluations.get(ctr)
            predicted_dict = predicted.as_dict() if predicted is not None else {}
            expected_disposition = expected.get("disposition")
            expected_findings = dict(expected.get("findings") or {})
            predicted_findings = {
                item.get("category"): item.get("state")
                for item in predicted_dict.get("findings") or []
            }
            checks = [
                predicted_dict.get("disposition") == expected_disposition
                if expected_disposition else True,
                *[
                    predicted_findings.get(category) == state
                    for category, state in expected_findings.items()
                ],
            ]
            guard_checks.append({
                "registration_no": ctr,
                "expected_disposition": expected_disposition,
                "actual_disposition": predicted_dict.get("disposition"),
                "expected_findings": expected_findings,
                "actual_findings": predicted_findings,
                "match": all(checks),
            })
        return {
            "case_id": case.case_id,
            "prompt": case.prompt,
            "search": case.search,
            "review_status": annotation.get("review_status", "pending"),
            "returned_ids": returned_ids,
            "raw_total_count": int(raw.get("total_count") or 0),
            "oracle": {
                "raw_total_count": oracle.raw_total_count,
                "top_k_ids": oracle_top_ids,
                "final_match_count": len(oracle.final_ranked_ids),
            },
            "candidate_pool_size": len(candidates),
            "candidates": candidates,
            "guard_summary": guard_counts,
            "machine_checks": {
                "returned_count_within_limit": len(returned_ids) <= search.max_results,
                "returned_ids_unique": len(returned_ids) == len(set(returned_ids)),
                "all_returned_ctr_exist": len(statuses) == len(set(returned_ids)),
                "all_returned_statuses_recruitable": all(
                    value in RECRUITABLE_STATUSES for value in statuses.values()
                ),
                "explicit_conflict_returned": returned_explicit_conflicts,
                "explicit_conflict_candidates_filtered": guard_counts.get("EXPLICIT_CONFLICT", 0),
            },
            "facts": {"actual": facts_actual, "checks": fact_checks},
            "retrieval": {
                "relevant_returned": relevant_returned,
                "irrelevant_returned": len(returned_ids) - relevant_returned,
                "oracle_top_k_recalled": recalled_at_k,
                "oracle_top_k_total": oracle_limit,
                "precision": safe_ratio(relevant_returned, len(returned_ids)),
                "recall_at_k": safe_ratio(recalled_at_k, oracle_limit),
                "unexpected_returned_ids": [
                    ctr for ctr in returned_ids if ctr not in oracle_final_set
                ],
                "missed_top_k_ids": [ctr for ctr in oracle_top_ids if ctr not in returned_set],
            },
            "source_field_checks": source_field_checks,
            "guard_checks": guard_checks,
            "notes": case.notes,
        }

    @staticmethod
    def _aggregate(
        cases: list[dict[str, Any]],
        thresholds: dict[str, float],
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = snapshot or {}
        returned = sum(len(case["returned_ids"]) for case in cases)
        reviewed = [case for case in cases if case["review_status"] == "complete"]
        relevant_returned = sum(case["retrieval"]["relevant_returned"] for case in cases)
        oracle_top_k_recalled = sum(case["retrieval"]["oracle_top_k_recalled"] for case in cases)
        oracle_top_k_total = sum(case["retrieval"]["oracle_top_k_total"] for case in cases)
        fact_checks = [check for case in cases for check in case["facts"]["checks"].values()]
        source_field_checks = [check for case in cases for check in case["source_field_checks"]]
        machine = [case["machine_checks"] for case in cases]
        ctr_existing = sum(
            len(case["returned_ids"]) if check["all_returned_ctr_exist"] else 0
            for case, check in zip(cases, machine)
        )
        status_compliant = sum(
            len(case["returned_ids"]) if check["all_returned_statuses_recruitable"] else 0
            for case, check in zip(cases, machine)
        )
        values = {
            "reviewed_case_count": len(reviewed),
            "total_case_count": len(cases),
            "returned_trial_count": returned,
            "ctr_existence_rate": safe_ratio(ctr_existing, returned),
            "status_compliance_rate": safe_ratio(status_compliant, returned),
            "fact_trace_rate": safe_ratio(sum(bool(item["match"]) for item in fact_checks), len(fact_checks)),
            "retrieval_precision": safe_ratio(relevant_returned, returned),
            "retrieval_recall_at_k": safe_ratio(oracle_top_k_recalled, oracle_top_k_total),
            "raw_total_count_accuracy": safe_ratio(
                sum(case["raw_total_count"] == case["oracle"]["raw_total_count"] for case in cases),
                len(cases),
            ),
            "source_field_fidelity": safe_ratio(
                sum(bool(item["match"]) for item in source_field_checks),
                len(source_field_checks),
            ),
            "explicit_conflict_leakage_rate": safe_ratio(
                sum(int(item["explicit_conflict_returned"]) for item in machine), returned
            ),
            "database_criteria_coverage": safe_ratio(
                int(snapshot.get("criteria_trial_count") or 0),
                int(snapshot.get("active_trial_count") or 0),
            ),
        }
        gates: dict[str, dict[str, Any]] = {}
        for metric, threshold in thresholds.items():
            maximum = metric.endswith("_max")
            value_key = metric[:-4] if maximum else metric
            value = values.get(value_key)
            if maximum:
                status = "NOT_READY" if value is None else ("PASS" if value <= threshold else "FAIL")
            else:
                status = threshold_status(value, threshold)
            gates[metric] = {"value": value, "threshold": threshold, "status": status}
        statuses = {item["status"] for item in gates.values()}
        overall = "FAIL" if "FAIL" in statuses else ("NOT_READY" if "NOT_READY" in statuses else "PASS")
        return {"metrics": values, "gates": gates, "overall_status": overall}


def annotation_bundle(report: dict[str, Any]) -> dict[str, Any]:
    cases = []
    for case in report["cases"]:
        candidates = []
        for item in case["candidates"]:
            candidates.append({
                **item,
                "relevant": None,
                "review_notes": "",
            })
        cases.append({
            "case_id": case["case_id"],
            "prompt": case["prompt"],
            "review_status": "pending",
            "reviewer": None,
            "reviewed_at": None,
            "candidates": candidates,
        })
    return {
        "schema_version": 1,
        "suite_id": report["suite_id"],
        "database_snapshot": report["database_snapshot"],
        "instructions": {
            "purpose": "可选的检索相关性抽查，不参与自动验收放行。",
            "relevant": "只判断记录是否符合查询条件；不判断患者最终能否入组。",
            "review_status": "如进行抽查，完成后可改为complete并填写审计信息。",
        },
        "cases": cases,
    }
