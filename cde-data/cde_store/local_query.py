from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .contracts import PATIENT_DEFAULT_STATUSES, RecruitmentStatus


DEFAULT_LIMIT = 10
MAX_LIMIT = 20
SEARCH_COLUMNS = (
    "indication_raw", "public_title", "professional_title", "drug_name_raw",
    "inclusion_criteria_raw", "exclusion_criteria_raw",
)
PRIMARY_DISEASE_COLUMNS = (
    "indication_raw", "public_title", "professional_title", "drug_name_raw",
)


@dataclass(frozen=True)
class LocalTrialQuery:
    disease: str = ""
    age: int | None = None
    age_min: int | None = None
    age_max: int | None = None
    sex: str = "unknown"
    city: str | None = None
    province: str | None = None
    region: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    recruitment_statuses: tuple[RecruitmentStatus, ...] = PATIENT_DEFAULT_STATUSES
    limit: int = DEFAULT_LIMIT
    blood_type: str | None = None
    agent_canonical_disease: str | None = None
    agent_candidate_terms: tuple[str, ...] = ()
    trial_phases: tuple[str, ...] = ()
    required_biomarkers: tuple[str, ...] = ()
    sponsors: tuple[str, ...] = ()
    site_names: tuple[str, ...] = ()
    project_codes: tuple[str, ...] = ()
    preserve_specificity: bool = False
    include_restricted_fields: bool = False

    def __post_init__(self) -> None:
        if not any((
            self.disease.strip(), self.required_biomarkers, self.sponsors,
            self.site_names, self.project_codes,
        )):
            raise ValueError("at least one auditable retrieval constraint is required")
        if self.age is not None and not 0 <= self.age <= 120:
            raise ValueError("age must be between 0 and 120")
        if self.age_min is not None and not 0 <= self.age_min <= 120:
            raise ValueError("age_min must be between 0 and 120")
        if self.age_max is not None and not 0 <= self.age_max <= 120:
            raise ValueError("age_max must be between 0 and 120")
        if self.age_min is not None and self.age_max is not None and self.age_min > self.age_max:
            raise ValueError("age_min must be on or before age_max")
        if self.sex not in {"male", "female", "unknown"}:
            raise ValueError("sex must be male, female, or unknown")
        if not 1 <= self.limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        if not self.recruitment_statuses:
            raise ValueError("at least one recruitment status is required")
        if len(self.agent_candidate_terms) > 8:
            raise ValueError("agent_candidate_terms may contain at most 8 terms")
        if len(self.required_biomarkers) > 8:
            raise ValueError("required_biomarkers may contain at most 8 terms")
        if len(self.sponsors) > 8:
            raise ValueError("sponsors may contain at most 8 terms")
        if len(self.site_names) > 8 or len(self.project_codes) > 8:
            raise ValueError("site_names and project_codes may contain at most 8 terms")
        invalid_phases = set(self.trial_phases) - {"I", "II", "III", "IV"}
        if invalid_phases:
            raise ValueError("trial_phases may contain only I, II, III, or IV")


class DiseaseConcepts:
    def __init__(self, path: str | Path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.version = int(payload["version"])
        self.concepts = payload["concepts"]

    def expand(
        self,
        disease: str,
        *,
        agent_canonical: str | None = None,
        agent_candidates: Iterable[str] = (),
        preserve_specificity: bool = False,
    ) -> list[dict[str, Any]]:
        original = disease.strip()
        lookup = original.casefold()
        matched = None
        for concept in self.concepts:
            values = [concept["canonical"], *concept.get("synonyms", [])]
            if lookup in {str(value).casefold() for value in values}:
                matched = concept
                break
        terms: list[dict[str, Any]] = [{"term": original, "kind": "original", "weight": 100}]
        if matched:
            terms.append({"term": matched["canonical"], "kind": "canonical", "weight": 100})
            terms.extend({"term": value, "kind": "synonym", "weight": 95} for value in matched.get("synonyms", []))
            if matched.get("parent"):
                terms.append({
                    "term": str(matched["parent"]),
                    "kind": "parent",
                    "weight": 60,
                })
            if not preserve_specificity:
                terms.extend({"term": value, "kind": "related", "weight": 80} for value in matched.get("related", []))
        if agent_canonical and agent_canonical.strip():
            terms.append({"term": agent_canonical.strip(), "kind": "agent_canonical", "weight": 85})
        terms.extend(
            {"term": value.strip(), "kind": "agent_candidate", "weight": 75}
            for value in agent_candidates if value.strip()
        )
        deduplicated: dict[str, dict[str, Any]] = {}
        for item in terms:
            key = item["term"].casefold()
            if key not in deduplicated or item["weight"] > deduplicated[key]["weight"]:
                deduplicated[key] = item
        return list(deduplicated.values())


class LocalTrialQueryService:
    def __init__(self, connection: sqlite3.Connection, concepts: DiseaseConcepts):
        self.connection = connection
        self.concepts = concepts

    def search(self, query: LocalTrialQuery) -> dict[str, Any]:
        terms = self.concepts.expand(
            query.disease,
            agent_canonical=query.agent_canonical_disease,
            agent_candidates=query.agent_candidate_terms,
            preserve_specificity=query.preserve_specificity,
        ) if query.disease.strip() else []
        rows = self._candidate_rows(query, terms)
        accepted = []
        excluded = {"age": 0, "sex": 0, "biomarker": 0}
        for row in rows:
            item = dict(row)
            if query.trial_phases and not set(query.trial_phases).intersection(
                extract_trial_phases(item.get("trial_phase_raw") or "")
            ):
                continue
            evidence = self._disease_evidence(item, terms)
            direct_primary_evidence = [
                entry for entry in evidence
                if entry["field"] in PRIMARY_DISEASE_COLUMNS
                and entry["term_kind"] != "parent"
            ]
            parent_primary_evidence = [
                entry for entry in evidence
                if entry["field"] in PRIMARY_DISEASE_COLUMNS
                and entry["term_kind"] == "parent"
            ]
            specific_inclusion_evidence = [
                entry for entry in evidence
                if entry["field"] == "inclusion_criteria_raw"
                and entry["term_kind"] != "parent"
            ]
            contextual_inclusion_match = bool(
                parent_primary_evidence and specific_inclusion_evidence
            )
            if query.disease and not direct_primary_evidence and not contextual_inclusion_match:
                continue
            age_state = age_range_compatibility(
                query.age,
                query.age_min,
                query.age_max,
                item["inclusion_criteria_raw"] or "",
            )
            sex_state = sex_compatibility(query.sex, item["inclusion_criteria_raw"] or "")
            if age_state == "excluded":
                excluded["age"] += 1
                continue
            if sex_state == "excluded":
                excluded["sex"] += 1
                continue
            biomarker_evidence = self._required_biomarker_evidence(
                item, query.required_biomarkers
            )
            if query.required_biomarkers and len(biomarker_evidence) != len(
                query.required_biomarkers
            ):
                excluded["biomarker"] += 1
                continue
            sponsor_evidence = self._required_sponsor_evidence(item, query.sponsors)
            if query.sponsors and len(sponsor_evidence) != len(query.sponsors):
                continue
            project_evidence = self._required_project_evidence(item, query.project_codes)
            if query.project_codes and len(project_evidence) != len(query.project_codes):
                continue
            result = self._public_result(item)
            scoring_evidence = direct_primary_evidence or (
                parent_primary_evidence + specific_inclusion_evidence
            )
            constraint_scoring = [
                *({**entry, "score": 100} for entry in sponsor_evidence),
                *({**entry, "score": 95} for entry in biomarker_evidence),
                *({**entry, "score": 100} for entry in project_evidence),
            ]
            result["match_score"] = max(
                (entry["score"] for entry in (*scoring_evidence, *constraint_scoring)),
                default=0,
            )
            result["match_evidence"] = [*evidence, *constraint_scoring]
            result["exclusion_risk_evidence"] = [
                entry for entry in evidence if entry["field"] == "exclusion_criteria_raw"
            ]
            result["age_assessment"] = age_state
            result["sex_assessment"] = sex_state
            result["needs_confirmation"] = [
                label for state, label in (
                    (age_state, "年龄范围"), (sex_state, "性别限制"),
                ) if state == "unknown"
            ]
            result["constraint_evidence"] = {
                "biomarkers": biomarker_evidence,
                "sponsors": sponsor_evidence,
                "project_codes": project_evidence,
            }
            if query.city:
                result["city_match"] = query.city.strip()
            if query.include_restricted_fields:
                result.update(self._restricted_fields(item["current_version_id"]))
            accepted.append(result)

        accepted.sort(key=self._sort_key, reverse=True)
        total = len(accepted)
        response = {
            "total_count": total,
            "returned_count": min(total, query.limit),
            "results": accepted[: query.limit],
            "query": {
                "disease": query.disease,
                "expanded_terms": terms,
                "date_semantics": "first_publication_or_source_update_or_status_changed",
                "date_from": query.date_from.isoformat() if query.date_from else None,
                "date_to": query.date_to.isoformat() if query.date_to else None,
                "statuses": [str(status) for status in query.recruitment_statuses],
                "limit": query.limit,
                "city": query.city,
                "province": query.province,
                "region": query.region,
                "age_min": query.age_min,
                "age_max": query.age_max,
                "trial_phases": list(query.trial_phases),
                "required_biomarkers": list(query.required_biomarkers),
                "sponsors": list(query.sponsors),
                "site_names": list(query.site_names),
                "project_codes": list(query.project_codes),
            },
            "excluded_counts": excluded,
            "warnings": [],
            "data_source": "local_sqlite",
            "requires_live_verification": False,
        }
        if query.blood_type:
            response["warnings"].append("血型已接收，但当前版本不将血型用于CDE匹配。")
        return response

    def get_trial(self, registration_no: str, *, include_restricted_fields: bool = False) -> dict[str, Any] | None:
        ctr = registration_no.strip().upper()
        if not re.fullmatch(r"CTR\d{6,}", ctr):
            raise ValueError("registration_no must be a CTR number")
        row = self.connection.execute(
            "SELECT t.*,v.inclusion_criteria_raw,v.exclusion_criteria_raw,"
            "v.content_hash AS version_content_hash,v.version_no AS current_version_no "
            "FROM trials t JOIN trial_versions v ON v.id=t.current_version_id "
            "WHERE t.registration_no=? AND t.is_active=1",
            (ctr,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        result = self._public_result(item)
        result["_trial_version_id"] = item["current_version_id"]
        result["_version_content_hash"] = item["version_content_hash"]
        result["_current_version_no"] = item["current_version_no"]
        result["inclusion_criteria"] = [dict(value) for value in self.connection.execute(
            "SELECT sequence_no,raw_text FROM trial_criteria WHERE trial_version_id=? "
            "AND criterion_type='inclusion' ORDER BY sequence_no", (item["current_version_id"],),
        )]
        result["exclusion_criteria"] = [dict(value) for value in self.connection.execute(
            "SELECT sequence_no,raw_text FROM trial_criteria WHERE trial_version_id=? "
            "AND criterion_type='exclusion' ORDER BY sequence_no", (item["current_version_id"],),
        )]
        if include_restricted_fields:
            result.update(self._restricted_fields(item["current_version_id"]))
            result["parties"] = [dict(value) for value in self.connection.execute(
                "SELECT role,name_raw,sequence_no FROM trial_parties WHERE trial_version_id=? "
                "ORDER BY role,sequence_no", (item["current_version_id"],),
            )]
            result["contacts"] = [dict(value) for value in self.connection.execute(
                "SELECT contact_type,sequence_no,name_raw,organization_raw,phone_raw,email_raw,"
                "address_raw,postal_code_raw FROM trial_contacts WHERE trial_version_id=? "
                "ORDER BY contact_type,sequence_no", (item["current_version_id"],),
            )]
        return result

    def _candidate_rows(self, query: LocalTrialQuery, terms: list[dict[str, Any]]) -> list[sqlite3.Row]:
        sql = [
            "SELECT t.*,v.inclusion_criteria_raw,v.exclusion_criteria_raw FROM trials t",
            "JOIN trial_versions v ON v.id=t.current_version_id",
        ]
        where = ["t.is_active=1"]
        parameters: list[Any] = []
        status_marks = ",".join("?" for _ in query.recruitment_statuses)
        where.append(f"t.recruitment_status_normalized IN ({status_marks})")
        parameters.extend(str(status) for status in query.recruitment_statuses)

        escaped_terms = [item["term"] for item in terms if item["term"]]
        if escaped_terms:
            term_clauses = []
            for term in escaped_terms:
                escaped = escape_like(term)
                term_clauses.append("(" + " OR ".join(
                    f"COALESCE({('v' if column.endswith('_criteria_raw') else 't')}.{column},'') LIKE ? ESCAPE '\\'"
                    for column in PRIMARY_DISEASE_COLUMNS
                ) + ")")
                parameters.extend(f"%{escaped}%" for _ in PRIMARY_DISEASE_COLUMNS)
            where.append("(" + " OR ".join(term_clauses) + ")")

        for sponsor in query.sponsors:
            escaped = f"%{escape_like(sponsor)}%"
            where.append(
                "(COALESCE(t.sponsor_name_raw,'') LIKE ? ESCAPE '\\' OR "
                "EXISTS(SELECT 1 FROM trial_parties p "
                "WHERE p.trial_version_id=t.current_version_id "
                "AND p.role IN ('applicant','sponsor') "
                "AND COALESCE(p.name_raw,'') LIKE ? ESCAPE '\\'))"
            )
            parameters.extend((escaped, escaped))

        for code in query.project_codes:
            escaped = f"%{escape_like(code)}%"
            where.append(
                "(COALESCE(t.public_title,'') LIKE ? ESCAPE '\\' OR "
                "COALESCE(t.professional_title,'') LIKE ? ESCAPE '\\' OR "
                "COALESCE(t.drug_name_raw,'') LIKE ? ESCAPE '\\' OR "
                "COALESCE(t.indication_raw,'') LIKE ? ESCAPE '\\' OR "
                "COALESCE(v.inclusion_criteria_raw,'') LIKE ? ESCAPE '\\' OR "
                "COALESCE(v.exclusion_criteria_raw,'') LIKE ? ESCAPE '\\')"
            )
            parameters.extend((escaped, escaped, escaped, escaped, escaped, escaped))

        if query.city and query.city.strip():
            locations = location_variants(query.city)
            marks = ",".join("?" for _ in locations)
            where.append(
                "EXISTS(SELECT 1 FROM trial_sites s WHERE s.trial_version_id=t.current_version_id "
                f"AND (s.city IN ({marks}) OR s.province IN ({marks})))"
            )
            parameters.extend(locations)
            parameters.extend(locations)
        if query.province and query.province.strip():
            provinces = location_variants(query.province)
            marks = ",".join("?" for _ in provinces)
            where.append(
                "EXISTS(SELECT 1 FROM trial_sites s WHERE s.trial_version_id=t.current_version_id "
                f"AND s.province IN ({marks}))"
            )
            parameters.extend(provinces)
        for site_name in query.site_names:
            normalized_site = normalized_organization(site_name)
            where.append(
                "EXISTS(SELECT 1 FROM trial_sites s WHERE s.trial_version_id=t.current_version_id "
                "AND REPLACE(REPLACE(REPLACE(COALESCE(s.site_name_raw,''),' ',''),'医学院',''),'　','') LIKE ?)"
            )
            parameters.append(f"%{escape_like(normalized_site)}%")
        if query.region and query.region.strip():
            provinces = REGION_PROVINCES.get(query.region.strip())
            if provinces:
                marks = ",".join("?" for _ in provinces)
                where.append(
                    "EXISTS(SELECT 1 FROM trial_sites s WHERE s.trial_version_id=t.current_version_id "
                    f"AND (s.province IN ({marks}) OR s.city IN ({marks})))"
                )
                parameters.extend(provinces)
                parameters.extend(provinces)
        if query.date_from or query.date_to:
            date_clauses = []
            for column, timestamp in (
                ("first_publication_date", False),
                ("source_update_date", False),
                ("status_changed_at", True),
            ):
                parts = []
                if query.date_from:
                    parts.append(f"t.{column}>=?")
                    parameters.append(query.date_from.isoformat())
                if query.date_to:
                    parts.append(f"t.{column}<=?")
                    parameters.append(
                        query.date_to.isoformat() + "T23:59:59" if timestamp
                        else query.date_to.isoformat()
                    )
                date_clauses.append("(" + " AND ".join(parts) + ")")
            where.append("(" + " OR ".join(date_clauses) + ")")
        sql.append("WHERE " + " AND ".join(where))
        return self.connection.execute(" ".join(sql), parameters).fetchall()

    @staticmethod
    def _disease_evidence(row: dict[str, Any], terms: list[dict[str, Any]]) -> list[dict[str, Any]]:
        field_weights = {
            "indication_raw": 100, "public_title": 90, "professional_title": 90,
            "inclusion_criteria_raw": 70, "exclusion_criteria_raw": 70, "drug_name_raw": 50,
        }
        evidence = []
        for term in terms:
            needle = term["term"].casefold()
            for field, field_score in field_weights.items():
                if needle and contains_term(str(row.get(field) or ""), term["term"]):
                    evidence.append({
                        "term": term["term"], "term_kind": term["kind"],
                        "field": field, "score": min(field_score, term["weight"]),
                    })
        return sorted(evidence, key=lambda item: (-item["score"], item["field"], item["term"]))

    @staticmethod
    def _required_biomarker_evidence(
        row: dict[str, Any], required: tuple[str, ...]
    ) -> list[dict[str, str]]:
        fields = (
            "indication_raw", "public_title", "professional_title",
            "drug_name_raw", "inclusion_criteria_raw",
        )
        evidence: list[dict[str, str]] = []
        for marker in required:
            for field in fields:
                if contains_biomarker(str(row.get(field) or ""), marker):
                    evidence.append({"term": marker, "field": field})
                    break
        return evidence

    def _required_sponsor_evidence(
        self, row: dict[str, Any], required: tuple[str, ...]
    ) -> list[dict[str, str]]:
        evidence: list[dict[str, str]] = []
        for sponsor in required:
            if contains_company(str(row.get("sponsor_name_raw") or ""), sponsor):
                evidence.append({"term": sponsor, "field": "sponsor_name_raw"})
                continue
            party = self.connection.execute(
                "SELECT name_raw FROM trial_parties WHERE trial_version_id=? "
                "AND role IN ('applicant','sponsor')",
                (row["current_version_id"],),
            ).fetchall()
            if any(contains_company(str(value[0] or ""), sponsor) for value in party):
                evidence.append({"term": sponsor, "field": "trial_parties"})
        return evidence

    @staticmethod
    def _required_project_evidence(
        row: dict[str, Any], required: tuple[str, ...]
    ) -> list[dict[str, str]]:
        fields = (
            "public_title", "professional_title", "drug_name_raw", "indication_raw",
            "inclusion_criteria_raw", "exclusion_criteria_raw",
        )
        evidence: list[dict[str, str]] = []
        for code in required:
            for field in fields:
                if contains_project_code(str(row.get(field) or ""), code):
                    evidence.append({"term": code, "field": field})
                    break
        return evidence

    @staticmethod
    def _public_result(row: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "registration_no", "detail_url", "public_title", "professional_title",
            "indication_raw", "drug_name_raw", "trial_phase_raw", "trial_scope_raw",
            "overall_status_raw", "recruitment_status_raw", "first_publication_date",
            "source_update_date", "status_changed_at", "last_verified_at",
            "sponsor_name_raw",
        )
        return {field: row.get(field) for field in fields}

    def _restricted_fields(self, version_id: int) -> dict[str, Any]:
        return {
            "sites": [dict(row) for row in self.connection.execute(
                "SELECT site_name_raw,province,city,principal_investigator FROM trial_sites "
                "WHERE trial_version_id=? ORDER BY sequence_no", (version_id,),
            )],
            "sponsor_name_raw": self.connection.execute(
                "SELECT sponsor_name_raw FROM trial_versions WHERE id=?", (version_id,),
            ).fetchone()[0],
        }

    @staticmethod
    def _sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            item["match_score"], item.get("first_publication_date") or "",
            item.get("source_update_date") or "", item.get("status_changed_at") or "",
            item["registration_no"],
        )


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def contains_term(text: str, term: str) -> bool:
    """Match ASCII abbreviations as tokens; CJK/phrases use literal substring."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .+/-]*", term) and " " not in term.strip():
        pattern = rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
        return re.search(pattern, text, flags=re.IGNORECASE) is not None
    haystack = text.casefold()
    needle = term.casefold()
    start = haystack.find(needle)
    while start >= 0:
        if term.startswith("非") or start == 0 or text[start - 1] != "非":
            return True
        start = haystack.find(needle, start + 1)
    return False


def normalized_organization(value: str) -> str:
    return re.sub(r"[\s　]", "", value or "").replace("医学院", "")


def contains_project_code(text: str, term: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
    return re.search(pattern, text or "", re.IGNORECASE) is not None


def contains_company(text: str, term: str) -> bool:
    """Match a database-resolved company alias without inventing relationships."""
    compact_text = re.sub(r"[\s()（）]", "", text or "").casefold()
    compact_term = re.sub(r"[\s()（）]", "", term or "").casefold()
    return bool(compact_term and compact_term in compact_text)


def contains_biomarker(text: str, term: str) -> bool:
    """Compare biomarker notation independent of spaces, hyphens and case."""
    exon_event = re.search(
        r"(?:(?P<gene>[A-Z][A-Z0-9-]*)\s*)?EXON\s*(?P<exon>[1-9][0-9]?)\s*"
        r"(?P<event>DEL|DELETION|INS|INSERTION)",
        term or "",
        re.IGNORECASE,
    )
    if exon_event:
        gene = exon_event.group("gene")
        event = exon_event.group("event").upper()
        event_pattern = (
            r"(?:缺失|删除|DEL(?:ETION)?)" if event.startswith("DEL")
            else r"(?:插入|INS(?:ERTION)?)"
        )
        gene_pattern = rf"{re.escape(gene)}.{{0,20}}?" if gene else ""
        return re.search(
            rf"{gene_pattern}(?:EXON\s*)?{exon_event.group('exon')}"
            rf"(?:号?外显子)?.{{0,20}}?{event_pattern}",
            text or "",
            re.IGNORECASE,
        ) is not None

    def compact(value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9]+", "", value or "").upper()
        # Users commonly omit the conventional G in KRAS12D/KRAS12C.
        return re.sub(r"(KRAS|NRAS|HRAS)G(?=12[ACDRV])", r"\1", normalized)

    raw_text = re.sub(r"[^A-Za-z0-9]+", "", text or "").upper()
    raw_term = re.sub(r"[^A-Za-z0-9]+", "", term or "").upper()
    compact_text = compact(text)
    compact_term = compact(term)
    return bool(
        raw_term
        and (raw_term in raw_text or compact_term in compact_text)
    )


def location_variants(value: str) -> tuple[str, ...]:
    """Map a user city to equivalent CDE site spellings."""
    cleaned = (value or "").strip()
    if not cleaned:
        return ()
    variants = {cleaned}
    for suffix in ("市", "省"):
        if cleaned.endswith(suffix):
            variants.add(cleaned[:-1])
    base = cleaned[:-1] if cleaned.endswith("市") else cleaned
    if base:
        variants.add(base + "市")
    return tuple(sorted(variants))


def extract_trial_phases(value: str) -> set[str]:
    """Extract auditable I-IV phases from common CDE Chinese/ASCII spellings."""
    normalized = value.upper().translate(str.maketrans({
        "Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV",
        "１": "I", "２": "II", "３": "III", "４": "IV",
    }))
    phases: set[str] = set()
    for token in re.findall(r"(?<![A-Z])(IV|III|II|I)(?=[AB]?\s*(?:期|/|\\|、|和|至|到|\+|$))", normalized):
        phases.add(token)
    for number, phase in (("1", "I"), ("2", "II"), ("3", "III"), ("4", "IV")):
        if re.search(rf"(?<!\d){number}\s*期", normalized):
            phases.add(phase)
    return phases


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
    re.compile(r"(?:年龄)?\s*(?:≥|>=|大于等于|不低于|不少于)\s*(\d{1,3})\s*岁?.{0,20}?(?:≤|<=|小于等于|不超过)\s*(\d{1,3})\s*岁"),
)


def age_compatibility(age: int | None, inclusion: str) -> str:
    if age is None:
        return "not_provided"
    text = inclusion or ""
    for pattern in _AGE_RANGE_PATTERNS:
        match = pattern.search(text)
        if match:
            lower, upper = int(match.group(1)), int(match.group(2))
            return "matched" if lower <= age <= upper else "excluded"
    minimum = re.search(r"(?:≥|>=|大于等于|不低于|不少于)\s*(\d{1,3})\s*岁", text)
    maximum = re.search(r"(?:≤|<=|小于等于|不超过)\s*(\d{1,3})\s*岁", text)
    if minimum and age < int(minimum.group(1)):
        return "excluded"
    if maximum and age > int(maximum.group(1)):
        return "excluded"
    return "matched" if minimum or maximum else "unknown"


def age_range_compatibility(
    age: int | None,
    age_min: int | None,
    age_max: int | None,
    inclusion: str,
) -> str:
    if age is not None:
        return age_compatibility(age, inclusion)
    if age_min is None and age_max is None:
        return "not_provided"
    requested_lower = age_min if age_min is not None else 0
    requested_upper = age_max if age_max is not None else 120
    text = inclusion or ""
    for pattern in _AGE_RANGE_PATTERNS:
        match = pattern.search(text)
        if match:
            trial_lower, trial_upper = int(match.group(1)), int(match.group(2))
            if requested_upper < trial_lower or requested_lower > trial_upper:
                return "excluded"
            if trial_lower <= requested_lower and requested_upper <= trial_upper:
                return "matched"
            return "unknown"
    minimum = re.search(r"(?:≥|>=|大于等于|不低于|不少于)\s*(\d{1,3})\s*岁", text)
    maximum = re.search(r"(?:≤|<=|小于等于|不超过)\s*(\d{1,3})\s*岁", text)
    if minimum and requested_upper < int(minimum.group(1)):
        return "excluded"
    if maximum and requested_lower > int(maximum.group(1)):
        return "excluded"
    if minimum and requested_lower < int(minimum.group(1)):
        return "unknown"
    if maximum and requested_upper > int(maximum.group(1)):
        return "unknown"
    return "matched" if minimum or maximum else "unknown"


REGION_PROVINCES: dict[str, tuple[str, ...]] = {
    "华东": ("上海", "上海市", "江苏", "江苏省", "浙江", "浙江省", "安徽", "安徽省", "福建", "福建省", "江西", "江西省", "山东", "山东省"),
    "华北": ("北京", "北京市", "天津", "天津市", "河北", "河北省", "山西", "山西省", "内蒙古", "内蒙古自治区"),
    "华南": ("广东", "广东省", "广西", "广西壮族自治区", "海南", "海南省"),
    "华中": ("河南", "河南省", "湖北", "湖北省", "湖南", "湖南省"),
    "西南": ("重庆", "重庆市", "四川", "四川省", "贵州", "贵州省", "云南", "云南省", "西藏", "西藏自治区"),
    "西北": ("陕西", "陕西省", "甘肃", "甘肃省", "青海", "青海省", "宁夏", "宁夏回族自治区", "新疆", "新疆维吾尔自治区"),
    "东北": ("辽宁", "辽宁省", "吉林", "吉林省", "黑龙江", "黑龙江省"),
}


def sex_compatibility(sex: str, inclusion: str) -> str:
    if sex == "unknown":
        return "not_provided"
    compact = re.sub(r"\s+", "", inclusion or "")
    explicitly_both = (
        ("男性" in compact and "女性" in compact)
        or bool(re.search(r"男女不限|性别不限", compact))
    )
    if explicitly_both:
        return "unknown"
    female_only = bool(re.search(r"仅限女性|只招募女性|仅招募女性|只允许女性|绝经后妇女", compact))
    male_only = bool(re.search(r"仅限男性|只招募男性|仅招募男性|只允许男性", compact))
    if female_only and not male_only:
        return "matched" if sex == "female" else "excluded"
    if male_only and not female_only:
        return "matched" if sex == "male" else "excluded"
    return "unknown"
