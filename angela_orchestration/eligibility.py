from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Any, Iterable

from .patient_facts import FactStatus, PatientFacts


class EligibilityState(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class TrialDisposition(str, Enum):
    POTENTIAL_MATCH = "POTENTIAL_MATCH"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    EXPLICIT_CONFLICT = "EXPLICIT_CONFLICT"


@dataclass(frozen=True)
class GuardFinding:
    category: str
    state: EligibilityState
    reason_code: str
    patient_value: Any = None
    trial_requirement: Any = None
    evidence: str | None = None
    source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "state": self.state.value,
            "reason_code": self.reason_code,
            "patient_value": self.patient_value,
            "trial_requirement": self.trial_requirement,
            "evidence": self.evidence,
            "source": self.source,
        }


@dataclass(frozen=True)
class TrialGuardResult:
    registration_no: str
    disposition: TrialDisposition
    findings: tuple[GuardFinding, ...]
    missing_categories: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "registration_no": self.registration_no,
            "disposition": self.disposition.value,
            "findings": [finding.as_dict() for finding in self.findings],
            "missing_categories": list(self.missing_categories),
            "metadata": self.metadata,
        }


_AGE_RANGE_PATTERNS = (
    re.compile(r"(?:年龄)?\s*(\d{1,3})\s*(?:岁|周岁)?\s*(?:[-—~～至到])\s*(\d{1,3})\s*(?:岁|周岁)"),
    re.compile(
        r"(\d{1,3})\s*(?:岁|周岁)?\s*(?:≤|<)\s*(?:年龄)?\s*(?:≤|<)\s*"
        r"(\d{1,3})\s*(?:岁|周岁)"
    ),
    re.compile(
        r"(?:年龄)?\s*(\d{1,3})\s*(?:岁|周岁)\s*(?:且|并且|，|,)\s*"
        r"(?:年龄)?\s*(?:≤|<=|小于等于|不超过)\s*(\d{1,3})\s*(?:岁|周岁)?"
    ),
    re.compile(
        r"(?:≥|>=|大于等于|不低于|不少于)\s*(\d{1,3})\s*岁?.{0,24}?"
        r"(?:≤|<=|小于等于|不超过)\s*(\d{1,3})\s*岁"
    ),
)
_MIN_AGE = re.compile(r"(?:≥|>=|大于等于|不低于|不少于|年满)\s*(\d{1,3})\s*(?:岁|周岁)")
_MAX_AGE = re.compile(r"(?:≤|<=|小于等于|不超过)\s*(\d{1,3})\s*(?:岁|周岁)")

_CATEGORY_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "disease_stage": (
        re.compile(r"(?:I|II|III|IV|Ⅰ|Ⅱ|Ⅲ|Ⅳ|一|二|三|四)\s*期", re.IGNORECASE),
        re.compile(r"(?:局限期|晚期|转移性|复发|难治|进展)"),
    ),
    "biomarker": (
        re.compile(r"(?:突变|融合|扩增|缺失|阳性|阴性|PD-?L1|HER2|EGFR|KRAS|BRAF|HRAS|MET)", re.IGNORECASE),
    ),
    "score": (
        re.compile(r"(?:SLEDAI(?:-2K)?|BILAG|ECOG|评分|量表)", re.IGNORECASE),
    ),
    "prior_treatment": (
        re.compile(r"(?:既往|经治|初治|一线|二线|三线|后线|化疗|放疗|靶向|免疫|ADC|洗脱期)"),
    ),
    "disease_duration": (
        re.compile(r"(?:病史|病程|确诊).{0,12}?(?:年|月|周)"),
    ),
    "comorbidity": (
        re.compile(r"(?:合并症|其他恶性肿瘤|第二原发|自身免疫病|心脏病|高血压|糖尿病|感染|结核|乙肝|丙肝|HIV)", re.IGNORECASE),
    ),
    "organ_function": (
        re.compile(r"(?:肝功能|肾功能|心功能|肌酐|胆红素|转氨酶|血红蛋白|中性粒细胞|血小板)"),
    ),
    "reproductive": (
        re.compile(r"(?:妊娠|怀孕|哺乳|备孕|避孕|生育)"),
    ),
    "allergy": (
        re.compile(r"(?:过敏|超敏反应)"),
    ),
}


def _criteria_text(trial: dict[str, Any], kind: str) -> str:
    values = trial.get(f"{kind}_criteria") or []
    if isinstance(values, list):
        parts = [str(item.get("raw_text") or "") if isinstance(item, dict) else str(item) for item in values]
        if any(parts):
            return "\n".join(parts)
    return str(trial.get(f"{kind}_criteria_raw") or "")


def _extract_age_requirement(text: str) -> tuple[int | None, int | None, str | None]:
    for pattern in _AGE_RANGE_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1)), int(match.group(2)), match.group(0)
    minimum = _MIN_AGE.search(text)
    maximum = _MAX_AGE.search(text)
    if minimum or maximum:
        evidence = "；".join(match.group(0) for match in (minimum, maximum) if match)
        return (
            int(minimum.group(1)) if minimum else None,
            int(maximum.group(1)) if maximum else None,
            evidence,
        )
    return None, None, None


def _interval_assessment(
    patient_min: int | None,
    patient_max: int | None,
    trial_min: int | None,
    trial_max: int | None,
) -> EligibilityState:
    if trial_min is None and trial_max is None:
        return EligibilityState.NOT_APPLICABLE
    if patient_min is None or patient_max is None:
        return EligibilityState.UNKNOWN
    lower = trial_min if trial_min is not None else 0
    upper = trial_max if trial_max is not None else 120
    if patient_max < lower or patient_min > upper:
        return EligibilityState.FAIL
    if lower <= patient_min and patient_max <= upper:
        return EligibilityState.PASS
    return EligibilityState.UNKNOWN


def _contains_any(text: str, values: Iterable[str]) -> str | None:
    folded = text.casefold()
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned.casefold() in folded:
            return cleaned
    return None


def _detected_categories(text: str) -> set[str]:
    return {
        category
        for category, patterns in _CATEGORY_PATTERNS.items()
        if any(pattern.search(text) for pattern in patterns)
    }


@dataclass(frozen=True)
class _ParsedPublicRules:
    trial_min_age: int | None
    trial_max_age: int | None
    age_evidence: str | None
    age_source: str | None
    female_only: bool
    male_only: bool
    sex_unrestricted: bool
    healthy_participants_only: bool
    participant_type_evidence: str | None
    detected_categories: tuple[str, ...]


@lru_cache(maxsize=4096)
def _parse_public_rules(
    version_content_hash: str,
    inclusion: str,
    exclusion: str,
    public_fields: str,
) -> _ParsedPublicRules:
    """Cache deterministic parsing by immutable CDE trial-version content."""
    del version_content_hash  # It is intentionally part of the cache identity.
    trial_min, trial_max, age_evidence = _extract_age_requirement(inclusion)
    age_source = "inclusion_criteria" if age_evidence else None
    public_scope = f"{inclusion}\n{public_fields}"
    compact = re.sub(r"\s+", "", public_scope)
    has_adult = "成人" in compact
    has_child = bool(re.search(r"儿童|儿科|未成年|青少年", compact))
    participant_scope = re.sub(r"\s+", "", f"{public_fields}\n{inclusion}")
    healthy_match = re.search(
        r"健康(?:成人)?(?:参与者|受试者|志愿者|人群)|healthy(?:adult)?(?:participants?|subjects?|volunteers?)",
        participant_scope,
        re.IGNORECASE,
    )
    patient_population = bool(re.search(r"患者|病人|患有|确诊", participant_scope))
    if trial_min is None and has_adult and not has_child:
        trial_min = 18
        age_evidence = re.search(r"成人(?:患者|受试者|参与者)?", compact).group(0)
        age_source = "public_trial_fields"
    if trial_max is None and has_child and not has_adult:
        trial_max = 17
        child = re.search(r"(?:儿童|儿科|未成年|青少年)(?:患者|受试者|参与者)?", compact)
        age_evidence = child.group(0) if child else "儿童/未成年人"
        age_source = "public_trial_fields"
    return _ParsedPublicRules(
        trial_min_age=trial_min,
        trial_max_age=trial_max,
        age_evidence=age_evidence,
        age_source=age_source,
        female_only=bool(re.search(
            r"仅限女性|只招募女性|仅招募女性|只允许女性"
            r"|(?:^|[，,；;。])女性(?:受试者|患者|参与者)(?:$|[，,；;。])"
            r"|女性(?:受试者|患者|参与者)(?!必须|应当|需要|需|须|应)"
            r"|在女性.{0,30}?(?:患者|受试者|参与者)中",
            compact,
        )),
        male_only=bool(re.search(
            r"仅限男性|只招募男性|仅招募男性|只允许男性"
            r"|(?:^|[，,；;。])男性(?:受试者|患者|参与者)(?:$|[，,；;。])"
            r"|男性(?:受试者|患者|参与者)(?!必须|应当|需要|需|须|应)"
            r"|在男性.{0,30}?(?:患者|受试者|参与者)中",
            compact,
        )),
        sex_unrestricted=bool(re.search(r"男女不限|性别不限|男性和女性|男性或女性", compact)),
        healthy_participants_only=bool(healthy_match and not patient_population),
        participant_type_evidence=healthy_match.group(0) if healthy_match else None,
        detected_categories=tuple(sorted(_detected_categories(f"{inclusion}\n{exclusion}"))),
    )


class EligibilityGuard:
    """Evidence-bound eligibility guard.

    It only emits FAIL for deterministic contradictions backed by public trial
    criteria. Semantic or incomplete conditions remain UNKNOWN. This keeps broad
    real-world questions usable without presenting model guesses as eligibility.
    """

    def evaluate(self, facts: PatientFacts, trial: dict[str, Any]) -> TrialGuardResult:
        inclusion = _criteria_text(trial, "inclusion")
        exclusion = _criteria_text(trial, "exclusion")
        findings: list[GuardFinding] = []
        version_content_hash = str(
            trial.get("_version_content_hash")
            or trial.get("content_hash")
            or f"unversioned:{hash((inclusion, exclusion))}"
        )
        public_fields = "\n".join(
            str(trial.get(field) or "")
            for field in ("indication_raw", "public_title", "professional_title")
        )
        parsed_rules = _parse_public_rules(
            version_content_hash,
            inclusion,
            exclusion,
            public_fields,
        )

        if facts.diseases and parsed_rules.healthy_participants_only:
            findings.append(GuardFinding(
                "participant_type",
                EligibilityState.FAIL,
                "PATIENT_QUERY_CONFLICTS_WITH_HEALTHY_PARTICIPANTS_ONLY",
                "patient_with_condition",
                "healthy_participants_only",
                parsed_rules.participant_type_evidence,
                "public_trial_fields",
            ))

        trial_min = parsed_rules.trial_min_age
        trial_max = parsed_rules.trial_max_age
        age_evidence = parsed_rules.age_evidence
        age_state = _interval_assessment(
            facts.age.minimum_years,
            facts.age.maximum_years,
            trial_min,
            trial_max,
        )
        findings.append(GuardFinding(
            "age",
            age_state,
            "AGE_EXPLICIT_MISMATCH" if age_state == EligibilityState.FAIL else "AGE_ASSESSED",
            [facts.age.minimum_years, facts.age.maximum_years],
            [trial_min, trial_max] if trial_min is not None or trial_max is not None else None,
            age_evidence,
            parsed_rules.age_source,
        ))

        sex_finding = self._sex_finding(facts, parsed_rules)
        findings.append(sex_finding)

        disease_text = public_fields
        matched_disease = _contains_any(
            disease_text,
            (*facts.diseases, *facts.canonical_diseases),
        )
        findings.append(GuardFinding(
            "disease",
            EligibilityState.PASS if matched_disease else EligibilityState.UNKNOWN,
            "DISEASE_PUBLIC_FIELD_MATCH" if matched_disease else "DISEASE_REQUIRES_CONFIRMATION",
            list(facts.diseases),
            matched_disease,
            matched_disease,
            "public_trial_fields" if matched_disease else None,
        ))

        self._append_semantic_finding(
            findings,
            "disease_stage",
            (str(facts.disease_stage.value),) if facts.disease_stage.status == FactStatus.KNOWN else (),
            inclusion,
        )
        self._append_semantic_finding(findings, "biomarker", facts.biomarkers, inclusion)
        self._append_review_finding(findings, "score", facts.scores, inclusion)
        self._append_semantic_finding(findings, "prior_treatment", facts.prior_treatments, inclusion)
        self._append_semantic_finding(findings, "comorbidity", facts.comorbidities, inclusion)
        self._append_semantic_finding(findings, "disease_status", facts.disease_statuses, inclusion)
        self._append_review_finding(
            findings, "organ_function", facts.organ_function_findings, f"{inclusion}\n{exclusion}"
        )
        self._append_review_finding(
            findings, "reproductive", facts.reproductive_statuses, f"{inclusion}\n{exclusion}"
        )
        self._append_review_finding(
            findings, "allergy", facts.allergies, f"{inclusion}\n{exclusion}"
        )
        if facts.treatment_line.status == FactStatus.KNOWN:
            self._append_semantic_finding(
                findings, "treatment_line", (str(facts.treatment_line.value),), inclusion
            )

        detected = set(parsed_rules.detected_categories)
        supplied_categories = {
            "disease_stage" if facts.disease_stage.status == FactStatus.KNOWN else "",
            "biomarker" if facts.biomarkers else "",
            "score" if facts.scores else "",
            "prior_treatment" if facts.prior_treatments else "",
            "comorbidity" if facts.comorbidities else "",
            "disease_duration" if facts.disease_duration_months.status == FactStatus.KNOWN else "",
            "organ_function" if facts.organ_function_findings else "",
            "reproductive" if facts.reproductive_statuses else "",
            "allergy" if facts.allergies else "",
        }
        missing_categories = tuple(sorted(category for category in detected if category not in supplied_categories))
        for category in missing_categories:
            findings.append(GuardFinding(
                category,
                EligibilityState.UNKNOWN,
                "PATIENT_FACT_MISSING_FOR_PUBLIC_CRITERION",
                None,
                "公开入排标准包含该类条件",
                None,
                "public_criteria",
            ))

        disposition = TrialDisposition.POTENTIAL_MATCH
        if any(finding.state == EligibilityState.FAIL for finding in findings):
            disposition = TrialDisposition.EXPLICIT_CONFLICT
        elif any(finding.state == EligibilityState.UNKNOWN for finding in findings):
            disposition = TrialDisposition.NEEDS_CONFIRMATION
        return TrialGuardResult(
            registration_no=str(trial.get("registration_no") or ""),
            disposition=disposition,
            findings=tuple(findings),
            missing_categories=missing_categories,
            metadata={
                "guard_version": "eligibility-guard-v1",
                "evidence_policy": "public_evidence_required_for_pass_or_fail",
                "rule_cache_key": version_content_hash,
            },
        )

    @staticmethod
    def _sex_finding(facts: PatientFacts, parsed_rules: _ParsedPublicRules) -> GuardFinding:
        female_only = parsed_rules.female_only
        male_only = parsed_rules.male_only
        explicit_both = parsed_rules.sex_unrestricted or (female_only and male_only)
        patient = str(facts.sex.value or "unknown")
        if explicit_both or (not female_only and not male_only):
            state = EligibilityState.NOT_APPLICABLE
            requirement = "不限" if explicit_both else None
        elif patient == "unknown":
            state = EligibilityState.UNKNOWN
            requirement = "female" if female_only else "male"
        else:
            requirement = "female" if female_only else "male"
            state = EligibilityState.PASS if patient == requirement else EligibilityState.FAIL
        evidence = None
        if female_only:
            evidence = "仅限女性"
        elif male_only:
            evidence = "仅限男性"
        elif explicit_both:
            evidence = "性别不限"
        return GuardFinding(
            "sex",
            state,
            "SEX_EXPLICIT_MISMATCH" if state == EligibilityState.FAIL else "SEX_ASSESSED",
            patient,
            requirement,
            evidence,
            "inclusion_criteria" if evidence else None,
        )

    @staticmethod
    def _append_semantic_finding(
        findings: list[GuardFinding],
        category: str,
        patient_values: Iterable[str],
        criteria_text: str,
    ) -> None:
        values = tuple(value for value in patient_values if value)
        if not values:
            return
        match = _contains_any(criteria_text, values)
        findings.append(GuardFinding(
            category,
            EligibilityState.PASS if match else EligibilityState.UNKNOWN,
            "EXACT_PUBLIC_TEXT_EVIDENCE" if match else "SEMANTIC_REVIEW_REQUIRED",
            list(values),
            match,
            match,
            "public_criteria" if match else None,
        ))

    @staticmethod
    def _append_review_finding(
        findings: list[GuardFinding],
        category: str,
        patient_values: Iterable[str],
        criteria_text: str,
    ) -> None:
        values = tuple(value for value in patient_values if value)
        if not values:
            return
        match = _contains_any(criteria_text, values)
        findings.append(GuardFinding(
            category,
            EligibilityState.UNKNOWN,
            "THRESHOLD_OR_CLINICAL_REVIEW_REQUIRED",
            list(values),
            match,
            match,
            "public_criteria" if match else None,
        ))


def filter_guarded_results(
    results: list[dict[str, Any]],
    evaluations: dict[str, TrialGuardResult],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    counts = {disposition.value: 0 for disposition in TrialDisposition}
    accepted: list[dict[str, Any]] = []
    for result in results:
        ctr = str(result.get("registration_no") or "")
        evaluation = evaluations.get(ctr)
        if evaluation is None:
            evaluation = TrialGuardResult(
                ctr,
                TrialDisposition.NEEDS_CONFIRMATION,
                (),
                metadata={"reason": "guard_evaluation_missing"},
            )
        counts[evaluation.disposition.value] += 1
        result = dict(result)
        result["eligibility_guard"] = evaluation.as_dict()
        result["needs_confirmation"] = list(dict.fromkeys([
            *(result.get("needs_confirmation") or []),
            *(evaluation.missing_categories or ()),
        ]))
        if evaluation.disposition != TrialDisposition.EXPLICIT_CONFLICT:
            accepted.append(result)
        if len(accepted) >= limit:
            break
    return accepted, counts
