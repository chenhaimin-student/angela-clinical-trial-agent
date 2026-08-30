from __future__ import annotations

import calendar
import re
import sqlite3
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

from .config import OrchestrationConfig
from .evidence_filter import (
    ConstraintEvidenceFilter,
    apply_constraint_reviews,
    requested_evidence_fields,
)
from .eligibility import EligibilityGuard, filter_guarded_results
from .models import FastPathResponse, RequestMode, SearchParameters
from .patient_facts import PatientFacts, PatientFactsBuilder
from .renderer import render_search


_ASCII_MEDICAL_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])([A-Z][A-Z0-9-]{2,11})(?![A-Za-z0-9])"
)


def _ascii_disease_retry_terms(source_text: str, search: SearchParameters) -> tuple[str, ...]:
    """Return source tokens eligible for an evidence-backed zero-result retry."""
    from .normalization import (
        extract_biomarker_terms,
        extract_project_codes,
        is_biomarker_term,
    )

    biomarkers = {value.casefold() for value in extract_biomarker_terms(source_text)}
    project_codes = {value.casefold() for value in extract_project_codes(source_text)}
    current = (search.disease or "").casefold()
    candidates = []
    for match in _ASCII_MEDICAL_TOKEN.finditer(source_text or ""):
        value = match.group(1).upper()
        folded = value.casefold()
        if (
            folded == current
            or folded in biomarkers
            or folded in project_codes
            or is_biomarker_term(value)
            or re.fullmatch(r"(?:I|II|III|IV)", value)
        ):
            continue
        candidates.append(value)
    return tuple(dict.fromkeys(candidates[:6]))


def _months_ago(today: date, months: int) -> date:
    month_index = today.year * 12 + today.month - 1 - months
    year, zero_month = divmod(month_index, 12)
    month = zero_month + 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


class LocalCdeFastPath:
    def __init__(self, config: OrchestrationConfig):
        self.config = config
        data_root = config.project_root / "cde-data"
        if str(data_root) not in sys.path:
            sys.path.insert(0, str(data_root))

    def search(
        self,
        search: SearchParameters,
        *,
        patient_facts: PatientFacts | None = None,
        source_text: str = "",
    ) -> FastPathResponse:
        from cde_store.contracts import PATIENT_DEFAULT_STATUSES
        from cde_store.local_query import DiseaseConcepts, LocalTrialQuery, LocalTrialQueryService

        if not self.config.database_path.is_file():
            raise RuntimeError(f"CDE database unavailable: {self.config.database_path}")
        date_from = _months_ago(date.today(), search.lookback_months) if search.lookback_months else None
        information_retrieval = search.request_mode == RequestMode.INFORMATION_RETRIEVAL
        evidence_fields = requested_evidence_fields(search)
        evidence_filter_active = bool(information_retrieval and evidence_fields)
        facts = patient_facts or (
            None if information_retrieval else PatientFactsBuilder().build(source_text, search)
        )
        query_age = search.age if search.age is not None else (facts.age.exact if facts else None)
        query_age_min = search.age_min
        query_age_max = search.age_max
        if query_age is None:
            if query_age_min is None:
                query_age_min = facts.age.minimum_years if facts else None
            if query_age_max is None:
                query_age_max = facts.age.maximum_years if facts else None
        connection = sqlite3.connect(
            f"file:{self.config.database_path}?mode=ro", uri=True, timeout=5
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            service = LocalTrialQueryService(connection, DiseaseConcepts(self.config.concepts_path))
            candidate_limit = min(20, max(search.max_results, search.max_results * 4))
            def run_query(disease: str, canonical_disease: str | None) -> dict:
                return service.search(LocalTrialQuery(
                    disease=disease,
                    age=query_age,
                    age_min=query_age_min,
                    age_max=query_age_max,
                    sex=search.sex,
                    city=search.city,
                    province=search.province,
                    region=search.region,
                    date_from=date_from,
                    recruitment_statuses=PATIENT_DEFAULT_STATUSES,
                    limit=candidate_limit,
                    blood_type=search.blood_type,
                    trial_phases=search.trial_phases,
                    required_biomarkers=search.biomarkers,
                    sponsors=search.sponsors,
                    site_names=search.site_names,
                    project_codes=search.project_codes,
                    agent_canonical_disease=canonical_disease,
                    preserve_specificity=search.preserve_specificity,
                ))

            result = run_query(search.disease, search.canonical_disease)
            if not result.get("results") and source_text:
                for retry_term in _ascii_disease_retry_terms(source_text, search):
                    retried = run_query(retry_term, retry_term)
                    primary_evidence = any(
                        any(
                            item.get("field") in {
                                "indication_raw", "public_title", "professional_title"
                            }
                            for item in record.get("match_evidence") or ()
                        )
                        for record in retried.get("results") or ()
                    )
                    if not primary_evidence:
                        continue
                    search = replace(
                        search,
                        disease=retry_term,
                        raw_disease=retry_term,
                        canonical_disease=retry_term,
                    )
                    result = retried
                    if not information_retrieval:
                        facts = PatientFactsBuilder().build(source_text, search)
                    break
            evaluations = {}
            evidence_reviews = {}
            if evidence_filter_active:
                evidence_filter = ConstraintEvidenceFilter()
                for item in result.get("results") or []:
                    ctr = str(item.get("registration_no") or "")
                    trial = service.get_trial(ctr)
                    if trial is not None:
                        evidence_reviews[ctr] = evidence_filter.evaluate(search, trial)
                guarded, guard_counts = apply_constraint_reviews(
                    list(result.get("results") or []),
                    evidence_reviews,
                    limit=search.max_results,
                )
            elif information_retrieval:
                guarded = list(result.get("results") or [])[:search.max_results]
                guard_counts = {}
            else:
                guard = EligibilityGuard()
                for item in result.get("results") or []:
                    ctr = str(item.get("registration_no") or "")
                    trial = service.get_trial(ctr)
                    if trial is not None:
                        evaluations[ctr] = guard.evaluate(facts, trial)
                guarded, guard_counts = filter_guarded_results(
                    list(result.get("results") or []),
                    evaluations,
                    limit=search.max_results,
                )
            result["results"] = guarded
            result["returned_count"] = len(guarded)
            result["guard_summary"] = guard_counts
            result["guard_evaluated_count"] = len(evaluations) + len(evidence_reviews)
            result["guard_scope"] = (
                "local_constraint_evidence"
                if evidence_filter_active
                else "information_retrieval" if information_retrieval
                else "top_candidates"
            )
            result["evidence_filter_active"] = evidence_filter_active
            result["evidence_filter_fields"] = list(evidence_fields)
            result["patient_facts_schema"] = facts.schema_version if facts else None
        finally:
            connection.close()
        return render_search(result, search)
