from __future__ import annotations

import re

from .models import CdeTrial, PatientProfile, TrialMatch


def preliminary_match(profile: PatientProfile, trial: CdeTrial) -> TrialMatch | None:
    searchable = " ".join(
        value
        for value in (
            trial.indication,
            trial.drug_name,
            trial.public_title,
            trial.professional_title,
            trial.inclusion_criteria,
        )
        if value
    ).casefold()
    matched: list[str] = []
    confirmations: list[str] = ["当前是否仍在招募", "完整入选/排除标准", "研究医生评估"]
    score = 0

    condition_terms = profile.condition_terms + profile.pathology_terms
    for term in condition_terms:
        if term.casefold() in searchable:
            matched.append(f"疾病/病理关键词：{term}")
            score += 3
    if condition_terms and not matched:
        return None

    if profile.sex in {"female", "male"}:
        sex_label = "女性" if profile.sex == "female" else "男性"
        opposite = "男性" if profile.sex == "female" else "女性"
        criteria = (trial.inclusion_criteria or "") + (trial.exclusion_criteria or "")
        if opposite in criteria and sex_label not in criteria:
            return None
        if sex_label in criteria:
            matched.append(f"性别条件：{sex_label}")
            score += 1
        else:
            confirmations.append("性别限制")

    if profile.age is not None:
        age_result = _age_compatibility(profile.age, trial.inclusion_criteria or "")
        if age_result == "excluded":
            return None
        if age_result == "matched":
            matched.append(f"年龄：{profile.age} 岁")
            score += 1
        else:
            confirmations.append("年龄范围")

    if profile.prior_treatments:
        confirmations.append("既往治疗要求")
    if profile.region:
        confirmations.append("试验机构所在地与可及性")
    if not matched:
        return None
    return TrialMatch(
        trial=trial,
        score=score,
        matched_fields=tuple(matched),
        needs_confirmation=tuple(dict.fromkeys(confirmations)),
    )


def _age_compatibility(age: int, criteria: str) -> str:
    if not criteria:
        return "unknown"
    ranges = re.findall(r"(\d{1,3})\s*(?:-|—|至|~)\s*(\d{1,3})\s*岁", criteria)
    if ranges:
        lower, upper = (int(value) for value in ranges[0])
        return "matched" if lower <= age <= upper else "excluded"
    minimum = re.search(r"(?:≥|>=|不少于)\s*(\d{1,3})\s*岁", criteria)
    maximum = re.search(r"(?:≤|<=|不超过)\s*(\d{1,3})\s*岁", criteria)
    if minimum and age < int(minimum.group(1)):
        return "excluded"
    if maximum and age > int(maximum.group(1)):
        return "excluded"
    return "matched" if minimum or maximum else "unknown"
