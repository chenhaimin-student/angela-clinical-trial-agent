from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from angela_orchestration.eligibility import EligibilityGuard, TrialDisposition
from angela_orchestration.models import SearchParameters
from angela_orchestration.patient_facts import PatientFactsBuilder

from .database import connect_read_only
from .oracle import IndependentRetrievalOracle


TUPLE_FIELDS = {
    "trial_phases",
    "prior_treatments",
    "biomarkers",
    "scores",
    "comorbidities",
    "disease_statuses",
    "organ_function_findings",
    "reproductive_statuses",
    "allergies",
}
RECRUITABLE_STATUSES = {"recruiting", "not_yet_recruiting"}
NO_RESULT_PHRASES = (
    "暂未找到",
    "未找到",
    "没有找到",
    "未检索到",
    "没有检索到",
    "未能检索到",
    "检索结果为空",
    "0 条",
    "0条",
    "0 项",
)
CTR_PATTERN = re.compile(r"CTR\d{6,}", re.IGNORECASE)


@dataclass(frozen=True)
class AnswerValidation:
    passed: bool
    reasons: tuple[str, ...]
    returned_ids: tuple[str, ...]
    expected_top_ids: tuple[str, ...]
    ctr_existence_rate: float
    status_compliance_rate: float
    retrieval_precision: float
    explicit_conflict_count: int
    reported_total_count: int | None
    oracle_total_count: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def search_parameters(payload: dict[str, Any]) -> SearchParameters:
    values = dict(payload)
    for field in TUPLE_FIELDS:
        if field in values:
            values[field] = tuple(values[field] or ())
    return SearchParameters(**values)


def _ratio(numerator: int, denominator: int) -> float:
    return 1.0 if denominator == 0 else numerator / denominator


def _reported_total_count(answer: str) -> int | None:
    patterns = (
        r"(?:初步匹配|总匹配(?:数)?|共(?:匹配|找到)?)\s*(\d+)\s*[条项]",
        r"匹配(?:到)?\s*(\d+)\s*[条项]",
    )
    for pattern in patterns:
        match = re.search(pattern, answer)
        if match:
            return int(match.group(1))
    return None


def extract_returned_trial_ids(answer: str) -> tuple[str, ...]:
    """Extract unique returned trials in their first-appearance order.

    An answer may cite one CTR in a result heading and again in a link list.
    Repeated textual references are evidence for one trial, not duplicate
    returned trial records.
    """
    return tuple(dict.fromkeys(match.upper() for match in CTR_PATTERN.findall(answer)))


def has_no_result_statement(answer: str) -> bool:
    """Recognize ordinary user-facing ways to state that no result was found."""
    return any(phrase in (answer or "") for phrase in NO_RESULT_PHRASES)


class EndToEndAnswerValidator:
    """Validate user-visible answers against a read-only formal CDE database.

    This deliberately does not trust the HTTP status or the presence of a CTR.
    Every returned CTR must exist, remain recruitable, satisfy the independent
    retrieval contract, and pass the deterministic explicit-conflict guard.
    """

    def __init__(
        self,
        *,
        project_root: str | Path,
        database_path: str | Path,
        reference_date: date | None = None,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.database_path = Path(database_path).expanduser().resolve()
        self.reference_date = reference_date or date.today()
        self.concepts_path = self.project_root / "cde-data/config/disease_concepts.json"
        data_root = self.project_root / "cde-data"
        if str(data_root) not in sys.path:
            sys.path.insert(0, str(data_root))

    def validate(
        self,
        *,
        prompt: str,
        search_payload: dict[str, Any],
        answer: str,
        required_terms: tuple[str, ...] = (),
    ) -> AnswerValidation:
        search = search_parameters(search_payload)
        returned_ids = extract_returned_trial_ids(answer)
        reasons: list[str] = []
        if len(returned_ids) > search.max_results:
            reasons.append("returned_count_exceeds_limit")
        missing_terms = [
            term for term in required_terms if term.casefold() not in answer.casefold()
        ]
        if missing_terms:
            reasons.append("missing_condition_trace:" + ",".join(missing_terms))

        facts = PatientFactsBuilder().build(
            prompt,
            search,
            reference_date=self.reference_date,
        )
        connection = connect_read_only(self.database_path)
        try:
            oracle = IndependentRetrievalOracle(connection, self.concepts_path).search(
                search,
                reference_date=self.reference_date,
                patient_age_min=facts.age.minimum_years,
                patient_age_max=facts.age.maximum_years,
                patient_sex=str(facts.sex.value or "unknown"),
            )
            rows = self._returned_rows(connection, returned_ids)
        finally:
            connection.close()

        by_ctr = {str(row["registration_no"]).upper(): dict(row) for row in rows}
        existing = sum(ctr in by_ctr for ctr in returned_ids)
        recruitable = sum(
            ctr in by_ctr
            and str(by_ctr[ctr].get("recruitment_status_normalized"))
            in RECRUITABLE_STATUSES
            for ctr in returned_ids
        )
        allowed = set(oracle.final_ranked_ids)
        relevant = sum(ctr in allowed for ctr in returned_ids)
        if existing != len(returned_ids):
            reasons.append("returned_ctr_missing_from_database")
        if recruitable != len(returned_ids):
            reasons.append("returned_ctr_not_recruitable")
        if relevant != len(returned_ids):
            unexpected = sorted(set(returned_ids) - allowed)
            reasons.append("returned_ctr_outside_query_contract:" + ",".join(unexpected))

        guard = EligibilityGuard()
        explicit_conflicts: list[str] = []
        for ctr in returned_ids:
            trial = by_ctr.get(ctr)
            if trial is None:
                continue
            evaluation = guard.evaluate(facts, trial)
            if evaluation.disposition == TrialDisposition.EXPLICIT_CONFLICT:
                explicit_conflicts.append(ctr)
        if explicit_conflicts:
            reasons.append("explicit_conflict_leakage:" + ",".join(explicit_conflicts))

        has_no_result_text = has_no_result_statement(answer)
        if not returned_ids and oracle.final_ranked_ids and has_no_result_text:
            reasons.append("false_empty_answer")
        elif not returned_ids and not has_no_result_text:
            reasons.append("answer_has_neither_ctr_nor_no_result_statement")

        top_limit = min(search.max_results, len(oracle.final_ranked_ids))
        return AnswerValidation(
            passed=not reasons,
            reasons=tuple(reasons),
            returned_ids=returned_ids,
            expected_top_ids=tuple(oracle.final_ranked_ids[:top_limit]),
            ctr_existence_rate=_ratio(existing, len(returned_ids)),
            status_compliance_rate=_ratio(recruitable, len(returned_ids)),
            retrieval_precision=_ratio(relevant, len(returned_ids)),
            explicit_conflict_count=len(explicit_conflicts),
            reported_total_count=_reported_total_count(answer),
            oracle_total_count=oracle.raw_total_count,
        )

    @staticmethod
    def _returned_rows(
        connection: sqlite3.Connection,
        returned_ids: tuple[str, ...],
    ) -> list[sqlite3.Row]:
        if not returned_ids:
            return []
        marks = ",".join("?" for _ in returned_ids)
        return connection.execute(
            "SELECT t.registration_no,t.recruitment_status_normalized,"
            "t.indication_raw,t.public_title,t.professional_title,"
            "v.inclusion_criteria_raw,v.exclusion_criteria_raw,v.content_hash "
            "FROM trials t JOIN trial_versions v ON v.id=t.current_version_id "
            f"WHERE t.registration_no IN ({marks})",
            returned_ids,
        ).fetchall()


def request_stage_timings(
    request_database_path: str | Path,
    session_key: str,
) -> dict[str, Any] | None:
    """Return durable request-stage timings for one synthetic test session."""
    path = Path(request_database_path)
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        request = connection.execute(
            "SELECT request_id,state,stage,reason_code,created_at,updated_at "
            "FROM requests WHERE session_key=? ORDER BY created_at DESC LIMIT 1",
            (session_key,),
        ).fetchone()
        if request is None:
            return None
        events = connection.execute(
            "SELECT to_state,stage,reason_code,created_at,metadata_json "
            "FROM request_events WHERE request_id=? ORDER BY id",
            (request["request_id"],),
        ).fetchall()
    finally:
        connection.close()

    points: list[tuple[str, str | None, datetime]] = []
    for event in events:
        points.append(
            (
                str(event["to_state"]),
                str(event["stage"]) if event["stage"] else None,
                datetime.fromisoformat(str(event["created_at"])),
            )
        )
    if not points:
        return None
    started = points[0][2]
    finished = points[-1][2]

    def first_time(*, state: str | None = None, stages: tuple[str, ...] = ()) -> datetime | None:
        for item_state, item_stage, timestamp in points:
            if state is not None and item_state != state:
                continue
            if stages and item_stage not in stages:
                continue
            return timestamp
        return None

    ready = first_time(state="READY")
    queued = first_time(state="QUEUED")
    running = first_time(state="RUNNING")
    delivering = first_time(state="DELIVERING")
    completed = first_time(state="COMPLETED")

    def milliseconds(begin: datetime | None, end: datetime | None) -> int | None:
        if begin is None or end is None:
            return None
        return max(0, round((end - begin).total_seconds() * 1000))

    route = next(
        (
            stage
            for state, stage, _timestamp in points
            if state == "RUNNING" and stage in {"local_cde_query", "main_agent"}
        ),
        None,
    )
    return {
        "request_id": str(request["request_id"]),
        "final_state": str(request["state"]),
        "final_stage": request["stage"],
        "reason_code": request["reason_code"],
        "execution_route": route,
        "routing_ms": milliseconds(started, ready),
        "queue_ms": milliseconds(queued, running),
        "execution_ms": milliseconds(running, delivering or completed),
        "delivery_ms": milliseconds(delivering, completed),
        "total_ms": milliseconds(started, completed or finished),
        "event_count": len(points),
    }
