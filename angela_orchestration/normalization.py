from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import RequestMode, SearchParameters, preserve_locked_search_fields


_ASCII_DISEASE_TERM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .+/_-]*$")
_DISEASE_SUFFIX = re.compile(
    r"(?:癌|瘤|病|症|障碍|综合征|白血病|淋巴瘤|哮喘)$",
    re.IGNORECASE,
)
_ATTRIBUTE_ONLY_PATTERN = re.compile(
    r"(?:突变|融合|扩增|基因|生物标志物|阳性|阴性|"
    r"SLEDAI(?:-2K)?|BILAG|ECOG)",
    re.IGNORECASE,
)
_GENE_SYMBOL = (
    r"(?:KRAS|NRAS|HRAS|BRAF|EGFR|ERBB2|HER2|ALK|ROS1|MET|RET|"
    r"NTRK[123]?|BRCA[12]?|PD-?L1)"
)
# HGVS-like protein substitutions share one syntax: original amino-acid letter,
# residue number and replacement letter. Keep a standalone variant verbatim;
# never infer its gene (for example, G12C is not silently rewritten as KRAS G12C).
_PROTEIN_VARIANT = r"(?:[A-Z][1-9][0-9]{0,3}[A-Z*])"
# When the gene is explicitly present, tolerate compact conventions that omit
# the reference amino-acid letter (KRAS12D). This still does not infer a gene
# for a standalone 12D/G12D value.
_GENE_SUFFIX_VARIANT = r"(?:[A-Z]?[1-9][0-9]{0,3}[A-Z*])"
_EXON_EVENT = (
    r"(?:(?:EXON\s*)?[1-9][0-9]?\s*(?:号?外显子)?\s*"
    r"(?:缺失|删除|插入|DEL(?:ETION)?|INS(?:ERTION)?))"
)
_BIOMARKER_VARIANT_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_])(?:"
    rf"{_GENE_SYMBOL}(?:[\s_-]*(?:{_GENE_SUFFIX_VARIANT}|{_EXON_EVENT}|20INS|20))?"
    rf"|{_PROTEIN_VARIANT}|{_EXON_EVENT}"
    rf")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def is_biomarker_term(value: str) -> bool:
    """Return true when a standalone value is a gene/variant, not a disease."""
    cleaned = re.sub(
        r"(?:基因)?(?:突变|融合|扩增|缺失|阳性|阴性|表达|插入)$",
        "",
        (value or "").strip(" ，,。；;：:、()（）"),
        flags=re.IGNORECASE,
    ).strip()
    return _BIOMARKER_VARIANT_PATTERN.fullmatch(cleaned) is not None


def extract_biomarker_terms(text: str) -> tuple[str, ...]:
    """Extract compact, auditable biomarker tokens from arbitrary prose."""
    values: list[str] = []
    exon19_spans: list[tuple[int, int]] = []
    exon_event_pattern = re.compile(
        rf"(?<![A-Za-z0-9_])(?:(?P<gene>{_GENE_SYMBOL})[\s_-]*)?"
        r"(?:EXON\s*)?(?P<exon>[1-9][0-9]?)\s*(?:号?外显子)?\s*"
        r"(?P<event>缺失|删除|插入|DEL(?:ETION)?|INS(?:ERTION)?)"
        r"(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )
    for match in exon_event_pattern.finditer(text or ""):
        event = match.group("event").upper()
        normalized_event = "DEL" if event in {"缺失", "删除", "DEL", "DELETION"} else "INS"
        prefix = f"{match.group('gene').upper()} " if match.group("gene") else ""
        values.append(f"{prefix}EXON{match.group('exon')} {normalized_event}")
        exon19_spans.append(match.span())
    for match in _BIOMARKER_VARIANT_PATTERN.finditer(text or ""):
        if any(start <= match.start() < end for start, end in exon19_spans):
            continue
        value = re.sub(r"\s+", " ", match.group(0).upper()).strip()
        value = re.sub(r"^(PD)-L1$", r"\1-L1", value)
        suffix = re.match(
            r"(?:基因)?(突变|融合|扩增|缺失|阳性|阴性|表达|插入)",
            (text or "")[match.end():],
            flags=re.IGNORECASE,
        )
        values.append(value + (suffix.group(1) if suffix else ""))
    return tuple(dict.fromkeys(values))


def extract_project_codes(text: str) -> tuple[str, ...]:
    """Extract explicit study/program codes without treating prose as codes."""
    values = re.findall(
        r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,15}-[A-Za-z0-9][A-Za-z0-9-]{1,20})(?![A-Za-z0-9])",
        text or "",
    )
    excluded = {
        "YEAR-OLD", "NON-SMALL", "SMALL-CELL", "HER2-POSITIVE",
        "HER2-NEGATIVE", "PD-L1-POSITIVE", "PD-L1-NEGATIVE",
        "SLEDAI-2K",
    }
    accepted: list[str] = []
    for value in values:
        normalized = value.upper()
        if normalized in excluded or is_biomarker_term(value):
            continue
        segments = normalized.split("-")
        original_is_upper = value == value.upper()
        has_code_number = any(character.isdigit() for character in value)
        numeric_suffix = any(character.isdigit() for character in segments[-1])
        if original_is_upper or (has_code_number and numeric_suffix):
            accepted.append(normalized)
    return tuple(dict.fromkeys(accepted))


def disease_term_occurs(text: str, term: str) -> bool:
    """Match pure-ASCII disease terms as tokens; other terms as phrases."""
    cleaned = (term or "").strip()
    if not cleaned:
        return False
    if _ASCII_DISEASE_TERM.fullmatch(cleaned):
        return re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(cleaned)}(?![A-Za-z0-9_])",
            text,
            flags=re.IGNORECASE,
        ) is not None
    return cleaned.casefold() in text.casefold()


def disease_phrase_from_clause(value: str) -> str | None:
    """Extract a disease phrase before trailing stage/treatment attributes."""
    cleaned = (value or "").strip(" ，,。；;：:、")
    cleaned = re.sub(
        r"^(?:(?:你好|您好|嗨|请问|请帮我|帮我(?:归纳一下)?|想问一下|"
        r"请查(?:询)?|想找|关于|查询|查找|搜索|寻找)\s*)+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" ，,。；;：:、")
    cleaned = re.sub(
        r"^(?:\d{1,3}\s*岁)?(?:男性|女性|男患者|女患者|男|女)?",
        "",
        cleaned,
    ).strip(" ，,。；;：:、")
    # A compact variant is an attribute prefix, not part of the disease name:
    # ``KRAS12D的胰腺癌`` -> ``胰腺癌``.
    cleaned = re.sub(
        rf"^(?:{_BIOMARKER_VARIANT_PATTERN.pattern})(?:基因)?"
        r"(?:突变|融合|扩增|缺失|阳性|阴性|表达|插入)?的",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" ，,。；;：:、")
    cleaned = re.sub(
        r"^(?:我有个|我这里有个|有个|一名|一个)?(?:患者|病人)?"
        r"(?:是|患有|患|得了|诊断为|确诊为|确诊)?",
        "",
        cleaned,
    ).strip(" ，,。；;：:、")
    cleaned = re.sub(
        r"^(?:diagnosed\s+with|suffering\s+from|living\s+with|has)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" ，,。；;：:、")
    # Remove question grammar only when a disease-shaped phrase follows.  The
    # lookahead avoids corrupting real names that merely start with 有.
    cleaned = re.sub(
        r"^(?:有没有|是否有|有什么|有哪些|有啥|什么|哪些|有)"
        r"(?=.{0,40}?(?:癌|瘤|病|症|障碍|综合征|白血病|淋巴瘤|哮喘))",
        "",
        cleaned,
    ).strip(" ，,。；;：:、")
    subject = re.split(
        r"(?:目前|现在|在研|在运行中|有啥|有什么|有哪些|是否有|临床项目|临床研究|临床试验|项目|研究|试验)",
        cleaned,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" ，,。；;：:、")
    subject = re.sub(
        r"的?(?:[一二三四ⅠⅡⅢⅣ]|I{1,3}|IV|[1-4])\s*期$",
        "",
        subject,
        flags=re.IGNORECASE,
    ).strip(" 的")
    if len(subject) == 2 and _DISEASE_SUFFIX.search(subject):
        return subject
    english = re.search(
        r"([A-Za-z0-9+/-]+(?:\s+[A-Za-z0-9+/-]+){0,6}\s+"
        r"(?:cancer|carcinoma|lymphoma|leukemia|melanoma|sarcoma|disease|"
        r"disorder|syndrome|arthritis|asthma|diabetes\s+mellitus|diabetes))",
        subject,
        flags=re.IGNORECASE,
    )
    if english:
        return english.group(1).strip()
    matches = re.findall(
        r"([A-Za-z0-9+/\u4e00-\u9fff-]{2,40}?(?:癌|瘤|病|症|障碍|综合征|白血病|淋巴瘤|哮喘))",
        subject,
        flags=re.IGNORECASE,
    )
    if not matches:
        return None
    phrase = matches[-1].strip()
    phrase = re.sub(
        r"^(?:G?\d{1,4}[A-Z]|[VLT]\d{2,4}[A-Z])",
        "",
        phrase,
        flags=re.IGNORECASE,
    ).strip()
    qualifier = re.compile(
        r"^(?:(?:初治|初诊|新诊断|早期|中期|晚期|局部晚期|进展期|"
        r"不能手术|无法手术|可手术|术后|复发|难治|"
        r"[一二三四ⅠⅡⅢⅣIV1-4]+期)的?)+"
    )
    phrase = qualifier.sub("", phrase).strip(" 的")
    phrase = re.sub(r"^(?:临床|呼吸|消化|神经|血液|肿瘤)(?=.{2,}$)", "", phrase)
    return phrase or None


def is_attribute_only_clause(value: str) -> bool:
    """True when a clause states a biomarker/score but no disease name."""
    cleaned = (value or "").strip()
    return bool(
        cleaned
        and (
            _ATTRIBUTE_ONLY_PATTERN.search(cleaned)
            or _BIOMARKER_VARIANT_PATTERN.search(cleaned)
        )
        and not _DISEASE_SUFFIX.search(cleaned)
        and disease_phrase_from_clause(cleaned) is None
    )


CITY_ALIASES = {
    "魔都": "上海", "beijing": "北京", "shanghai": "上海",
    "guangzhou": "广州", "shenzhen": "深圳", "hangzhou": "杭州",
    "nanjing": "南京", "chengdu": "成都", "wuhan": "武汉",
}
CHINESE_CITIES = (
    "北京", "上海", "天津", "重庆", "广州", "深圳", "杭州", "南京", "苏州",
    "成都", "武汉", "西安", "长沙", "郑州", "济南", "青岛", "沈阳", "大连",
    "哈尔滨", "长春", "福州", "厦门", "合肥", "南昌", "昆明", "贵阳", "南宁",
    "海口", "石家庄", "太原", "呼和浩特", "兰州", "西宁", "银川", "乌鲁木齐",
)
REGION_ALIASES = {
    "华东地区": "华东", "华东": "华东", "华北地区": "华北", "华北": "华北",
    "华南地区": "华南", "华南": "华南", "华中地区": "华中", "华中": "华中",
    "西南地区": "西南", "西南": "西南", "西北地区": "西北", "西北": "西北",
    "东北地区": "东北", "东北": "东北",
}


def normalize_city(value: str | None) -> str | None:
    """Use one suffix-free city representation at the orchestration boundary."""
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    aliased = CITY_ALIASES.get(cleaned.casefold(), cleaned)
    return aliased[:-1] if aliased.endswith("市") else aliased


def strip_city_prefix(value: str, city: str | None) -> str:
    """Remove an already-resolved city from the start of a disease phrase."""
    cleaned = (value or "").strip()
    if not cleaned or not city:
        return cleaned
    aliases = [city]
    aliases.extend(
        alias for alias, canonical in CITY_ALIASES.items()
        if normalize_city(canonical) == city and not alias.isascii()
    )
    prefix = "|".join(re.escape(alias) for alias in sorted(set(aliases), key=len, reverse=True))
    return re.sub(
        rf"^(?:{prefix})(?:市|地区)?\s*(?:有没有|是否有|的|有)?\s*",
        "",
        cleaned,
        count=1,
        flags=re.IGNORECASE,
    ).strip()

_FACT_SPLIT_PATTERN = re.compile(r"[，,。；;：:\n]+")
_FACT_PATTERNS = {
    "prior_treatments": re.compile(
        r"既往|曾经|使用|用过|服用|治疗|化疗|放疗|手术|靶向|免疫|内分泌|"
        r"抗抑郁药|激素|免疫抑制剂|疗效不佳",
        re.IGNORECASE,
    ),
    "biomarkers": re.compile(
        r"突变|融合|扩增|基因|生物标志物|HER2|EGFR|KRAS|ALK|ROS1|BRAF|"
        r"PD-?L1|BRCA|阳性|阴性",
        re.IGNORECASE,
    ),
    "comorbidities": re.compile(
        r"伴有|伴随|合并|并发|无其他|高血压|糖尿病|冠心病|肾炎|肝炎|"
        r"自身免疫|感染",
        re.IGNORECASE,
    ),
    "disease_statuses": re.compile(
        r"初诊|初次诊断|新诊断|进展|复发|难治|缓解|稳定|控制中|恶化|耐药",
        re.IGNORECASE,
    ),
    "organ_function_findings": re.compile(
        r"肝功能|肾功能|心功能|肺功能|器官功能|血常规|肌酐|转氨酶|胆红素|"
        r"血小板|中性粒细胞|血红蛋白",
        re.IGNORECASE,
    ),
    "reproductive_statuses": re.compile(
        r"妊娠|怀孕|哺乳|备孕|绝经|生育",
        re.IGNORECASE,
    ),
    "allergies": re.compile(r"过敏|超敏反应", re.IGNORECASE),
}


@dataclass(frozen=True)
class DiseaseMatch:
    raw: str
    canonical: str
    query_type: str = "disease"
    preserve_specificity: bool = False


class SearchNormalizer:
    """Auditable extraction that stabilizes the mini model's structured output."""

    def __init__(self, concepts_path: str | Path):
        self._terms: list[tuple[str, str, bool]] = []
        self._audited_terms: dict[str, DiseaseMatch] = {}
        try:
            payload = json.loads(Path(concepts_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {"concepts": []}
        for concept in payload.get("concepts", []):
            if not isinstance(concept, dict):
                continue
            canonical = str(concept.get("canonical") or "").strip()
            if not canonical:
                continue
            specific = bool(concept.get("parent") or concept.get("preserve_specificity"))
            # Only canonical names and true synonyms may normalize a user's disease.
            # Related parent/child concepts are retrieval expansions, not aliases; treating
            # them as aliases silently downgraded 肺腺癌/NSCLC/MDD to broader diseases.
            for value in [canonical, *(concept.get("synonyms") or [])]:
                term = str(value or "").strip()
                if term:
                    self._terms.append((term, canonical, specific))
                    self._audited_terms[term.casefold()] = DiseaseMatch(
                        term,
                        canonical,
                        preserve_specificity=specific,
                    )
        self._terms.sort(key=lambda item: len(item[0]), reverse=True)

    def from_text(
        self,
        text: str,
        *,
        payload: dict[str, Any] | None = None,
        fallback: SearchParameters | None = None,
    ) -> SearchParameters | None:
        payload = payload or {}
        disease = self.extract_disease(text)
        payload_raw = _string(payload.get("raw_disease") or payload.get("disease"))
        if payload_raw and is_biomarker_term(payload_raw):
            payload_raw = None
        query_type = _enum(payload.get("query_type"), {"disease", "drug", "unknown"}, "disease")
        if disease is None and payload_raw:
            audited = self._audited_terms.get(payload_raw.casefold())
            if audited is not None:
                disease = DiseaseMatch(
                    payload_raw,
                    audited.canonical,
                    query_type=query_type,
                    preserve_specificity=audited.preserve_specificity,
                )
            else:
                # The model-proposed canonical name is advisory only. Unknown
                # mappings are not silently promoted into retrieval terms.
                disease = DiseaseMatch(
                    payload_raw,
                    payload_raw,
                    query_type=query_type,
                    preserve_specificity=bool(payload.get("preserve_specificity")),
                )
        if (
            disease is None
            and fallback is not None
            and fallback.disease
            and not is_biomarker_term(fallback.disease)
        ):
            disease = DiseaseMatch(
                fallback.raw_disease or fallback.disease,
                fallback.canonical_disease or fallback.disease,
                fallback.query_type,
                fallback.preserve_specificity,
            )
        if disease is None:
            return None

        age, age_min, age_max = self.extract_age(text)
        payload_age = _age(payload.get("age"))
        payload_age_min = _age(payload.get("age_min"))
        payload_age_max = _age(payload.get("age_max"))
        age = age if age is not None else payload_age
        age_min = age_min if age_min is not None else payload_age_min
        age_max = age_max if age_max is not None else payload_age_max
        if age is not None:
            age_min = age_max = None

        sex = self.extract_sex(text)
        if sex == "unknown":
            sex = _enum(payload.get("sex"), {"male", "female", "unknown"}, "unknown")
        city = self.extract_city(text) or _string(payload.get("city"))
        city = normalize_city(city)
        disease_raw = strip_city_prefix(disease.raw, city)
        disease_canonical = strip_city_prefix(disease.canonical, city)
        if disease_raw and disease_raw != disease.raw:
            audited = self._audited_terms.get(disease_raw.casefold())
            disease = audited or DiseaseMatch(
                disease_raw,
                disease_canonical or disease_raw,
                query_type=disease.query_type,
                preserve_specificity=disease.preserve_specificity,
            )
        region = self.extract_region(text) or _string(payload.get("region"))
        region = REGION_ALIASES.get(region or "", region)
        months = self.extract_lookback(text)
        if months is None:
            months = _bounded_int(payload.get("lookback_months"), 1, 60)
        phases = self.extract_phases(text)
        if not phases:
            phases = _phases(payload.get("trial_phases"))
        limit = self.extract_limit(text)
        if limit is None:
            limit = fallback.max_results if fallback else 10

        birth_year = self.extract_birth_year(text)
        if birth_year is None:
            birth_year = fallback.birth_year if fallback else None
        duration_months = self.extract_disease_duration_months(text)
        if duration_months is None:
            duration_months = _bounded_int(payload.get("disease_duration_months"), 0, 1440)

        source_facts = self.extract_patient_fact_fragments(text)
        source_facts["biomarkers"] = self.filter_disease_only_biomarkers(
            extract_biomarker_terms(text),
            disease,
        )
        source_facts["comorbidities"] = self.filter_disease_only_facts(
            source_facts["comorbidities"], disease, "comorbidities"
        )
        fallback_prior_treatments = fallback.prior_treatments if fallback else ()
        fallback_biomarkers = fallback.biomarkers if fallback else ()
        fallback_scores = fallback.scores if fallback else ()
        fallback_comorbidities = fallback.comorbidities if fallback else ()
        fallback_disease_statuses = fallback.disease_statuses if fallback else ()
        fallback_organ_findings = fallback.organ_function_findings if fallback else ()
        fallback_reproductive_statuses = fallback.reproductive_statuses if fallback else ()
        fallback_allergies = fallback.allergies if fallback else ()

        normalized = SearchParameters(
            disease=disease.raw[:80],
            raw_disease=disease.raw[:80],
            canonical_disease=disease.canonical[:80],
            query_type=disease.query_type,
            preserve_specificity=disease.preserve_specificity,
            age=age if age is not None else (fallback.age if fallback else None),
            age_min=age_min,
            age_max=age_max,
            sex=sex if sex != "unknown" else (fallback.sex if fallback else "unknown"),
            city=city or (fallback.city if fallback else None),
            province=fallback.province if fallback else None,
            region=region or (fallback.region if fallback else None),
            blood_type=self.extract_blood_type(text) or (fallback.blood_type if fallback else None),
            lookback_months=months if months is not None else (fallback.lookback_months if fallback else None),
            trial_phases=phases or (fallback.trial_phases if fallback else ()),
            max_results=limit,
            birth_year=birth_year,
            disease_duration_months=(
                duration_months if duration_months is not None
                else (fallback.disease_duration_months if fallback else None)
            ),
            disease_stage=(
                _string(payload.get("disease_stage"))
                or self.extract_disease_stage(text)
                or (fallback.disease_stage if fallback else None)
            ),
            treatment_line=(
                _string(payload.get("treatment_line"))
                or self.extract_treatment_line(text)
                or (fallback.treatment_line if fallback else None)
            ),
            prior_treatments=_merge_string_tuples(
                _string_tuple(payload.get("prior_treatments")),
                source_facts["prior_treatments"],
                fallback_prior_treatments,
            ),
            biomarkers=_merge_string_tuples(
                _string_tuple(payload.get("biomarkers")),
                source_facts["biomarkers"],
                fallback_biomarkers,
            ),
            sponsors=fallback.sponsors if fallback else (),
            site_names=fallback.site_names if fallback else (),
            project_codes=fallback.project_codes if fallback else (),
            scores=_merge_string_tuples(
                self.extract_scores(text),
                _string_tuple(payload.get("scores")),
                fallback_scores,
            ),
            comorbidities=_merge_string_tuples(
                _string_tuple(payload.get("comorbidities")),
                source_facts["comorbidities"],
                fallback_comorbidities,
            ),
            disease_statuses=_merge_string_tuples(
                _string_tuple(payload.get("disease_statuses")),
                source_facts["disease_statuses"],
                fallback_disease_statuses,
            ),
            organ_function_findings=_merge_string_tuples(
                _string_tuple(payload.get("organ_function_findings")),
                source_facts["organ_function_findings"],
                fallback_organ_findings,
            ),
            reproductive_statuses=_merge_string_tuples(
                _string_tuple(payload.get("reproductive_statuses")),
                source_facts["reproductive_statuses"],
                fallback_reproductive_statuses,
            ),
            allergies=_merge_string_tuples(
                _string_tuple(payload.get("allergies")),
                source_facts["allergies"],
                fallback_allergies,
            ),
            precise_eligibility=bool(payload.get("precise_eligibility")) or (
                fallback.precise_eligibility if fallback else False
            ),
            request_mode=(
                fallback.request_mode if fallback
                else RequestMode.PATIENT_MATCHING
            ),
        )
        return preserve_locked_search_fields(normalized, fallback)

    def extract_disease(self, text: str) -> DiseaseMatch | None:
        # Resolve clauses in source order so a later comorbidity cannot replace
        # an earlier primary diagnosis (for example ``T2DM，合并慢性肾病``).
        # Within each clause, preserve a complete specific phrase before a
        # shorter audited alias embedded inside it (小细胞肺癌 vs 肺癌).
        for clause in _FACT_SPLIT_PATTERN.split(text or ""):
            # In "2型糖尿病合并慢性肾病", the left side is the primary
            # diagnosis and the right side is a comorbidity. Resolve the primary
            # segment first; only fall back to the whole clause when it contains
            # no usable disease at all.
            primary = re.split(r"(?:合并|伴有|伴随|并发)", clause, maxsplit=1)[0].strip()
            candidates = tuple(dict.fromkeys(value for value in (primary, clause) if value))
            for candidate_clause in candidates:
                phrase = disease_phrase_from_clause(candidate_clause)
                if phrase and not is_biomarker_term(phrase):
                    audited = self._audited_terms.get(phrase.casefold())
                    if audited is not None:
                        return audited
                    return DiseaseMatch(
                        phrase,
                        phrase,
                        preserve_specificity=bool(re.search(r"小细胞|腺癌|鳞癌|肉瘤|淋巴瘤|骨髓瘤", phrase)),
                    )
                for term, canonical, specific in self._terms:
                    if disease_term_occurs(candidate_clause, term):
                        return DiseaseMatch(term, canonical, preserve_specificity=specific)
        for fragment in re.split(r"(?:项目|研究|试验|入组)", text or ""):
            phrase = disease_phrase_from_clause(fragment)
            if phrase and not is_biomarker_term(phrase):
                return DiseaseMatch(phrase, phrase)
        # Mutation notation such as KRAS G12C or BRAF V600E must never be
        # promoted to a drug/disease code. The router may still preserve an
        # independently extracted disease (for example 肺癌) as fallback.
        if is_attribute_only_clause(text) or _BIOMARKER_VARIANT_PATTERN.search(text):
            return None
        code = re.search(
            r"(?<![A-Za-z0-9])([A-Z][A-Za-z]*[A-Z0-9-]*\d[A-Za-z0-9-]*|[A-Z][a-z]{4,}mab)(?![A-Za-z0-9])",
            text,
        )
        if code and not is_biomarker_term(code.group(1)):
            return DiseaseMatch(code.group(1), code.group(1), query_type="drug")
        return None

    @staticmethod
    def extract_age(text: str) -> tuple[int | None, int | None, int | None]:
        fuzzy = re.search(r"([一二三四五六七八九])十(?:来|多)岁", text)
        if fuzzy:
            lower = "一二三四五六七八九".index(fuzzy.group(1)) * 10 + 10
            return None, lower, lower + 9
        patterns = (
            r"(?<!\d)(\d{1,3})\s*岁",
            r"(?<!\d)(\d{1,3})\s*[- ]?year[- ]?old",
            r"\b(?:male|female)\s*,\s*(\d{1,3})\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match and 0 <= int(match.group(1)) <= 120:
                return int(match.group(1)), None, None
        return None, None, None

    @staticmethod
    def extract_birth_year(text: str) -> int | None:
        patterns = (
            r"(?<!\d)((?:19|20)\d{2})\s*年\s*(?:出生|生人|生的)",
            r"(?:出生于|生于)\s*((?:19|20)\d{2})(?:\s*年)?",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return int(match.group(1))
        short = re.search(r"(?<!\d)(\d{2})\s*年\s*(?:出生|生人|生的)", text)
        if short:
            return 2000 + int(short.group(1))
        return None

    @staticmethod
    def extract_disease_duration_months(text: str) -> int | None:
        number = r"[一二两三四五六七八九十\d]{1,3}"
        years = re.search(rf"(?:病史|患病|确诊|诊断)(?:时长|时间)?\s*({number})\s*年", text)
        if years:
            return _chinese_number(years.group(1)) * 12
        suffix_years = re.search(rf"(?<!\d)({number})\s*年(?:的)?病史", text)
        if suffix_years:
            return _chinese_number(suffix_years.group(1)) * 12
        disease_years = re.search(rf"(?:SLE|系统性红斑狼疮|患者|患病)\s*({number})\s*年", text, re.IGNORECASE)
        if disease_years:
            return _chinese_number(disease_years.group(1)) * 12
        months = re.search(rf"(?:病史|患病|确诊|诊断)(?:时长|时间)?\s*({number})\s*个?月", text)
        return _chinese_number(months.group(1)) if months else None

    @staticmethod
    def extract_scores(text: str) -> tuple[str, ...]:
        pattern = re.compile(
            r"(?:SLEDAI(?:-2K)?|BILAG|ECOG).{0,10}?"
            r"(?:≥|<=|≤|>=|>|<|大于等于|小于等于|大于|小于|不低于|不高于)?\s*"
            r"\d+(?:\.\d+)?\s*分?",
            re.IGNORECASE,
        )
        return tuple(dict.fromkeys(match.group(0).strip() for match in pattern.finditer(text)))

    @staticmethod
    def extract_disease_stage(text: str) -> str | None:
        pattern = re.compile(
            r"局部晚期|晚期|早期|转移性|(?:[一二三四ⅠⅡⅢⅣ]|I{1,3}|IV|[1-4])\s*期",
            re.IGNORECASE,
        )
        for match in pattern.finditer(text):
            # “II期临床研究” describes the trial phase, not the patient's
            # disease stage. Keep explicit patient wording such as “肺癌II期”.
            trailing = text[match.end():match.end() + 12]
            if re.match(r"\s*(?:临床)?(?:研究|试验|项目)", trailing):
                continue
            # A standalone phase followed by a retrieval phrase, for example
            # “申办方…，II期，有哪些项目”, is also a trial phase.  Keep the
            # punctuation requirement here so “肺癌II期，查询临床试验” remains
            # an explicit patient disease stage.
            leading = text[max(0, match.start() - 1):match.start()]
            if (
                (not leading or re.match(r"[\s,，、;；:：的]", leading))
                and re.match(
                    r"\s*[,，、;；:：]\s*(?:有(?:哪些|什么)?|查(?:询|找)?|找)"
                    r".{0,4}(?:研究|试验|项目)",
                    trailing,
                )
            ):
                continue
            return match.group(0).strip()
        return None

    @staticmethod
    def filter_disease_only_biomarkers(
        values: tuple[str, ...],
        disease: DiseaseMatch,
    ) -> tuple[str, ...]:
        """Drop a biomarker fact when its only evidence is the disease name."""
        disease_terms = tuple(dict.fromkeys(
            term for term in (disease.raw, disease.canonical) if term
        ))
        filtered: list[str] = []
        for value in values:
            residual = value
            for term in sorted(disease_terms, key=len, reverse=True):
                marker_base = re.sub(
                    r"(?:基因)?(?:突变|融合|扩增|缺失|阳性|阴性|表达|插入)$",
                    "",
                    value,
                    flags=re.IGNORECASE,
                )
                if marker_base.casefold() in term.casefold():
                    residual = ""
                    break
                residual = re.sub(re.escape(term), "", residual, flags=re.IGNORECASE)
            if residual and (
                _FACT_PATTERNS["biomarkers"].search(residual)
                or is_biomarker_term(residual)
            ):
                filtered.append(value)
        return tuple(filtered)

    @staticmethod
    def filter_disease_only_facts(
        values: tuple[str, ...], disease: DiseaseMatch, category: str
    ) -> tuple[str, ...]:
        """A disease mention alone must not become a second patient fact."""
        pattern = _FACT_PATTERNS[category]
        disease_terms = tuple(dict.fromkeys(
            term for term in (disease.raw, disease.canonical) if term
        ))
        filtered: list[str] = []
        for value in values:
            residual = value
            for term in sorted(disease_terms, key=len, reverse=True):
                residual = re.sub(re.escape(term), "", residual, flags=re.IGNORECASE)
            if pattern.search(residual):
                filtered.append(value)
        return tuple(filtered)

    @staticmethod
    def extract_treatment_line(text: str) -> str | None:
        match = re.search(r"(?:一|二|三|四|多|[1-4])\s*线(?:治疗)?", text)
        return match.group(0).strip() if match else None

    @staticmethod
    def extract_patient_fact_fragments(text: str) -> dict[str, tuple[str, ...]]:
        """Retain only source clauses with explicit medical-fact anchors."""
        clauses = [
            clause.strip(" \t、")[:160]
            for clause in _FACT_SPLIT_PATTERN.split(text or "")
            if clause.strip(" \t、")
        ]
        return {
            category: tuple(
                dict.fromkeys(clause for clause in clauses if pattern.search(clause))
            )
            for category, pattern in _FACT_PATTERNS.items()
        }

    @staticmethod
    def extract_sex(text: str) -> str:
        folded = text.casefold()
        if re.search(r"男女不限|性别不限", text):
            return "unknown"
        if re.search(r"\b(?:female|woman|girl)\b", folded) or re.search(r"女性|女患者|女孩", text):
            return "female"
        if re.search(r"\b(?:male|man|boy)\b", folded) or re.search(r"男性|男患者|男孩", text):
            return "male"
        if re.search(r"(?:^|[，,、\s])女(?:[，,、\s]|$)", text):
            return "female"
        if re.search(r"(?:^|[，,、\s])男(?:[，,、\s]|$)", text):
            return "male"
        return "unknown"

    @staticmethod
    def extract_city(text: str) -> str | None:
        folded = text.casefold()
        for alias, city in CITY_ALIASES.items():
            if alias in folded:
                return city
        return next((city for city in CHINESE_CITIES if city in text), None)

    @staticmethod
    def extract_region(text: str) -> str | None:
        return next((region for alias, region in REGION_ALIASES.items() if alias in text), None)

    @staticmethod
    def extract_lookback(text: str) -> int | None:
        folded = text.casefold()
        if re.search(r"(?:近|最近)?半[年载]", text):
            return 6
        if re.search(r"(?:近|最近)?一[年载]", text):
            return 12
        if "仨月" in text:
            return 3
        english = re.search(r"(?:last|past)\s+(\d{1,2})\s+months?", folded)
        if english:
            return _bounded_int(english.group(1), 1, 60)
        match = re.search(r"(?:近|最近|过去)?\s*(\d{1,2}|[一二两三四五六七八九十]{1,3})\s*个?月", text)
        if not match:
            return None
        return _chinese_number(match.group(1))

    @staticmethod
    def extract_limit(text: str) -> int | None:
        match = re.search(
            r"(?:返回|显示|给我|需要|查)?\s*"
            r"(\d{1,2}|[一二两三四五六七八九十]{1,3})\s*(?:条|个(?!月))",
            text,
        )
        return min(20, max(1, _chinese_number(match.group(1)))) if match else None

    @staticmethod
    def extract_phases(text: str) -> tuple[str, ...]:
        normalized = text.upper().translate(str.maketrans({"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV"}))
        return tuple(phase for phase in ("I", "II", "III", "IV") if re.search(rf"(?<![IV]){phase}\s*期", normalized))

    @staticmethod
    def extract_blood_type(text: str) -> str | None:
        match = re.search(r"(?<![A-Za-z])(AB|A|B|O)\s*型?血", text, flags=re.IGNORECASE)
        return f"{match.group(1).upper()}型血" if match else None


def _string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _enum(value: Any, allowed: set[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def _bounded_int(value: Any, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if minimum <= parsed <= maximum else None


def _age(value: Any) -> int | None:
    return _bounded_int(value, 0, 120)


def _phases(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if item in {"I", "II", "III", "IV"})


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    cleaned = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return tuple(dict.fromkeys(cleaned))


def _merge_string_tuples(*groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    for group in groups:
        for value in group:
            cleaned = value.strip()
            key = cleaned.casefold()
            if not cleaned:
                continue
            existing_keys = [item.casefold() for item in merged]
            if key in existing_keys or any(existing in key for existing in existing_keys):
                # Prefer the richer source phrase when it contains a shorter model phrase.
                merged = [item for item in merged if item.casefold() not in key]
                merged.append(cleaned)
                continue
            if any(key in existing for existing in existing_keys):
                continue
            merged.append(cleaned)
    return tuple(merged)


def _chinese_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return digits.get(value, 0)
