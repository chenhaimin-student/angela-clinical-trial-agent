from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from .models import SearchParameters


SUPPORTED_EVIDENCE_FIELDS = frozenset({
    "disease_stage",
    "treatment_line",
    "prior_treatments",
    "disease_statuses",
})


class EvidenceState(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ConstraintFinding:
    field: str
    value: str
    state: EvidenceState
    evidence: str | None = None
    source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "state": self.state.value,
            "evidence": self.evidence,
            "source": self.source,
        }


@dataclass(frozen=True)
class ConstraintReview:
    registration_no: str
    findings: tuple[ConstraintFinding, ...]

    @property
    def has_conflict(self) -> bool:
        return any(item.state == EvidenceState.FAIL for item in self.findings)

    @property
    def pass_count(self) -> int:
        return sum(item.state == EvidenceState.PASS for item in self.findings)

    @property
    def unknown_count(self) -> int:
        return sum(item.state == EvidenceState.UNKNOWN for item in self.findings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "registration_no": self.registration_no,
            "findings": [item.as_dict() for item in self.findings],
            "has_conflict": self.has_conflict,
            "pass_count": self.pass_count,
            "unknown_count": self.unknown_count,
        }


def requested_evidence_fields(search: SearchParameters) -> tuple[str, ...]:
    return tuple(
        field for field, value in (
            ("disease_stage", search.disease_stage),
            ("treatment_line", search.treatment_line),
            ("prior_treatments", search.prior_treatments),
            ("disease_statuses", search.disease_statuses),
        )
        if value
    )


class ConstraintEvidenceFilter:
    """Evidence-bound filtering for structured public-record questions.

    PASS requires a matching phrase in public trial fields or inclusion text.
    FAIL is emitted only for a narrow explicit contradiction. Missing or
    semantically incomplete evidence remains UNKNOWN and is never presented as
    qualification.
    """

    def evaluate(self, search: SearchParameters, trial: dict[str, Any]) -> ConstraintReview:
        sources = _sources(trial)
        findings: list[ConstraintFinding] = []
        if search.disease_stage:
            findings.append(self._find("disease_stage", search.disease_stage, sources))
        if search.treatment_line:
            findings.append(self._find_treatment_line(search.treatment_line, sources))
        for value in search.prior_treatments:
            findings.append(self._find("prior_treatments", value, sources))
        for value in search.disease_statuses:
            findings.append(self._find("disease_statuses", value, sources))
        return ConstraintReview(
            str(trial.get("registration_no") or ""),
            tuple(findings),
        )

    def _find(
        self,
        field: str,
        value: str,
        sources: tuple[tuple[str, str], ...],
    ) -> ConstraintFinding:
        aliases = _aliases(field, value)
        matched = _match_sources(sources, aliases)
        if matched:
            source, evidence = matched
            return ConstraintFinding(field, value, EvidenceState.PASS, evidence, source)
        return ConstraintFinding(field, value, EvidenceState.UNKNOWN)

    def _find_treatment_line(
        self,
        value: str,
        sources: tuple[tuple[str, str], ...],
    ) -> ConstraintFinding:
        aliases = _aliases("treatment_line", value)
        matched = _match_sources(sources, aliases)
        if matched:
            source, evidence = matched
            return ConstraintFinding(
                "treatment_line", value, EvidenceState.PASS, evidence, source
            )
        compact = re.sub(r"\s+", "", value)
        untreated = bool(re.search(r"初治|未经.*治疗|未接受.*治疗|一线", compact))
        if untreated:
            conflict = _match_sources(
                sources,
                (
                    r"既往.{0,20}(?:二线|三线|多线|系统治疗).{0,12}(?:失败|进展)",
                    r"(?:二线|三线|多线|后线)(?:治疗|患者|人群)",
                    r"经治(?:患者|人群)",
                ),
                regex=True,
            )
            if conflict:
                source, evidence = conflict
                return ConstraintFinding(
                    "treatment_line", value, EvidenceState.FAIL, evidence, source
                )
        return ConstraintFinding("treatment_line", value, EvidenceState.UNKNOWN)


def apply_constraint_reviews(
    results: list[dict[str, Any]],
    reviews: dict[str, ConstraintReview],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    accepted: list[tuple[dict[str, Any], ConstraintReview]] = []
    counts = {state.value: 0 for state in EvidenceState}
    for result in results:
        ctr = str(result.get("registration_no") or "")
        review = reviews.get(ctr, ConstraintReview(ctr, ()))
        for finding in review.findings:
            counts[finding.state.value] += 1
        if review.has_conflict:
            continue
        enriched = dict(result)
        enriched["constraint_review"] = review.as_dict()
        pending = [
            item.field for item in review.findings
            if item.state == EvidenceState.UNKNOWN
        ]
        enriched["needs_confirmation"] = list(dict.fromkeys([
            *(enriched.get("needs_confirmation") or []),
            *pending,
        ]))
        accepted.append((enriched, review))
    accepted.sort(
        key=lambda pair: (
            -pair[1].pass_count,
            pair[1].unknown_count,
            -int(pair[0].get("match_score") or 0),
            str(pair[0].get("registration_no") or ""),
        )
    )
    return [item for item, _review in accepted[:limit]], counts


def _sources(trial: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    inclusion = trial.get("inclusion_criteria") or []
    if isinstance(inclusion, list):
        inclusion_text = "\n".join(
            str(item.get("raw_text") or "") if isinstance(item, dict) else str(item)
            for item in inclusion
        )
    else:
        inclusion_text = ""
    if not inclusion_text:
        inclusion_text = str(trial.get("inclusion_criteria_raw") or "")
    return tuple(
        (field, str(value or ""))
        for field, value in (
            ("适应症", trial.get("indication_raw")),
            ("公开标题", trial.get("public_title")),
            ("专业标题", trial.get("professional_title")),
            ("入选标准", inclusion_text),
        )
        if str(value or "").strip()
    )


def _aliases(field: str, value: str) -> tuple[str, ...]:
    compact = re.sub(r"\s+", "", str(value or ""))
    folded = compact.casefold()
    if field == "treatment_line":
        if re.search(r"初治|未经.*治疗|未接受.*治疗|一线", compact):
            return ("初治", "一线", "未经治疗", "既往未接受", "未接受过系统")
        if re.search(r"二线", compact):
            return ("二线", "至少一线治疗后", "一线治疗失败")
        if re.search(r"三线|多线|后线", compact):
            return ("三线", "多线", "后线")
    if field == "disease_stage":
        for aliases in (
            ("IV期", "Ⅳ期", "四期", "4期"),
            ("III期", "Ⅲ期", "三期", "3期"),
            ("II期", "Ⅱ期", "二期", "2期"),
            ("I期", "Ⅰ期", "一期", "1期"),
        ):
            if any(alias.casefold() in folded for alias in aliases):
                return aliases
        if "晚期" in compact or "advanced" in folded:
            return ("晚期", "advanced")
    if field == "disease_statuses":
        groups = (
            ("进展", "疾病进展", "progressive", "progression"),
            ("复发", "复发性", "relapsed", "recurrent"),
            ("难治", "难治性", "refractory"),
            ("转移", "转移性", "metastatic"),
            ("术后", "辅助治疗", "adjuvant", "切除后"),
        )
        selected = [alias for group in groups if any(x.casefold() in folded for x in group) for alias in group]
        if selected:
            return tuple(dict.fromkeys(selected))
    if field == "prior_treatments":
        terms = _significant_terms(compact)
        if terms:
            return terms
    return (compact,) if compact else ()


def _significant_terms(value: str) -> tuple[str, ...]:
    cleaned = re.sub(r"^(?:既往|已经|曾经|用过|接受过|使用过)", "", value)
    chunks = re.split(r"(?:联合|以及|和|与|后|治疗后|用药后|失败|进展|耐药)", cleaned)
    terms: list[str] = []
    for chunk in chunks:
        term = chunk.strip("，,。；;、 ")
        term = re.sub(r"(?:治疗|用药)$", "", term)
        if len(term) >= 2 and term not in terms:
            terms.append(term)
    return tuple(terms)


def _match_sources(
    sources: tuple[tuple[str, str], ...],
    terms: Iterable[str],
    *,
    regex: bool = False,
) -> tuple[str, str] | None:
    for source, text in sources:
        for term in terms:
            match = re.search(term, text, re.IGNORECASE) if regex else re.search(
                re.escape(term), text, re.IGNORECASE
            )
            if match:
                start = max(0, match.start() - 35)
                end = min(len(text), match.end() + 55)
                evidence = re.sub(r"\s+", " ", text[start:end]).strip()
                return source, evidence
    return None
