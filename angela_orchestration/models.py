from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


class RouteKind(str, Enum):
    GENERAL = "general"
    CDE_SEARCH = "cde_search"
    CDE_DETAIL = "cde_detail"
    CDE_QUESTIONNAIRE = "cde_questionnaire"
    CDE_LIVE_VERIFY = "cde_live_verify"
    PRIVACY_BLOCK = "privacy_block"


class RequestMode(str, Enum):
    INFORMATION_RETRIEVAL = "information_retrieval"
    PATIENT_MATCHING = "patient_matching"
    CRITERIA_ANALYSIS = "criteria_analysis"


@dataclass(frozen=True)
class SearchParameters:
    disease: str = ""
    raw_disease: str | None = None
    canonical_disease: str | None = None
    query_type: str = "disease"
    preserve_specificity: bool = False
    age: int | None = None
    age_min: int | None = None
    age_max: int | None = None
    sex: str = "unknown"
    city: str | None = None
    province: str | None = None
    region: str | None = None
    blood_type: str | None = None
    lookback_months: int | None = None
    trial_phases: tuple[str, ...] = ()
    max_results: int = 10
    birth_year: int | None = None
    disease_duration_months: int | None = None
    disease_stage: str | None = None
    treatment_line: str | None = None
    prior_treatments: tuple[str, ...] = ()
    biomarkers: tuple[str, ...] = ()
    sponsors: tuple[str, ...] = ()
    site_names: tuple[str, ...] = ()
    project_codes: tuple[str, ...] = ()
    scores: tuple[str, ...] = ()
    comorbidities: tuple[str, ...] = ()
    disease_statuses: tuple[str, ...] = ()
    organ_function_findings: tuple[str, ...] = ()
    reproductive_statuses: tuple[str, ...] = ()
    allergies: tuple[str, ...] = ()
    precise_eligibility: bool = False
    request_mode: RequestMode = RequestMode.PATIENT_MATCHING
    locked_fields: tuple[str, ...] = ()
    field_sources: tuple[tuple[str, str], ...] = ()

    def source_for(self, field_name: str) -> str | None:
        return dict(self.field_sources).get(field_name)

    def complexity_reasons(self) -> tuple[str, ...]:
        """Explain why this request needs criteria-aware Agent review."""
        reasons: list[str] = []
        for present, label in (
            (self.scores, "score_or_scale"),
            (self.treatment_line, "treatment_line"),
            (self.prior_treatments, "prior_treatment"),
            (self.disease_stage, "disease_stage"),
            (self.disease_statuses, "disease_status"),
            (self.comorbidities, "comorbidity"),
            (self.organ_function_findings, "organ_function"),
            (self.reproductive_statuses, "reproductive_status"),
            (self.allergies, "allergy"),
            (self.precise_eligibility, "precise_eligibility"),
        ):
            if present:
                reasons.append(label)
        return tuple(reasons)

    def as_query_plan(self) -> dict[str, Any]:
        """Return the auditable plan shared by routing, tools and tests."""
        complexity = self.complexity_reasons()
        return {
            "request_mode": self.request_mode.value,
            "disease": self.disease,
            "canonical_disease": self.canonical_disease,
            "hard_filters": {
                "age": self.age,
                "age_min": self.age_min,
                "age_max": self.age_max,
                "sex": self.sex,
                "city": self.city,
                "province": self.province,
                "region": self.region,
                "lookback_months": self.lookback_months,
                "trial_phases": list(self.trial_phases),
                "sponsors": list(self.sponsors),
                "site_names": list(self.site_names),
                "project_codes": list(self.project_codes),
                "biomarkers": list(self.biomarkers),
            },
            "criteria_constraints": {
                "biomarkers": list(self.biomarkers),
                "scores": list(self.scores),
                "treatment_line": self.treatment_line,
                "prior_treatments": list(self.prior_treatments),
                "disease_stage": self.disease_stage,
                "disease_statuses": list(self.disease_statuses),
                "comorbidities": list(self.comorbidities),
            },
            "complexity_reasons": list(complexity),
            "requires_agent_review": bool(complexity),
            "max_results": self.max_results,
            "field_locks": {
                name: self.source_for(name) or "authoritative"
                for name in self.locked_fields
            },
        }


_LOCKABLE_SEARCH_FIELDS = frozenset({
    "disease", "raw_disease", "canonical_disease", "query_type",
    "preserve_specificity", "age", "age_min", "age_max", "sex", "city",
    "province", "region", "blood_type", "lookback_months", "trial_phases",
    "max_results", "birth_year", "disease_duration_months", "disease_stage",
    "treatment_line", "prior_treatments", "biomarkers", "sponsors",
    "site_names", "project_codes", "scores", "comorbidities",
    "disease_statuses", "organ_function_findings", "reproductive_statuses",
    "allergies", "precise_eligibility", "request_mode",
})


def preserve_locked_search_fields(
    proposed: SearchParameters,
    authoritative: SearchParameters | None,
) -> SearchParameters:
    """Keep database/rule-confirmed slots immutable during model enrichment."""
    if authoritative is None or not authoritative.locked_fields:
        return proposed
    locked = tuple(dict.fromkeys(
        name for name in authoritative.locked_fields
        if name in _LOCKABLE_SEARCH_FIELDS
    ))
    updates = {name: getattr(authoritative, name) for name in locked}
    updates["locked_fields"] = tuple(dict.fromkeys((*proposed.locked_fields, *locked)))
    sources = dict(proposed.field_sources)
    sources.update(dict(authoritative.field_sources))
    updates["field_sources"] = tuple(sorted(sources.items()))
    return replace(proposed, **updates)


@dataclass(frozen=True)
class RouteDecision:
    kind: RouteKind
    confidence: float
    reason: str
    fast_eligible: bool = False
    search: SearchParameters | None = None
    ctr_number: str | None = None


@dataclass(frozen=True)
class FastPathResponse:
    text: str
    total_count: int
    returned_count: int
    result_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdmissionResult:
    lease: Any | None = None
    rejection_text: str | None = None
    queue_position: int = 0
    queue_wait_seconds: float = 0.0
