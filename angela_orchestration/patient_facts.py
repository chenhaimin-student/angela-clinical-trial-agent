from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any

from .models import SearchParameters


class FactStatus(str, Enum):
    KNOWN = "known"
    DERIVED = "derived"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"
    CONFLICTED = "conflicted"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class FactValue:
    value: Any = None
    status: FactStatus = FactStatus.UNKNOWN
    confidence: float = 0.0
    source_text: str | None = None


@dataclass(frozen=True)
class AgeInterval:
    minimum_years: int | None = None
    maximum_years: int | None = None
    status: FactStatus = FactStatus.UNKNOWN
    as_of: date | None = None

    @property
    def exact(self) -> int | None:
        if self.minimum_years is not None and self.minimum_years == self.maximum_years:
            return self.minimum_years
        return None


@dataclass(frozen=True)
class FactIssue:
    code: str
    message: str
    fields: tuple[str, ...] = ()
    blocking: bool = False
    source_text: str | None = None


@dataclass(frozen=True)
class PatientFacts:
    schema_version: str
    reference_date: date
    birth_year: FactValue
    age: AgeInterval
    sex: FactValue
    diseases: tuple[str, ...]
    canonical_diseases: tuple[str, ...]
    disease_duration_months: FactValue
    disease_stage: FactValue
    treatment_line: FactValue
    prior_treatments: tuple[str, ...]
    biomarkers: tuple[str, ...]
    scores: tuple[str, ...]
    comorbidities: tuple[str, ...]
    disease_statuses: tuple[str, ...]
    organ_function_findings: tuple[str, ...]
    reproductive_statuses: tuple[str, ...]
    allergies: tuple[str, ...]
    city: str | None
    region: str | None
    lookback_months: int | None
    precise_eligibility: bool
    missing: tuple[FactIssue, ...] = ()
    conflicts: tuple[FactIssue, ...] = ()
    ambiguities: tuple[FactIssue, ...] = ()
    unsupported: tuple[FactIssue, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def requires_clarification(self) -> bool:
        return any(issue.blocking for issue in (*self.conflicts, *self.missing))

    def clarification_message(self) -> str | None:
        issues = [issue.message for issue in (*self.conflicts, *self.missing) if issue.blocking]
        if not issues:
            return None
        return "为了避免错误匹配，请先确认：" + "；".join(dict.fromkeys(issues))


_BIRTH_YEAR_PATTERNS = (
    re.compile(r"(?<!\d)((?:19|20)\d{2})\s*年\s*(?:出生|生人|生的)"),
    re.compile(r"(?:出生于|生于)\s*((?:19|20)\d{2})(?:\s*年)?"),
)
_SHORT_BIRTH_YEAR = re.compile(r"(?<!\d)(\d{2})\s*年\s*(?:出生|生人|生的)")
_EXACT_AGE = re.compile(r"(?<!\d)(\d{1,3})\s*(?:周?岁|year[- ]?old)(?!\d)", re.IGNORECASE)
_SMALL_NUMBER = r"[一二两三四五六七八九十\d]{1,3}"
_DURATION_YEARS = re.compile(rf"(?:病史|患病|确诊|诊断)(?:时长|时间)?\s*({_SMALL_NUMBER})\s*年")
_DURATION_MONTHS = re.compile(rf"(?:病史|患病|确诊|诊断)(?:时长|时间)?\s*({_SMALL_NUMBER})\s*个?月")
_DURATION_SUFFIX_YEARS = re.compile(rf"(?<!\d)({_SMALL_NUMBER})\s*年(?:的)?病史")
_DISEASE_FOLLOWED_BY_YEARS = re.compile(rf"(?:SLE|系统性红斑狼疮|患者|患病)\s*({_SMALL_NUMBER})\s*年", re.IGNORECASE)


def _source_fragment(text: str, match: re.Match[str] | None) -> str | None:
    if match is None:
        return None
    return text[match.start() : match.end()]


def _extract_birth_year(text: str, reference: date) -> tuple[int | None, str | None]:
    for pattern in _BIRTH_YEAR_PATTERNS:
        match = pattern.search(text)
        if match:
            value = int(match.group(1))
            if 1900 <= value <= reference.year:
                return value, _source_fragment(text, match)
    short = _SHORT_BIRTH_YEAR.search(text)
    if short:
        value = 2000 + int(short.group(1))
        if value <= reference.year:
            return value, _source_fragment(text, short)
    return None, None


def _extract_exact_age(text: str) -> tuple[int | None, str | None]:
    match = _EXACT_AGE.search(text)
    if not match:
        return None, None
    value = int(match.group(1))
    return (value, _source_fragment(text, match)) if 0 <= value <= 120 else (None, None)


def _extract_duration_months(text: str) -> tuple[int | None, str | None]:
    match = _DURATION_YEARS.search(text)
    if match:
        return _parse_small_number(match.group(1)) * 12, _source_fragment(text, match)
    match = _DURATION_SUFFIX_YEARS.search(text) or _DISEASE_FOLLOWED_BY_YEARS.search(text)
    if match:
        return _parse_small_number(match.group(1)) * 12, _source_fragment(text, match)
    match = _DURATION_MONTHS.search(text)
    if match:
        return _parse_small_number(match.group(1)), _source_fragment(text, match)
    return None, None


def _parse_small_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return digits.get(value, 0)


def _sex_conflict(text: str) -> bool:
    female = bool(re.search(r"(?:女性|女患者|女孩|\bfemale\b|\bwoman\b)", text, re.IGNORECASE))
    male = bool(re.search(r"(?:男性|男患者|男孩|\bmale\b|\bman\b)", text, re.IGNORECASE))
    return female and male and not bool(re.search(r"男女不限|性别不限", text))


class PatientFactsBuilder:
    """Build a small, auditable patient contract from routing output and source text.

    The language model may enrich ``SearchParameters``. Deterministic calculations
    own dates, ranges, and explicit contradictions so an LLM cannot silently
    override them.
    """

    def build(
        self,
        text: str,
        search: SearchParameters | None,
        *,
        reference_date: date | None = None,
    ) -> PatientFacts:
        reference = reference_date or date.today()
        raw = text or ""
        conflicts: list[FactIssue] = []
        missing: list[FactIssue] = []
        ambiguities: list[FactIssue] = []

        parsed_birth_year, birth_source = _extract_birth_year(raw, reference)
        birth_year = parsed_birth_year if parsed_birth_year is not None else (
            search.birth_year if search else None
        )
        birth_fact = FactValue(
            value=birth_year,
            status=FactStatus.KNOWN if birth_year is not None else FactStatus.UNKNOWN,
            confidence=1.0 if parsed_birth_year is not None else (0.9 if birth_year is not None else 0.0),
            source_text=birth_source,
        )

        parsed_age, age_source = _extract_exact_age(raw)
        exact_age = parsed_age if parsed_age is not None else (search.age if search else None)
        age_min = search.age_min if search else None
        age_max = search.age_max if search else None
        if exact_age is not None:
            age = AgeInterval(exact_age, exact_age, FactStatus.KNOWN, reference)
        elif birth_year is not None:
            maximum = reference.year - birth_year
            minimum = max(0, maximum - 1)
            age = AgeInterval(minimum, maximum, FactStatus.DERIVED, reference)
        elif age_min is not None or age_max is not None:
            age = AgeInterval(age_min, age_max, FactStatus.AMBIGUOUS, reference)
        else:
            age = AgeInterval(as_of=reference)

        if birth_year is not None and exact_age is not None:
            possible = {reference.year - birth_year, max(0, reference.year - birth_year - 1)}
            if exact_age not in possible:
                conflicts.append(FactIssue(
                    "AGE_BIRTH_YEAR_CONFLICT",
                    f"出生年份{birth_year}年与年龄{exact_age}岁不一致",
                    ("birth_year", "age"),
                    True,
                    "、".join(value for value in (birth_source, age_source) if value),
                ))

        duration, duration_source = _extract_duration_months(raw)
        if duration is None and search:
            duration = search.disease_duration_months
        duration_fact = FactValue(
            duration,
            FactStatus.KNOWN if duration is not None else FactStatus.UNKNOWN,
            1.0 if duration_source else (0.9 if duration is not None else 0.0),
            duration_source,
        )
        if duration is not None and age.maximum_years is not None and duration > age.maximum_years * 12:
            conflicts.append(FactIssue(
                "DISEASE_DURATION_EXCEEDS_AGE",
                "疾病持续时间长于患者年龄",
                ("disease_duration", "age"),
                True,
                duration_source,
            ))

        sex_value = search.sex if search else "unknown"
        if _sex_conflict(raw):
            conflicts.append(FactIssue(
                "SEX_CONFLICT",
                "同一条信息中出现了互相冲突的性别描述",
                ("sex",),
                True,
                None,
            ))
            sex_fact = FactValue(sex_value, FactStatus.CONFLICTED, 0.0, None)
        else:
            sex_fact = FactValue(
                sex_value,
                FactStatus.KNOWN if sex_value in {"male", "female"} else FactStatus.UNKNOWN,
                0.99 if sex_value in {"male", "female"} else 0.0,
                None,
            )

        diseases: tuple[str, ...] = ()
        canonical: tuple[str, ...] = ()
        if search and search.raw_disease:
            diseases = (search.raw_disease,)
        elif search and search.disease:
            diseases = (search.disease,)
        if search and search.canonical_disease:
            canonical = (search.canonical_disease,)
        elif diseases:
            canonical = diseases
        if not diseases:
            missing.append(FactIssue("DISEASE_MISSING", "需要提供要查询的疾病或适应症", ("diseases",), True))

        precise = bool(search and search.precise_eligibility) or bool(
            re.search(r"(?:精准|精确).*(?:判断|匹配)|(?:是否|能否|可否).*(?:适合|入组|参加)", raw)
        )
        if age.minimum_years is None:
            missing.append(FactIssue(
                "AGE_MISSING",
                "尚未提供年龄或出生年份",
                ("age",),
                False,
            ))
        if sex_fact.status == FactStatus.UNKNOWN:
            missing.append(FactIssue("SEX_MISSING", "尚未提供性别", ("sex",), False))

        if age.status == FactStatus.AMBIGUOUS:
            ambiguities.append(FactIssue(
                "AGE_AMBIGUOUS",
                "年龄是范围或模糊表达，需要在接近试验边界时进一步确认",
                ("age",),
                False,
            ))

        return PatientFacts(
            schema_version="1.0-lite",
            reference_date=reference,
            birth_year=birth_fact,
            age=age,
            sex=sex_fact,
            diseases=diseases,
            canonical_diseases=canonical,
            disease_duration_months=duration_fact,
            disease_stage=FactValue(
                search.disease_stage if search else None,
                FactStatus.KNOWN if search and search.disease_stage else FactStatus.UNKNOWN,
                0.9 if search and search.disease_stage else 0.0,
                None,
            ),
            treatment_line=FactValue(
                search.treatment_line if search else None,
                FactStatus.KNOWN if search and search.treatment_line else FactStatus.UNKNOWN,
                0.9 if search and search.treatment_line else 0.0,
                None,
            ),
            prior_treatments=search.prior_treatments if search else (),
            biomarkers=search.biomarkers if search else (),
            scores=search.scores if search else (),
            comorbidities=search.comorbidities if search else (),
            disease_statuses=search.disease_statuses if search else (),
            organ_function_findings=search.organ_function_findings if search else (),
            reproductive_statuses=search.reproductive_statuses if search else (),
            allergies=search.allergies if search else (),
            city=search.city if search else None,
            region=search.region if search else None,
            lookback_months=search.lookback_months if search else None,
            precise_eligibility=precise,
            missing=tuple(missing),
            conflicts=tuple(conflicts),
            ambiguities=tuple(ambiguities),
            provenance={
                "builder": "patient-facts-lite-v1",
                "source_text_present": bool(raw),
            },
        )
