"""Local MCP tools that expose the de-identified CDE Demo to Hermes."""

from __future__ import annotations

import calendar
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CDE_DEMO_ROOT = PROJECT_ROOT / "cde-demo"
if str(CDE_DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(CDE_DEMO_ROOT))
CDE_DATA_ROOT = PROJECT_ROOT / "cde-data"
if str(CDE_DATA_ROOT) not in sys.path:
    sys.path.insert(0, str(CDE_DATA_ROOT))

from cde_store.contracts import RecruitmentStatus
from cde_store.local_query import (
    REGION_PROVINCES,
    DiseaseConcepts,
    LocalTrialQuery,
    LocalTrialQueryService,
)

from angela_orchestration.eligibility import EligibilityGuard, filter_guarded_results
from angela_orchestration.models import RequestMode, SearchParameters
from angela_orchestration.patient_facts import PatientFactsBuilder

from cde_demo.cde_client import CdeBrowserWorkerClient, CdeWorkerError, WorkerResource
from cde_demo.models import InputPrivacyError, PatientProfile
from cde_demo.parser import CDE_DETAIL_URL, parse_result_page, parse_trial_html
from cde_demo.service import CdeLookupService

mcp = FastMCP("CDE Trial Lookup")

LOCAL_DATABASE_PATH = Path(
    os.getenv(
        "ANGELA_CDE_DATABASE",
        str(PROJECT_ROOT / ".runtime" / "cde_trials.sqlite3"),
    )
).expanduser().resolve()
LOCAL_CONCEPTS_PATH = Path(
    os.getenv(
        "ANGELA_DISEASE_CONCEPTS",
        str(CDE_DATA_ROOT / "config" / "disease_concepts.json"),
    )
).expanduser().resolve()
LOCAL_MAX_RESULTS = 20
_LOCAL_STATUS_BY_NAME = {
    "recruiting": RecruitmentStatus.RECRUITING,
    "not_yet_recruiting": RecruitmentStatus.NOT_YET_RECRUITING,
    "recruitment_complete": RecruitmentStatus.RECRUITMENT_COMPLETE,
    "completed": RecruitmentStatus.COMPLETED,
    "suspended": RecruitmentStatus.SUSPENDED,
    "terminated": RecruitmentStatus.TERMINATED,
    "withdrawn": RecruitmentStatus.WITHDRAWN,
    "unknown": RecruitmentStatus.UNKNOWN,
    "招募中": RecruitmentStatus.RECRUITING,
    "进行中，招募中": RecruitmentStatus.RECRUITING,
    "进行中 招募中": RecruitmentStatus.RECRUITING,
    "尚未招募": RecruitmentStatus.NOT_YET_RECRUITING,
    "进行中，尚未招募": RecruitmentStatus.NOT_YET_RECRUITING,
    "进行中 尚未招募": RecruitmentStatus.NOT_YET_RECRUITING,
    "招募完成": RecruitmentStatus.RECRUITMENT_COMPLETE,
    "已完成": RecruitmentStatus.COMPLETED,
    "暂停": RecruitmentStatus.SUSPENDED,
    "终止": RecruitmentStatus.TERMINATED,
}

FAST_MAX_RESULTS = 5
FAST_SEARCH_PAGES = 1
FAST_CANDIDATE_LIMIT = 10
DEEP_MAX_RESULTS = 10
DEEP_SEARCH_PAGES = 3
DEEP_CANDIDATE_LIMIT = 40

_CTR_PATTERN = re.compile(r"^CTR\d{6,}$", re.IGNORECASE)
_DETAIL_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)
_CDE_DETAIL_HOSTS = {"www.chinadrugtrials.org.cn", "chinadrugtrials.org.cn"}


def _parse_iso_date(value: str | None, label: str) -> date | None:
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD format") from exc


def _local_connection() -> sqlite3.Connection:
    """Open the configured local database read-only so MCP cannot mutate it."""
    if not LOCAL_DATABASE_PATH.is_file():
        raise RuntimeError(f"local CDE database is unavailable: {LOCAL_DATABASE_PATH}")
    connection = sqlite3.connect(
        f"file:{LOCAL_DATABASE_PATH}?mode=ro", uri=True, timeout=5
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _local_service(connection: sqlite3.Connection) -> LocalTrialQueryService:
    return LocalTrialQueryService(connection, DiseaseConcepts(LOCAL_CONCEPTS_PATH))


def _normalize_local_statuses(values: list[str] | None) -> tuple[RecruitmentStatus, ...]:
    requested = values or ["recruiting", "not_yet_recruiting"]
    try:
        return tuple(dict.fromkeys(_LOCAL_STATUS_BY_NAME[value.strip()] for value in requested))
    except KeyError as exc:
        allowed = ", ".join(_LOCAL_STATUS_BY_NAME)
        raise ValueError(f"unknown recruitment status {exc.args[0]!r}; allowed: {allowed}") from exc


def _compact_search_response(result: dict[str, Any]) -> dict[str, Any]:
    """Bound MCP output while preserving the evidence needed for a safe answer."""
    compact = {
        key: result.get(key)
        for key in (
            "total_count", "returned_count", "excluded_counts", "warnings",
            "data_source", "requires_live_verification",
        )
    }
    public_fields = (
        "registration_no", "detail_url", "public_title", "indication_raw",
        "drug_name_raw", "trial_phase_raw", "trial_scope_raw",
        "overall_status_raw", "recruitment_status_raw", "first_publication_date",
        "source_update_date", "status_changed_at", "last_verified_at",
        "match_score", "age_assessment", "sex_assessment", "needs_confirmation",
        "city_match", "eligibility_guard",
        "constraint_evidence",
    )
    compact_results = []
    for source in result.get("results", []):
        item = {key: source.get(key) for key in public_fields if key in source}
        if (result.get("query") or {}).get("sponsors"):
            item["sponsor_name_raw"] = source.get("sponsor_name_raw")
        evidence = []
        seen = set()
        for entry in source.get("match_evidence", []):
            key = (entry.get("term"), entry.get("field"))
            if key in seen:
                continue
            seen.add(key)
            evidence.append(entry)
            if len(evidence) == 5:
                break
        item["match_evidence"] = evidence
        item["exclusion_risk_evidence"] = source.get("exclusion_risk_evidence", [])[:2]
        compact_results.append(item)
    compact["results"] = compact_results
    query = dict(result.get("query", {}))
    query["expanded_terms"] = query.get("expanded_terms", [])[:12]
    compact["query"] = query
    return compact


def _parse_trial_reference(trial_reference: str) -> tuple[str | None, str | None]:
    """Accept only a CDE registration number or a CDE detail-page URL."""
    if not isinstance(trial_reference, str) or not trial_reference.strip():
        raise ValueError("trial_reference must be a non-empty CTR number or CDE detail URL")
    reference = trial_reference.strip()
    if _CTR_PATTERN.fullmatch(reference):
        return reference.upper(), None

    parsed = urlparse(reference)
    detail_id = parse_qs(parsed.query).get("id", [""])[0]
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in _CDE_DETAIL_HOSTS
        or not parsed.path.endswith("/clinicaltrials.searchlistdetail.dhtml")
        or not _DETAIL_ID_PATTERN.fullmatch(detail_id)
    ):
        raise ValueError(
            "trial_reference must be a CTR registration number or a CDE detail URL from chinadrugtrials.org.cn"
        )
    return None, detail_id.lower()


def _load_cde_trial(trial_reference: str):
    """Load one public CDE detail page, without fetching user-supplied domains."""
    registration_no, detail_id = _parse_trial_reference(trial_reference)
    client = CdeBrowserWorkerClient()
    job_id = f"cde-trial-detail-{uuid4().hex}"
    source_url: str

    if registration_no:
        search_response = client.fetch(
            job_id,
            f"{job_id}-registration-search",
            [WorkerResource(kind="result_page", query_term=registration_no, page=1)],
        )
        records = parse_result_page(search_response[0]["content"])
        record = next(
            (item for item in records if item.registration_no.upper() == registration_no),
            None,
        )
        if record is None:
            raise ValueError(f"CDE registration number {registration_no} was not found")
        detail_id = record.detail_id
        source_url = record.detail_url
    else:
        source_url = CDE_DETAIL_URL.format(detail_id=detail_id)

    detail_response = client.fetch(
        job_id,
        f"{job_id}-detail",
        [WorkerResource(kind="detail", detail_id=detail_id)],
    )
    trial = parse_trial_html(detail_response[0]["content"], source_url)
    if registration_no and trial.registration_no.upper() != registration_no:
        raise ValueError("CDE detail page registration number did not match the requested registration number")
    return trial


def _trial_questionnaire_source(trial: Any) -> dict[str, Any]:
    """Return public source material and strict generation limits for the agent."""
    criteria_available = bool(trial.inclusion_criteria or trial.exclusion_criteria)
    return {
        "lookup_status": "verified" if criteria_available else "criteria_not_available",
        "source": "中国药物临床试验登记与信息公示平台公开登记信息",
        "trial": {
            "registration_no": trial.registration_no,
            "public_title": trial.public_title,
            "professional_title": trial.professional_title,
            "indication": trial.indication,
            "drug_name": trial.drug_name,
            "trial_phase": trial.trial_phase,
            "status": trial.status,
            "detail_url": trial.detail_url,
        },
        "criteria_for_questionnaire": {
            "inclusion_criteria": trial.inclusion_criteria,
            "exclusion_criteria": trial.exclusion_criteria,
        },
        "agent_response_instruction": (
            "仅基于上述同一条试验的公开入选/排除标准生成不超过10条通俗问题。"
            "每题请用户回答“是/否/不确定”，并说明是需要确认、可能冲突还是信息缺失。"
            "不要混入其他疾病或通用肿瘤问卷；如果某项标准未出现在来源中，不得编造。"
            "不得给出入组结论、诊断或治疗建议；最终由研究医生和研究中心确认。"
        ),
        "disclaimer": "这是公开登记信息的初步自查辅助，不构成医疗建议或临床试验入组结论。",
    }


def calendar_months_ago(today: date, months: int) -> date:
    """Return the same day N calendar months earlier, clamped if necessary."""
    if not 1 <= months <= 24:
        raise ValueError("lookback_months must be between 1 and 24")
    month_index = today.year * 12 + today.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def normalize_terms(values: list[str] | None, label: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{label} must be a list of non-empty strings")
    terms = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
    if len(terms) > 8:
        raise ValueError(f"{label} may contain at most 8 terms")
    return terms


def normalize_optional_text(value: str | None, label: str) -> str | None:
    """Treat a model-produced empty optional string as an omitted value."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string when provided")
    return value.strip() or None


def normalize_optional_birth_year(
    value: int | None,
    *,
    age: int | None = None,
    age_min: int | None = None,
    age_max: int | None = None,
) -> int | None:
    """Keep birth year only when no stronger age fact is already supplied.

    PatientFacts validates user-provided age/birth-year contradictions before an
    agent request can reach this tool.  The model must therefore not be allowed
    to re-introduce an inferred year alongside an authoritative age interval.
    """
    if any(candidate is not None for candidate in (age, age_min, age_max)):
        return None
    if value in (None, 0):
        return None
    if not isinstance(value, int) or not 1900 <= value <= date.today().year:
        raise ValueError("birth_year must be between 1900 and the current year")
    return value


def _build_exploration_context(profile: PatientProfile, mode: str) -> dict[str, Any]:
    missing_information: list[str] = []
    if not profile.pathology_terms:
        missing_information.append("\u75c5\u7406\u5206\u578b\u6216\u5173\u952e\u6807\u5fd7\u7269\uff08\u5982\u679c\u5df2\u77e5\uff09")
    if not profile.prior_treatments:
        missing_information.append("\u65e2\u5f80\u6cbb\u7597\u60c5\u51b5\uff08\u5982\u679c\u613f\u610f\u8865\u5145\uff09")
    if not profile.region:
        missing_information.append("\u6240\u5728\u5730\u6216\u673a\u6784\u53ef\u53ca\u6027\uff08\u5982\u679c\u9700\u8981\u7b5b\u9009\u672c\u5730\u8bd5\u9a8c\uff09")
    return {
        "search_mode": mode,
        "match_score_explanation": (
            "\u5339\u914d\u5206\u4ec5\u662f\u516c\u5f00\u767b\u8bb0\u6587\u672c\u7684\u521d\u6b65\u89c4\u5219\u5206\u3002"
            "\u75be\u75c5/\u75c5\u7406\u5173\u952e\u8bcd\u6bcf\u547d\u4e2d\u4e00\u9879 +3\uff0c"
            "\u6027\u522b\u548c\u5e74\u9f84\u660e\u786e\u547d\u4e2d\u5404 +1\u3002"
            "\u8fd9\u4e0d\u4ee3\u8868\u533b\u5b66\u5165\u7ec4\u6982\u7387\u6216\u5165\u7ec4\u7ed3\u8bba\u3002"
        ),
        "agent_response_instruction": (
            "\u8bf7\u5148\u6e05\u695a\u8bf4\u660e\u8fd9\u662f\u516c\u5f00\u8bd5\u9a8c\u767b\u8bb0\u7684\u521d\u6b65\u7b5b\u9009\uff0c"
            "\u518d\u7528\u4e00\u53e5\u7b80\u77ed\u5f15\u5bfc\u9080\u8bf7\u7528\u6237\u7ee7\u7eed\u6df1\u67e5\u3001\u89e3\u91ca\u5339\u914d\u4f9d\u636e\u6216\u8865\u5145\u975e\u8eab\u4efd\u4fe1\u606f\u3002"
        ),
        "suggested_information_to_refine": missing_information,
        "demo_follow_up_options": [
            "\u7ee7\u7eed\u6df1\u67e5\u66f4\u591a\u516c\u5f00\u8bd5\u9a8c\u8bb0\u5f55",
            "\u89e3\u91ca\u6bcf\u6761\u7ed3\u679c\u7684\u5339\u914d\u4f9d\u636e\u548c\u521d\u6b65\u89c4\u5219\u5206",
            "\u6574\u7406\u8fd8\u9700\u54ea\u4e9b\u975e\u8eab\u4efd\u4fe1\u606f\u624d\u80fd\u8fdb\u4e00\u6b65\u7b5b\u9009",
        ],
    }


def _lookup_cde_trials(
    *,
    condition_terms: list[str],
    age: int | None,
    sex: Literal["female", "male", "unknown"],
    pathology_terms: list[str] | None,
    prior_treatments: list[str] | None,
    region: str | None,
    blood_type: str | None,
    lookback_months: int | None,
    max_results: int,
    search_pages: int,
    candidate_limit: int,
    mode: str,
) -> dict[str, Any]:
    if age is not None and (not isinstance(age, int) or not 0 <= age <= 120):
        raise ValueError("age must be an integer between 0 and 120")
    if not 1 <= max_results <= DEEP_MAX_RESULTS:
        raise ValueError(f"max_results must be between 1 and {DEEP_MAX_RESULTS}")
    conditions = normalize_terms(condition_terms, "condition_terms")
    pathology = normalize_terms(pathology_terms, "pathology_terms")
    treatments = normalize_terms(prior_treatments, "prior_treatments")
    normalized_region = normalize_optional_text(region, "region")
    query_terms = tuple(dict.fromkeys((*conditions, *pathology)))[:3]
    if not query_terms:
        raise ValueError("At least one disease or pathology term is required")

    profile = PatientProfile(
        age=age,
        sex=sex,
        condition_terms=conditions,
        pathology_terms=pathology,
        prior_treatments=treatments,
        region=normalized_region,
    )
    published_after = (
        calendar_months_ago(date.today(), lookback_months)
        if lookback_months is not None
        else None
    )
    try:
        result = CdeLookupService(CdeBrowserWorkerClient()).lookup(
            profile=profile,
            query_terms=query_terms,
            max_results=max_results,
            published_after=published_after,
            search_pages=search_pages,
            candidate_limit=candidate_limit,
        ).to_dict()
    except (ValueError, InputPrivacyError, CdeWorkerError) as exc:
        return {
            "error": "CDE lookup could not be completed",
            "detail": str(exc),
            "disclaimer": "\u672a\u8fd4\u56de\u5019\u9009\u7ed3\u679c\u4e0d\u4ee3\u8868\u4e0d\u5b58\u5728\u5408\u9002\u8bd5\u9a8c\uff1b\u8bf7\u7a0d\u540e\u91cd\u8bd5\u6216\u7531\u7814\u7a76\u533b\u751f\u786e\u8ba4\u3002",
        }

    result["search_coverage"] = {
        "mode": mode,
        "pages_per_query_term": search_pages,
        "candidate_detail_pages_reviewed_at_most": candidate_limit,
        "results_shown_at_most": max_results,
    }
    result.update(_build_exploration_context(profile, mode))
    if blood_type:
        result.setdefault("warnings", []).append(
            "\u8840\u578b\u5df2\u63a5\u6536\uff0c\u4f46\u5f53\u524d\u7248\u672c\u4e0d\u5c06\u8840\u578b\u7528\u4e8e CDE \u5339\u914d\u3002"
        )
    return result


def search_cde_trials_local(
    disease: str = "",
    age: int | None = None,
    age_min: int | None = None,
    age_max: int | None = None,
    birth_year: int | None = None,
    sex: Literal["female", "male", "unknown"] = "unknown",
    city: str | None = None,
    province: str | None = None,
    region: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    recruitment_statuses: list[str] | None = None,
    blood_type: str | None = None,
    agent_canonical_disease: str | None = None,
    agent_candidate_terms: list[str] | None = None,
    trial_phases: list[Literal["I", "II", "III", "IV"]] | None = None,
    disease_duration_months: int | None = None,
    disease_stage: str | None = None,
    treatment_line: str | None = None,
    prior_treatments: list[str] | None = None,
    biomarkers: list[str] | None = None,
    sponsors: list[str] | None = None,
    site_names: list[str] | None = None,
    project_codes: list[str] | None = None,
    scores: list[str] | None = None,
    comorbidities: list[str] | None = None,
    disease_statuses: list[str] | None = None,
    organ_function_findings: list[str] | None = None,
    reproductive_statuses: list[str] | None = None,
    allergies: list[str] | None = None,
    precise_eligibility: bool = False,
    request_mode: Literal[
        "information_retrieval", "patient_matching", "criteria_analysis"
    ] = "patient_matching",
    max_results: int = 10,
) -> dict[str, Any]:
    """PRIMARY tool for ordinary CDE trial matching; search the local SQLite database.

    Use this before any live-web search. It returns total_count (all matching
    records) and returned_count (records included in this response). Date
    filters match first publication, source update, or status change. The
    default statuses are recruiting and not-yet-recruiting. Blood type is
    accepted but intentionally does not affect matching. Never send direct
    patient identifiers. Use a live tool only when the user explicitly asks
    for current website verification, a CTR is absent locally, or local data
    is stale.
    """
    normalized_prior_treatments = normalize_terms(prior_treatments, "prior_treatments")
    normalized_biomarkers = normalize_terms(biomarkers, "biomarkers")
    normalized_sponsors = normalize_terms(sponsors, "sponsors")
    normalized_sites = normalize_terms(site_names, "site_names")
    normalized_project_codes = normalize_terms(project_codes, "project_codes")
    normalized_scores = normalize_terms(scores, "scores")
    normalized_comorbidities = normalize_terms(comorbidities, "comorbidities")
    normalized_disease_statuses = normalize_terms(disease_statuses, "disease_statuses")
    normalized_organ_function = normalize_terms(
        organ_function_findings, "organ_function_findings"
    )
    normalized_reproductive = normalize_terms(
        reproductive_statuses, "reproductive_statuses"
    )
    normalized_allergies = normalize_terms(allergies, "allergies")
    search = SearchParameters(
        disease=disease,
        raw_disease=disease,
        canonical_disease=normalize_optional_text(
            agent_canonical_disease, "agent_canonical_disease"
        ),
        age=age,
        age_min=age_min,
        age_max=age_max,
        birth_year=normalize_optional_birth_year(
            birth_year,
            age=age,
            age_min=age_min,
            age_max=age_max,
        ),
        sex=sex,
        city=normalize_optional_text(city, "city"),
        province=normalize_optional_text(province, "province"),
        region=normalize_optional_text(region, "region"),
        blood_type=normalize_optional_text(blood_type, "blood_type"),
        trial_phases=tuple(trial_phases or ()),
        max_results=max_results,
        disease_duration_months=disease_duration_months,
        disease_stage=normalize_optional_text(disease_stage, "disease_stage"),
        treatment_line=normalize_optional_text(treatment_line, "treatment_line"),
        prior_treatments=normalized_prior_treatments,
        biomarkers=normalized_biomarkers,
        sponsors=normalized_sponsors,
        site_names=normalized_sites,
        project_codes=normalized_project_codes,
        scores=normalized_scores,
        comorbidities=normalized_comorbidities,
        disease_statuses=normalized_disease_statuses,
        organ_function_findings=normalized_organ_function,
        reproductive_statuses=normalized_reproductive,
        allergies=normalized_allergies,
        precise_eligibility=bool(precise_eligibility),
        request_mode=RequestMode(request_mode),
    )
    facts = PatientFactsBuilder().build("", search)
    information_retrieval = search.request_mode == RequestMode.INFORMATION_RETRIEVAL
    if facts.requires_clarification and not information_retrieval:
        return {
            "lookup_status": "needs_clarification",
            "reason_code": "PATIENT_FACTS_CONFLICT",
            "clarification": facts.clarification_message(),
            "agent_response_instruction": (
                "Pause the search and ask the user to resolve the explicit conflict. "
                "Do not present trial matches until the conflict is clarified."
            ),
        }
    candidate_limit = min(LOCAL_MAX_RESULTS, max(max_results, max_results * 4))
    query = LocalTrialQuery(
        disease=disease,
        age=age,
        age_min=age_min,
        age_max=age_max,
        sex=sex,
        city=search.city,
        province=search.province,
        region=search.region,
        date_from=_parse_iso_date(date_from, "date_from"),
        date_to=_parse_iso_date(date_to, "date_to"),
        recruitment_statuses=_normalize_local_statuses(recruitment_statuses),
        limit=candidate_limit,
        blood_type=search.blood_type,
        agent_canonical_disease=search.canonical_disease,
        agent_candidate_terms=normalize_terms(
            agent_candidate_terms, "agent_candidate_terms"
        ),
        trial_phases=tuple(trial_phases or ()),
        required_biomarkers=normalized_biomarkers,
        sponsors=normalized_sponsors,
        site_names=normalized_sites,
        project_codes=normalized_project_codes,
    )
    with _local_connection() as connection:
        service = _local_service(connection)
        result = service.search(query)
        evaluations = {}
        if information_retrieval:
            guarded = list(result.get("results") or [])[:max_results]
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
                limit=max_results,
            )
        result["results"] = guarded
        result["returned_count"] = len(guarded)
        result["guard_summary"] = guard_counts
        result["guard_evaluated_count"] = len(evaluations)
        result["patient_facts_schema"] = facts.schema_version
    result = _compact_search_response(result)
    result["guard_summary"] = guard_counts
    result["guard_evaluated_count"] = len(evaluations)
    result["patient_facts_schema"] = facts.schema_version
    result["query_plan"] = search.as_query_plan()
    result["agent_response_instruction"] = (
        "Treat these as preliminary public-record matches, not an enrollment decision. "
        "Never present EXPLICIT_CONFLICT records. State total_count and returned_count, cite "
        "public evidence, keep UNKNOWN unresolved, and do not invent missing eligibility criteria. "
        "When query_plan contains biomarkers, every returned record already has matching public "
        "biomarker evidence; do not broaden back to disease-only records."
    )
    return result


@mcp.tool()
def get_cde_trial_local(ctr_number: str) -> dict[str, Any]:
    """Read one CDE trial from local SQLite by its required CTR registration number.

    Use for ordinary trial-detail questions. Restricted fields such as sites,
    contacts, investigators, and sponsors remain hidden here.
    """
    ctr, detail_id = _parse_trial_reference(ctr_number)
    if detail_id is not None or ctr is None:
        raise ValueError("ctr_number must be a CTR registration number")
    with _local_connection() as connection:
        trial = _local_service(connection).get_trial(ctr)
    if trial is None:
        return {
            "lookup_status": "not_found_locally",
            "ctr_number": ctr,
            "requires_live_verification": True,
            "agent_response_instruction": (
                "Do not invent this trial. Ask to verify it on the official CDE website."
            ),
        }
    return {
        "lookup_status": "found",
        "data_source": "local_sqlite",
        "trial": {key: value for key, value in trial.items() if key not in {
            "inclusion_criteria", "exclusion_criteria"
        } and not key.startswith("_")},
        "requires_live_verification": False,
    }


def get_cde_trial_criteria_local(ctr_number: str) -> dict[str, Any]:
    """Read official inclusion/exclusion criteria locally for one required CTR number.

    Use this before producing a CTR-specific questionnaire or preliminary
    screening. Preserve the source meaning when translating criteria into
    plain questions. If criteria_available is false, do not substitute a
    generic questionnaire and do not claim it is trial-specific.
    """
    ctr, detail_id = _parse_trial_reference(ctr_number)
    if detail_id is not None or ctr is None:
        raise ValueError("ctr_number must be a CTR registration number")
    with _local_connection() as connection:
        trial = _local_service(connection).get_trial(ctr)
    if trial is None:
        return {
            "lookup_status": "not_found_locally",
            "ctr_number": ctr,
            "criteria_available": False,
            "requires_live_verification": True,
            "agent_response_instruction": "Do not invent criteria; use live verification.",
        }
    inclusion = trial.pop("inclusion_criteria")
    exclusion = trial.pop("exclusion_criteria")
    available = bool(inclusion or exclusion)
    return {
        "lookup_status": "found",
        "data_source": "local_sqlite",
        "ctr_number": ctr,
        "title": trial.get("public_title") or trial.get("professional_title"),
        "indication": trial.get("indication_raw"),
        "recruitment_status": trial.get("recruitment_status_raw"),
        "last_verified_at": trial.get("last_verified_at"),
        "criteria_available": available,
        "inclusion_criteria": inclusion,
        "exclusion_criteria": exclusion,
        "requires_live_verification": not available,
        "agent_response_instruction": (
            "Turn each available criterion into a plain-language question without changing "
            "thresholds; keep unknown answers unresolved and never give a final enrollment decision."
            if available else
            "The local public record contains no eligibility criteria. Do not create a generic "
            "trial-specific questionnaire; offer official live verification instead."
        ),
    }


def get_cde_database_status() -> dict[str, Any]:
    """Report local CDE coverage and freshness without accessing the CDE website."""
    with _local_connection() as connection:
        trial_stats = connection.execute(
            "SELECT COUNT(*) AS trial_count, MIN(first_publication_date) AS earliest_publication, "
            "MAX(first_publication_date) AS latest_publication, MAX(last_verified_at) AS last_verified_at "
            "FROM trials WHERE is_active=1"
        ).fetchone()
        run = connection.execute(
            "SELECT id,run_type,started_at,finished_at,status,records_discovered,trials_created,"
            "trials_updated,records_failed,records_skipped FROM sync_runs "
            "ORDER BY COALESCE(finished_at,started_at) DESC,id DESC LIMIT 1"
        ).fetchone()
    last_verified = trial_stats["last_verified_at"]
    age_days = None
    if last_verified:
        verified = datetime.fromisoformat(last_verified.replace("Z", "+00:00"))
        if verified.tzinfo is None:
            verified = verified.replace(tzinfo=timezone.utc)
        age_days = max(0, (datetime.now(timezone.utc) - verified).days)
    return {
        "data_source": "local_sqlite",
        "database_path": str(LOCAL_DATABASE_PATH),
        "trial_count": trial_stats["trial_count"],
        "publication_date_range": {
            "from": trial_stats["earliest_publication"],
            "to": trial_stats["latest_publication"],
        },
        "last_verified_at": last_verified,
        "freshness_age_days": age_days,
        "latest_sync_run": dict(run) if run else None,
        "agent_response_instruction": (
            "Freshness describes the local copy, not a guarantee that every trial is still recruiting."
        ),
    }


@mcp.tool()
def search_cde_trials(
    condition_terms: list[str] | None = None,
    age: int | None = None,
    age_min: int | None = None,
    age_max: int | None = None,
    birth_year: int | None = None,
    sex: Literal["female", "male", "unknown"] = "unknown",
    pathology_terms: list[str] | None = None,
    prior_treatments: list[str] | None = None,
    region: str | None = None,
    city: str | None = None,
    province: str | None = None,
    blood_type: str | None = None,
    lookback_months: int | None = None,
    trial_phases: list[Literal["I", "II", "III", "IV"]] | None = None,
    disease_duration_months: int | None = None,
    disease_stage: str | None = None,
    treatment_line: str | None = None,
    biomarkers: list[str] | None = None,
    sponsors: list[str] | None = None,
    site_names: list[str] | None = None,
    project_codes: list[str] | None = None,
    scores: list[str] | None = None,
    comorbidities: list[str] | None = None,
    disease_statuses: list[str] | None = None,
    organ_function_findings: list[str] | None = None,
    reproductive_statuses: list[str] | None = None,
    allergies: list[str] | None = None,
    precise_eligibility: bool = False,
    request_mode: Literal[
        "information_retrieval", "patient_matching", "criteria_analysis"
    ] = "patient_matching",
    max_results: int = 10,
) -> dict[str, Any]:
    """DEFAULT CDE screening tool backed by local SQLite, not the live website.

    Use this for every ordinary de-identified patient trial search, including
    requests for a total count, up to twenty results, phase filters, city, or
    recent months. A zero/omitted lookback means no date restriction. The
    local database is read-only. Do not use a live tool unless the user
    explicitly asks for current official-site verification or local data is
    missing/stale.
    """
    terms = normalize_terms(condition_terms, "condition_terms")
    normalized_biomarkers = normalize_terms(biomarkers, "biomarkers")
    normalized_sponsors = normalize_terms(sponsors, "sponsors")
    normalized_sites = normalize_terms(site_names, "site_names")
    normalized_project_codes = normalize_terms(project_codes, "project_codes")
    if not any((terms, normalized_biomarkers, normalized_sponsors, normalized_sites, normalized_project_codes)):
        raise ValueError("provide at least one auditable retrieval constraint")
    if not 1 <= max_results <= LOCAL_MAX_RESULTS:
        raise ValueError(f"max_results must be between 1 and {LOCAL_MAX_RESULTS}")
    date_from = None
    if lookback_months not in (None, 0):
        date_from = calendar_months_ago(date.today(), lookback_months).isoformat()
    candidates = [*terms[1:], *normalize_terms(pathology_terms, "pathology_terms")]
    normalized_region = normalize_optional_text(region, "region")
    is_broad_region = normalized_region in REGION_PROVINCES
    return search_cde_trials_local(
        disease=terms[0] if terms else "",
        age=age,
        age_min=age_min,
        age_max=age_max,
        birth_year=birth_year,
        sex=sex,
        city=normalize_optional_text(city, "city") or (None if is_broad_region else normalized_region),
        province=normalize_optional_text(province, "province"),
        region=normalized_region if is_broad_region else None,
        date_from=date_from,
        blood_type=blood_type,
        agent_candidate_terms=candidates[:8],
        trial_phases=trial_phases,
        disease_duration_months=disease_duration_months,
        disease_stage=disease_stage,
        treatment_line=treatment_line,
        prior_treatments=prior_treatments,
        biomarkers=list(normalized_biomarkers),
        sponsors=list(normalized_sponsors),
        site_names=list(normalized_sites),
        project_codes=list(normalized_project_codes),
        scores=scores,
        comorbidities=comorbidities,
        disease_statuses=disease_statuses,
        organ_function_findings=organ_function_findings,
        reproductive_statuses=reproductive_statuses,
        allergies=allergies,
        precise_eligibility=precise_eligibility,
        request_mode=request_mode,
        max_results=max_results,
    )


def search_cde_trials_live(
    condition_terms: list[str],
    age: int | None = None,
    sex: Literal["female", "male", "unknown"] = "unknown",
    pathology_terms: list[str] | None = None,
    prior_treatments: list[str] | None = None,
    region: str | None = None,
    blood_type: str | None = None,
    lookback_months: int | None = None,
    max_results: int = FAST_MAX_RESULTS,
) -> dict[str, Any]:
    """LIVE-WEB fallback CDE screening; never use for an ordinary request.

    Prefer search_cde_trials. Use this only when the user explicitly
    requests current official-site verification, local data is unavailable or
    stale, or a requested CTR is missing locally. It accesses CDE through the
    browser worker and can be slower or blocked by website validation.
    """
    if not 1 <= max_results <= FAST_MAX_RESULTS:
        raise ValueError(f"max_results must be between 1 and {FAST_MAX_RESULTS} for fast screening")
    return _lookup_cde_trials(
        condition_terms=condition_terms,
        age=age,
        sex=sex,
        pathology_terms=pathology_terms,
        prior_treatments=prior_treatments,
        region=region,
        blood_type=blood_type,
        lookback_months=lookback_months,
        max_results=max_results,
        search_pages=FAST_SEARCH_PAGES,
        candidate_limit=FAST_CANDIDATE_LIMIT,
        mode="fast",
    )


def explore_cde_trials_deep_live(
    condition_terms: list[str],
    age: int | None = None,
    sex: Literal["female", "male", "unknown"] = "unknown",
    pathology_terms: list[str] | None = None,
    prior_treatments: list[str] | None = None,
    region: str | None = None,
    blood_type: str | None = None,
    lookback_months: int | None = None,
    max_results: int = DEEP_MAX_RESULTS,
) -> dict[str, Any]:
    """LIVE-WEB deep fallback, only when the user explicitly requests it.

    This reviews up to three result pages and forty candidate detail pages,
    returning at most ten preliminary matches. It is intentionally slower than
    the local search_cde_trials and should not be used for an ordinary request.
    """
    return _lookup_cde_trials(
        condition_terms=condition_terms,
        age=age,
        sex=sex,
        pathology_terms=pathology_terms,
        prior_treatments=prior_treatments,
        region=region,
        blood_type=blood_type,
        lookback_months=lookback_months,
        max_results=max_results,
        search_pages=DEEP_SEARCH_PAGES,
        candidate_limit=DEEP_CANDIDATE_LIMIT,
        mode="deep",
    )


@mcp.tool()
def get_cde_trial_questionnaire_source(trial_reference: str) -> dict[str, Any]:
    """DEFAULT questionnaire source: local SQLite first, live CDE only as fallback.

    Use for ordinary CTR-specific criteria and questionnaire requests. A CTR
    number is read from local SQLite. The live website is used only for an
    official detail URL, a missing local CTR, or unavailable local criteria.
    Never invent a trial-specific questionnaire when neither source verifies
    the criteria.
    """
    ctr, detail_id = _parse_trial_reference(trial_reference)
    if ctr is not None:
        local = get_cde_trial_criteria_local(ctr)
        if local.get("lookup_status") == "found" and local.get("criteria_available"):
            canonical_local = {
                key: value
                for key, value in local.items()
                if key not in {"inclusion_criteria", "exclusion_criteria"}
            }
            return {
                **canonical_local,
                "lookup_status": "verified",
                "lookup_route": "local_primary",
                "verification_scope": "local_public_snapshot",
                "source": "中国药物临床试验登记与信息公示平台公开登记信息的本地快照",
                "trial": {
                    "registration_no": local.get("ctr_number"),
                    "public_title": local.get("title"),
                    "indication": local.get("indication"),
                    "status": local.get("recruitment_status"),
                    "last_verified_at": local.get("last_verified_at"),
                },
                "criteria_for_questionnaire": {
                    "inclusion_criteria": local.get("inclusion_criteria") or [],
                    "exclusion_criteria": local.get("exclusion_criteria") or [],
                },
                "agent_response_instruction": (
                    "Generate the requested trial-specific questionnaire from "
                    "criteria_for_questionnaire. Preserve all thresholds, keep unknown answers "
                    "unresolved, and never give a final enrollment decision."
                ),
            }
    live = get_cde_trial_questionnaire_source_live(trial_reference)
    if isinstance(live, dict):
        live["lookup_route"] = "live_fallback"
        if ctr is not None:
            live["local_lookup_status"] = local.get("lookup_status")
            live["local_criteria_available"] = local.get("criteria_available")
    return live


def get_cde_trial_questionnaire_source_live(trial_reference: str) -> dict[str, Any]:
    """LIVE-WEB fallback for one CDE trial by CTR number or detail URL.

    Do not use for an ordinary questionnaire request. Use only when the user
    explicitly requests current official-site verification, the CTR is absent
    locally, local data is stale, or local eligibility criteria are unavailable.
    This tool only accesses the official CDE detail page; it does not browse arbitrary links.
    The returned questionnaire source must be used as-is: never substitute a
    generic disease or oncology questionnaire when the criteria cannot be
    verified.
    """
    try:
        trial = _load_cde_trial(trial_reference)
        return _trial_questionnaire_source(trial)
    except (ValueError, CdeWorkerError) as exc:
        return {
            "lookup_status": "unverified",
            "error": "The requested CDE trial detail could not be verified",
            "detail": str(exc),
            "agent_response_instruction": (
                "明确说明未能验证该条项目的真实公开条件。不要生成或暗示为该项目专属的通用问卷，"
                "也不要声称使用了 Safari、CDP、搜索引擎或任何未实际调用的渠道。"
                "可请用户提供项目页面中公开的入选/排除标准文字或截图后再协助整理。"
            ),
            "disclaimer": "未验证到项目条件时，不能据此判断是否适合参加试验。",
        }


@mcp.tool()
def verify_cde_trial_live(trial_reference: str) -> dict[str, Any]:
    """Explicitly verify one CTR number or official CDE detail URL on the live website.

    This network fallback is only for explicit real-time verification, a local
    record that is missing or stale, or unavailable local criteria. It accepts
    no arbitrary website URL and must not be called merely because a normal
    local search returned few matches.
    """
    return get_cde_trial_questionnaire_source_live(trial_reference)


if __name__ == "__main__":
    mcp.run(transport="stdio")
