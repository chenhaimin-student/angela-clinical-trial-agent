from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


DIRECT_IDENTIFIER_KEYS = {
    "name",
    "patient_name",
    "phone",
    "telephone",
    "id_number",
    "medical_record_number",
    "sample_id",
    "姓名",
    "手机号",
    "电话",
    "身份证",
    "病历号",
    "样本编号",
}


class InputPrivacyError(ValueError):
    """Raised before an input containing direct identifiers is processed."""


def reject_direct_identifiers(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = DIRECT_IDENTIFIER_KEYS.intersection(value)
        if forbidden:
            labels = ", ".join(sorted(forbidden))
            raise InputPrivacyError(f"Input must not include direct identifiers: {labels}")
        for item in value.values():
            reject_direct_identifiers(item)
    elif isinstance(value, list):
        for item in value:
            reject_direct_identifiers(item)


@dataclass(frozen=True)
class PatientProfile:
    """Only fields needed for a preliminary, non-diagnostic CDE search."""

    age: int | None = None
    sex: str | None = None
    condition_terms: tuple[str, ...] = ()
    pathology_terms: tuple[str, ...] = ()
    prior_treatments: tuple[str, ...] = ()
    region: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PatientProfile":
        age = raw.get("age")
        if age is not None and (not isinstance(age, int) or not 0 <= age <= 120):
            raise ValueError("patient.age must be an integer between 0 and 120")
        sex = raw.get("sex")
        if sex not in {None, "female", "male", "unknown"}:
            raise ValueError("patient.sex must be female, male, or unknown")
        return cls(
            age=age,
            sex=sex,
            condition_terms=_strings(raw.get("condition_terms", []), "condition_terms"),
            pathology_terms=_strings(raw.get("pathology_terms", []), "pathology_terms"),
            prior_treatments=_strings(raw.get("prior_treatments", []), "prior_treatments"),
            region=_optional_string(raw.get("region"), "region"),
        )


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"patient.{name} must be a list of strings")
    normalized = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
    if len(normalized) > 8:
        raise ValueError(f"patient.{name} may contain at most 8 entries")
    return normalized


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"patient.{name} must be a non-empty string when provided")
    return value.strip()


@dataclass(frozen=True)
class CdeRssRecord:
    registration_no: str
    title: str
    description: str
    detail_url: str
    detail_id: str


@dataclass(frozen=True)
class CdeTrial:
    registration_no: str
    status: str | None
    recruitment_status: str | None
    first_publication_date: str | None
    sponsor_name: str | None
    drug_name: str | None
    indication: str | None
    public_title: str | None
    professional_title: str | None
    trial_phase: str | None
    trial_scope: str | None
    inclusion_criteria: str | None
    exclusion_criteria: str | None
    detail_url: str
    raw_text: str


@dataclass(frozen=True)
class TrialMatch:
    trial: CdeTrial
    score: int
    matched_fields: tuple[str, ...]
    needs_confirmation: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self.trial)
        result.pop("raw_text", None)
        # Criteria are used internally for preliminary matching, but can be
        # lengthy and may include unrelated public page material. The chat
        # response exposes only the review status, never the raw page text.
        result.pop("inclusion_criteria", None)
        result.pop("exclusion_criteria", None)
        result.update(
            {
                "possible_match": True,
                "score": self.score,
                "matched_fields": list(self.matched_fields),
                "needs_confirmation": list(self.needs_confirmation),
                "eligibility_criteria_reviewed": True,
                "disclaimer": "仅为公开临床试验登记信息的初步匹配；是否招募及是否可入组，必须由研究医生和试验机构确认。",
            }
        )
        return result


@dataclass
class LookupResult:
    query_terms: tuple[str, ...]
    published_after: str | None = None
    matches: list[TrialMatch] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_terms": list(self.query_terms),
            "published_after": self.published_after,
            "matches": [item.to_dict() for item in self.matches],
            "warnings": self.warnings,
            "disclaimer": "本工具不保存病例信息，也不构成诊断、治疗建议或入组结论。",
        }
