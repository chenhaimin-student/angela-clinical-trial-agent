from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from angela_orchestration.fast_path import _months_ago
from angela_orchestration.models import SearchParameters


POSITIVE_FIELDS = {
    "indication_raw": 100,
    "public_title": 90,
    "professional_title": 90,
    "inclusion_criteria_raw": 70,
    "drug_name_raw": 50,
}
REGION_PROVINCES = {
    "华东": ("上海", "上海市", "江苏", "江苏省", "浙江", "浙江省", "安徽", "安徽省", "福建", "福建省", "江西", "江西省", "山东", "山东省"),
    "华北": ("北京", "北京市", "天津", "天津市", "河北", "河北省", "山西", "山西省", "内蒙古", "内蒙古自治区"),
    "华南": ("广东", "广东省", "广西", "广西壮族自治区", "海南", "海南省"),
    "华中": ("河南", "河南省", "湖北", "湖北省", "湖南", "湖南省"),
    "西南": ("重庆", "重庆市", "四川", "四川省", "贵州", "贵州省", "云南", "云南省", "西藏", "西藏自治区"),
    "西北": ("陕西", "陕西省", "甘肃", "甘肃省", "青海", "青海省", "宁夏", "宁夏回族自治区", "新疆", "新疆维吾尔自治区"),
    "东北": ("辽宁", "辽宁省", "吉林", "吉林省", "黑龙江", "黑龙江省"),
}


def _location_variants(value: str) -> tuple[str, ...]:
    """Return auditable aliases used by the strict evaluation oracle.

    User-facing parsers intentionally normalize ``魔都`` to ``上海`` while
    CDE site rows commonly store ``上海市``.  Treating those values as exact
    strings made the old evaluator agree with a false-empty production answer.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return ()
    variants = {cleaned}
    for suffix in ("市", "省"):
        if cleaned.endswith(suffix):
            variants.add(cleaned[: -len(suffix)])
    base = cleaned[:-1] if cleaned.endswith("市") else cleaned
    if base:
        variants.add(base + "市")
    return tuple(sorted(variants))


@dataclass(frozen=True)
class OracleResult:
    raw_total_count: int
    raw_ranked_ids: tuple[str, ...]
    final_ranked_ids: tuple[str, ...]
    records: dict[str, dict[str, Any]]


def _contains(text: str, term: str) -> bool:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+/-]*", term):
        return re.search(
            rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
            text,
            flags=re.IGNORECASE,
        ) is not None
    return term.casefold() in text.casefold()


def _phase_values(value: str) -> set[str]:
    normalized = (value or "").upper().translate(str.maketrans({
        "Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV",
        "１": "I", "２": "II", "３": "III", "４": "IV",
    }))
    found = set(re.findall(r"(?<![A-Z])(IV|III|II|I)(?=[AB]?\s*(?:期|/|\\|、|和|至|到|\+|$))", normalized))
    for number, phase in (("1", "I"), ("2", "II"), ("3", "III"), ("4", "IV")):
        if re.search(rf"(?<!\d){number}\s*期", normalized):
            found.add(phase)
    return found


def _age_interval_excluded(
    patient_min: int | None,
    patient_max: int | None,
    inclusion: str,
    public_text: str,
) -> bool:
    if patient_min is None and patient_max is None:
        return False
    patient_min = patient_min if patient_min is not None else 0
    patient_max = patient_max if patient_max is not None else 120
    compact = re.sub(r"\s+", "", inclusion or "")
    ranges = (
        r"(?:年龄)?(\d{1,3})(?:岁|周岁)?(?:[-—~～至到])(\d{1,3})(?:岁|周岁)",
        r"(\d{1,3})(?:岁|周岁)(?:≤|<)(?:年龄)?(?:≤|<)(\d{1,3})(?:岁|周岁)",
        r"(?:年龄)?(\d{1,3})(?:岁|周岁)(?:且|并且|，|,)(?:年龄)?(?:≤|<=|小于等于|不超过)(\d{1,3})(?:岁|周岁)?",
        r"(?:≥|>=|大于等于|不低于|不少于)(\d{1,3})岁?.{0,20}?(?:≤|<=|小于等于|不超过)(\d{1,3})岁",
    )
    for pattern in ranges:
        match = re.search(pattern, compact)
        if match:
            lower, upper = int(match.group(1)), int(match.group(2))
            return patient_max < lower or patient_min > upper
    minimum = re.search(
        r"(?:≥|>=|大于等于|不低于|不少于|年满)(\d{1,3})(?:周岁|岁)", compact
    )
    maximum = re.search(r"(?:≤|<=|小于等于|不超过)(\d{1,3})(?:周岁|岁)", compact)
    if minimum and patient_max < int(minimum.group(1)):
        return True
    if maximum and patient_min > int(maximum.group(1)):
        return True
    public_compact = re.sub(r"\s+", "", public_text or "")
    if "成人" in public_compact and not re.search(r"儿童|儿科|未成年|青少年", public_compact):
        return patient_max < 18
    if re.search(r"儿童|儿科|未成年|青少年", public_compact) and "成人" not in public_compact:
        return patient_min > 17
    return False


def _age_excluded(search: SearchParameters, inclusion: str, public_text: str) -> bool:
    patient_min = search.age if search.age is not None else search.age_min
    patient_max = search.age if search.age is not None else search.age_max
    return _age_interval_excluded(patient_min, patient_max, inclusion, public_text)


def _sex_excluded(sex: str, inclusion: str) -> bool:
    if sex not in {"male", "female"}:
        return False
    compact = re.sub(r"\s+", "", inclusion or "")
    explicitly_both = bool(re.search(r"男女不限|性别不限|男性.{0,24}女性|女性.{0,24}男性", compact))
    if explicitly_both:
        return False
    female_only = bool(re.search(r"仅限女性|只招募女性|仅女性|绝经后妇女", compact))
    male_only = bool(re.search(r"仅限男性|只招募男性|仅男性", compact))
    return (female_only and not male_only and sex == "male") or (
        male_only and not female_only and sex == "female"
    )


def _healthy_only(public_text: str) -> bool:
    healthy = bool(re.search(
        r"健康(?:成人)?(?:参与者|受试者|志愿者|人群)|healthy(?:adult)?(?:participants?|subjects?|volunteers?)",
        public_text,
        re.IGNORECASE,
    ))
    patient = bool(re.search(r"患者|病人|患有|确诊", public_text))
    return healthy and not patient


class IndependentRetrievalOracle:
    """Independent SQL/Python implementation of the public search contract."""

    def __init__(self, connection: sqlite3.Connection, concepts_path: str | Path):
        self.connection = connection
        self.concepts = json.loads(Path(concepts_path).read_text(encoding="utf-8"))["concepts"]

    def search(
        self,
        search: SearchParameters,
        *,
        reference_date: date,
        patient_age_min: int | None = None,
        patient_age_max: int | None = None,
        patient_sex: str | None = None,
    ) -> OracleResult:
        terms = self._terms(search)
        rows = self._rows(search, terms, reference_date)
        ranked: list[dict[str, Any]] = []
        final: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if search.trial_phases and not set(search.trial_phases).intersection(
                _phase_values(str(item.get("trial_phase_raw") or ""))
            ):
                continue
            score = self._score(item, terms)
            if score <= 0:
                continue
            inclusion = str(item.get("inclusion_criteria_raw") or "")
            public = "\n".join(str(item.get(field) or "") for field in (
                "indication_raw", "public_title", "professional_title", "inclusion_criteria_raw"
            ))
            if _age_excluded(search, inclusion, public) or _sex_excluded(search.sex, inclusion):
                continue
            item["oracle_score"] = score
            ranked.append(item)
            final_age_excluded = _age_interval_excluded(
                patient_age_min, patient_age_max, inclusion, public
            )
            if not final_age_excluded and not _sex_excluded(patient_sex or search.sex, inclusion) and not _healthy_only(public):
                final.append(item)
        key = lambda item: (
            int(item["oracle_score"]), item.get("first_publication_date") or "",
            item.get("source_update_date") or "", item.get("status_changed_at") or "",
            item["registration_no"],
        )
        ranked.sort(key=key, reverse=True)
        final.sort(key=key, reverse=True)
        # Preserve source rows for every independently accepted search hit so
        # user-visible fields can be checked directly against the formal DB.
        records = {str(item["registration_no"]): item for item in ranked}
        return OracleResult(
            raw_total_count=len(ranked),
            raw_ranked_ids=tuple(str(item["registration_no"]) for item in ranked),
            final_ranked_ids=tuple(str(item["registration_no"]) for item in final),
            records=records,
        )

    def _terms(self, search: SearchParameters) -> list[tuple[str, int, str]]:
        values: list[tuple[str, int, str]] = [(search.disease.strip(), 100, "original")]
        lookup = search.disease.strip().casefold()
        for concept in self.concepts:
            names = [concept.get("canonical"), *(concept.get("synonyms") or [])]
            if lookup not in {str(value or "").casefold() for value in names}:
                continue
            values.append((str(concept["canonical"]), 100, "canonical"))
            values.extend((str(value), 95, "synonym") for value in concept.get("synonyms") or [])
            if concept.get("parent"):
                values.append((str(concept["parent"]), 60, "parent"))
            if not search.preserve_specificity:
                values.extend((str(value), 80, "related") for value in concept.get("related") or [])
            break
        if search.canonical_disease:
            values.append((search.canonical_disease, 85, "agent_canonical"))
        dedup: dict[str, tuple[str, int, str]] = {}
        for term, weight, kind in values:
            key = term.casefold()
            if term and (key not in dedup or weight > dedup[key][1]):
                dedup[key] = (term, weight, kind)
        return list(dedup.values())

    def _rows(
        self, search: SearchParameters, terms: list[tuple[str, int, str]], reference_date: date
    ) -> list[sqlite3.Row]:
        sql = [
            "SELECT t.registration_no,t.detail_url,t.public_title,t.professional_title,"
            "t.indication_raw,t.drug_name_raw,t.trial_phase_raw,t.recruitment_status_raw,"
            "t.overall_status_raw,t.recruitment_status_normalized,"
            "t.first_publication_date,t.source_update_date,t.status_changed_at,"
            "v.inclusion_criteria_raw,v.exclusion_criteria_raw FROM trials t "
            "JOIN trial_versions v ON v.id=t.current_version_id WHERE t.is_active=1 "
            "AND t.recruitment_status_normalized IN ('recruiting','not_yet_recruiting')"
        ]
        parameters: list[Any] = []
        clauses = []
        for term, _weight, _kind in terms:
            clauses.append("(" + " OR ".join([
                "COALESCE(t.indication_raw,'') LIKE ?",
                "COALESCE(t.public_title,'') LIKE ?",
                "COALESCE(t.professional_title,'') LIKE ?",
                "COALESCE(t.drug_name_raw,'') LIKE ?",
                "COALESCE(v.inclusion_criteria_raw,'') LIKE ?",
            ]) + ")")
            parameters.extend([f"%{term}%"] * 5)
        sql.append("AND (" + " OR ".join(clauses) + ")")
        if search.city:
            cities = _location_variants(search.city)
            marks = ",".join("?" for _ in cities)
            sql.append(
                "AND EXISTS(SELECT 1 FROM trial_sites s WHERE s.trial_version_id=t.current_version_id "
                f"AND (s.city IN ({marks}) OR s.province IN ({marks})))"
            )
            parameters.extend(cities)
            parameters.extend(cities)
        if search.region and search.region in REGION_PROVINCES:
            provinces = REGION_PROVINCES[search.region]
            marks = ",".join("?" for _ in provinces)
            sql.append(
                "AND EXISTS(SELECT 1 FROM trial_sites s WHERE s.trial_version_id=t.current_version_id "
                f"AND (s.province IN ({marks}) OR s.city IN ({marks})))"
            )
            parameters.extend(provinces)
            parameters.extend(provinces)
        if search.lookback_months:
            lower = _months_ago(reference_date, search.lookback_months).isoformat()
            upper = reference_date.isoformat()
            sql.append(
                "AND ((t.first_publication_date BETWEEN ? AND ?) OR "
                "(t.source_update_date BETWEEN ? AND ?) OR "
                "(t.status_changed_at BETWEEN ? AND ?))"
            )
            parameters.extend([lower, upper, lower, upper, lower, upper + "T23:59:59"])
        return self.connection.execute(" ".join(sql), parameters).fetchall()

    @staticmethod
    def _score(item: dict[str, Any], terms: list[tuple[str, int, str]]) -> int:
        direct_scores: list[int] = []
        parent_scores: list[int] = []
        inclusion_scores: list[int] = []
        for term, term_weight, kind in terms:
            for field, field_weight in POSITIVE_FIELDS.items():
                if _contains(str(item.get(field) or ""), term):
                    value = min(term_weight, field_weight)
                    if field == "inclusion_criteria_raw":
                        if kind != "parent":
                            inclusion_scores.append(value)
                    elif kind == "parent":
                        parent_scores.append(value)
                    else:
                        direct_scores.append(value)
        if direct_scores:
            return max(direct_scores)
        if parent_scores and inclusion_scores:
            return max(parent_scores + inclusion_scores)
        return 0
