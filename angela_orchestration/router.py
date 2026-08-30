from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from .entity_resolution import LocationEntityResolver, SiteEntityResolver, SponsorEntityResolver
from .models import RequestMode, RouteDecision, RouteKind, SearchParameters
from .normalization import (
    REGION_ALIASES,
    SearchNormalizer,
    disease_phrase_from_clause,
    disease_term_occurs,
    extract_biomarker_terms,
    extract_project_codes,
    is_attribute_only_clause,
    is_biomarker_term,
)


CTR_PATTERN = re.compile(r"(?<![A-Za-z0-9])(CTR\d{6,})(?![A-Za-z0-9])", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ID_PATTERN = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

SEARCH_WORDS = ("CDE", "临床试验", "临床研究", "招募", "试验项目", "临床项目")
QUESTIONNAIRE_WORDS = ("问卷", "适合不适合", "是否适合", "入选标准", "排除标准", "入排标准")
LIVE_WORDS = ("实时核验", "官网核验", "访问官网", "当前官网", "最新状态", "现在是否还在招募")
DISEASE_SUFFIX = re.compile(
    r"(?:癌|瘤|病|症|障碍|综合征|白血病|淋巴瘤|哮喘|SLE|NSCLC|MDD)$",
    re.IGNORECASE,
)

CHINESE_NUMBERS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
    "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
}

CITIES = (
    "北京", "上海", "天津", "重庆", "广州", "深圳", "杭州", "南京", "苏州",
    "成都", "武汉", "西安", "长沙", "郑州", "济南", "青岛", "沈阳", "大连",
    "哈尔滨", "长春", "福州", "厦门", "合肥", "南昌", "昆明", "贵阳", "南宁",
    "海口", "石家庄", "太原", "呼和浩特", "兰州", "西宁", "银川", "乌鲁木齐",
)
PROVINCES = (
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾",
    "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
)


def _contains_sensitive_identity(text: str) -> bool:
    return bool(PHONE_PATTERN.search(text) or ID_PATTERN.search(text) or EMAIL_PATTERN.search(text))


def _clean_disease(value: str) -> str:
    cleaned = value.strip(" ，,。；;：:、")
    cleaned = re.sub(r"^(?:患者|病人)(?:情况|信息)\s*[:：]?\s*", "", cleaned)
    cleaned = re.sub(
        r"^(?:一个|一名|患有|患|得了|诊断为|确诊为|确诊|查询|查找|搜索|寻找|关于)",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"^(?:diagnosed\s+with|suffering\s+from|living\s+with|has)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"(?:的)?(?:患者|病人|疾病|相关)$", "", cleaned)
    cleaned = re.split(
        r"(?:目前|现在|在研|在运行中|有啥|有什么|有哪些|是否有|临床项目|临床研究|临床试验|项目|研究|试验)",
        cleaned,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return cleaned.strip(" ，,。；;：:、")[:40]


class IntentRouter:
    def __init__(self, concepts_path: str | Path, database_path: str | Path | None = None):
        self._known_terms: list[str] = []
        path = Path(concepts_path)
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            terms: set[str] = set()
            for concept in payload.get("concepts", []):
                terms.add(str(concept.get("canonical", "")))
                terms.update(str(value) for value in concept.get("synonyms", []))
                terms.update(str(value) for value in concept.get("related", []))
            self._known_terms = sorted((term for term in terms if term), key=len, reverse=True)
        self._normalizer = SearchNormalizer(path)
        self._sponsor_resolver = SponsorEntityResolver(database_path)
        self._location_resolver = LocationEntityResolver(database_path)
        self._site_resolver = SiteEntityResolver(database_path)

    def classify(self, text: str) -> RouteDecision:
        normalized = (text or "").strip()
        if not normalized:
            return RouteDecision(RouteKind.GENERAL, 1.0, "empty_or_non_text")
        if _contains_sensitive_identity(normalized):
            return RouteDecision(RouteKind.PRIVACY_BLOCK, 1.0, "direct_identifier_detected")

        ctr_match = CTR_PATTERN.search(normalized)
        ctr = ctr_match.group(1).upper() if ctr_match else None
        if ctr and any(word in normalized for word in LIVE_WORDS):
            return RouteDecision(RouteKind.CDE_LIVE_VERIFY, 0.99, "ctr_with_live_verification", ctr_number=ctr)
        if ctr and any(word in normalized for word in QUESTIONNAIRE_WORDS):
            return RouteDecision(RouteKind.CDE_QUESTIONNAIRE, 0.99, "ctr_questionnaire", ctr_number=ctr)
        if ctr:
            return RouteDecision(RouteKind.CDE_DETAIL, 0.96, "ctr_detail", ctr_number=ctr)

        biomarker_terms = extract_biomarker_terms(normalized)
        project_codes = self._extract_project_codes(normalized)
        sponsor_matches = self._sponsor_resolver.resolve(normalized)
        location_matches = self._location_resolver.resolve(normalized)
        location_matches = tuple(
            match for match in location_matches
            if not (
                re.fullmatch(r"[A-Za-z]{1,3}", match.raw_alias)
                and not re.search(
                    rf"(?:\bin|\bnear|\baround|\bfrom)\s+{re.escape(match.raw_alias)}\b",
                    normalized,
                    re.IGNORECASE,
                )
            )
        )
        location_aliases = {
            value.casefold()
            for match in location_matches
            for value in (match.raw_alias, match.canonical_name)
        }
        region_aliases = {
            value.casefold()
            for value in (*REGION_ALIASES.keys(), *REGION_ALIASES.values())
        }
        sponsor_matches = tuple(
            match for match in sponsor_matches
            if match.raw_alias.casefold() not in location_aliases
            and match.raw_alias.casefold() not in region_aliases
            and match.raw_alias not in {"一线", "二线", "三线", "初治", "初诊", "进展"}
        )
        explicit_sponsor = re.search(
            r"(?:^|[，,。；;])(?:在|由)?([A-Za-z0-9\u4e00-\u9fff]{2,16}?)(?:公司|药业|医药|生物)(?=$|[，,。；;])",
            normalized,
            re.IGNORECASE,
        )
        sponsor_terms = tuple(dict.fromkeys((
            *(match.raw_alias for match in sponsor_matches),
            *((explicit_sponsor.group(1),) if explicit_sponsor else ()),
        )))
        # Database-backed aliases can overlap with ordinary request prose (for
        # example ``clinical trials``). A site becomes an authoritative SQL
        # filter only when the user actually names an institution.
        site_matches = tuple(
            match for match in self._site_resolver.resolve(normalized)
            if (
                re.search(
                    r"医院|医学中心|研究中心|临床中心|院区|院点",
                    match.raw_alias,
                    re.IGNORECASE,
                )
                or re.search(
                    r"医院|医学中心|研究中心|临床中心|院区|院点",
                    match.canonical_name,
                    re.IGNORECASE,
                )
                or re.search(
                    rf"(?:在|由|来自|位于|选择|指定|查询|查找).{{0,8}}"
                    rf"{re.escape(match.raw_alias)}",
                    normalized,
                    re.IGNORECASE,
                )
            )
        )
        disease_text = normalized
        for match in site_matches:
            disease_text = re.sub(re.escape(match.raw_alias), "", disease_text, flags=re.IGNORECASE)
        for match in location_matches:
            disease_text = re.sub(
                rf"(?<![A-Za-z0-9]){re.escape(match.raw_alias)}(?:省|市|地区)?",
                "",
                disease_text,
                flags=re.IGNORECASE,
            )
        disease_text = re.sub(r"(?:参与的|开展的|牵头的|负责的)", "", disease_text)
        disease = self._extract_disease(disease_text)
        if disease and any(code.casefold() in disease.casefold() for code in project_codes):
            disease = None
        if project_codes:
            biomarker_terms = tuple(
                marker for marker in biomarker_terms
                if not any(marker.casefold() in code.casefold() for code in project_codes)
            )
        search_signal = any(word.casefold() in normalized.casefold() for word in SEARCH_WORDS)
        broad_search_signal = bool(
            (disease or sponsor_terms or biomarker_terms or project_codes or site_matches)
            and re.search(r"(?:项目|研究|试验|招募|trials?|studies)", normalized, re.IGNORECASE)
        )
        entity_pair_search = bool(sponsor_terms and biomarker_terms)
        structured_patient = bool(
            disease
            and (
                re.search(r"\d{1,3}\s*岁", normalized)
                or re.search(r"[一二三四五六七八九]十(?:来|多)岁", normalized)
                or re.search(r"(?:男性|女性|男|女|男女不限)", normalized)
                or re.search(r"(?<![A-Za-z])(?:AB|A|B|O)\s*型?血", normalized, re.IGNORECASE)
            )
        )
        if (disease or sponsor_terms or biomarker_terms or project_codes or site_matches) and (
            search_signal or broad_search_signal or structured_patient or entity_pair_search
        ):
            lexical_fallback = SearchParameters(
                disease=disease or "",
                raw_disease=disease,
                canonical_disease=disease,
                age=self._extract_age(normalized),
                sex=self._extract_sex(normalized),
                blood_type=self._extract_blood_type(normalized),
                lookback_months=self._extract_lookback(normalized),
                trial_phases=self._extract_phases(normalized),
                max_results=self._extract_limit(normalized),
                request_mode=RequestMode.INFORMATION_RETRIEVAL,
            )
            search = (
                self._normalizer.from_text(normalized, fallback=lexical_fallback)
                if disease else lexical_fallback
            ) or lexical_fallback
            if disease:
                normalized_disease = self._normalizer.extract_disease(disease)
                search = replace(
                    search,
                    disease=disease,
                    raw_disease=disease,
                    canonical_disease=(
                        normalized_disease.canonical if normalized_disease else disease
                    ),
                    preserve_specificity=(
                        normalized_disease.preserve_specificity
                        if normalized_disease else search.preserve_specificity
                    ),
                )
            explicit_locations = tuple(
                match for match in location_matches
                if (
                    re.search(
                        rf"(?:在|位于)?{re.escape(match.raw_alias)}(?:地区)?"
                        r"(?:有|设有|存在|的).{0,10}(?:研究中心|医院|院点|中心)",
                        normalized,
                        re.IGNORECASE,
                    )
                    or re.search(
                        rf"(?:在|位于|来自|靠近|附近|in|near|around|from)\s*"
                        rf"{re.escape(match.raw_alias)}(?:地区)?",
                        normalized,
                        re.IGNORECASE,
                    )
                )
            )
            location = (
                explicit_locations[0]
                if explicit_locations
                else next((match for match in location_matches if match.kind == "province"), None)
                or (location_matches[0] if location_matches else None)
            )
            province_mention = next(
                (
                    name for name in PROVINCES
                    if name not in {"北京", "天津", "上海", "重庆", "香港", "澳门"}
                    and re.search(rf"{re.escape(name)}(?:省|自治区)?", normalized)
                ),
                None,
            )
            candidate_city = search.city or (
                location.canonical_name.removesuffix("市")
                if location and location.kind == "city" else None
            )
            sponsor_contaminated_city = bool(
                candidate_city
                and any(str(candidate_city).casefold() in sponsor.raw_alias for sponsor in sponsor_matches)
                and not explicit_locations
            )
            biomarker_contaminated_city = bool(
                candidate_city
                and any(
                    str(candidate_city).casefold() in biomarker.casefold()
                    for biomarker in biomarker_terms
                )
                and not explicit_locations
            )
            grammar_contaminated_city = bool(
                candidate_city
                and str(candidate_city).casefold() in {"old", "year", "male", "female"}
                and not explicit_locations
            )
            region_contaminated_city = bool(
                search.region
                and location
                and (
                    location.raw_alias.casefold() == str(search.region).casefold()
                    or location.canonical_name.casefold() == str(search.region).casefold()
                )
            )
            search = replace(
                search,
                biomarkers=biomarker_terms or search.biomarkers,
                sponsors=sponsor_terms,
                site_names=tuple(dict.fromkeys(match.raw_alias for match in site_matches)),
                project_codes=project_codes,
                city=(
                    None
                    if province_mention or (
                        sponsor_contaminated_city
                        or biomarker_contaminated_city
                        or grammar_contaminated_city
                        or region_contaminated_city
                    )
                    else candidate_city
                ),
                province=(
                    (province_mention + ("省" if province_mention not in {"北京", "天津", "上海", "重庆", "香港", "澳门"} else ""))
                    if province_mention
                    else location.canonical_name
                    if location and location.kind == "province"
                    else search.province
                ),
            )
            textual_complexity = tuple(
                label for pattern, label in (
                    (r"判断.*入组|能不能入组|是否适合|最适合", "eligibility_judgment"),
                    (r"复杂既往|治疗经历复杂|多线治疗|同时有", "complex_history"),
                    (r"(?:一|二|三|四|1|2|3|4)线|初治|初次治疗|术后|治疗后|用药后|耐药|进展|转移", "treatment_or_response_constraint"),
                    (
                        r"(?:公司|申办方|企业|研发方).*(?:比较|对比|竞争|关系|格局|优劣|最好)"
                        r"|(?:比较|对比|竞争|关系|格局|优劣).*(?:公司|申办方|企业|研发方)"
                        r"|对照药|研发关系|申办关系",
                        "company_or_drug_relationship",
                    ),
                    (
                        r"(?:哪家|哪些|有哪些).{0,8}(?:医院|研究中心|院点)"
                        r"|(?:医院|研究中心|院点).*(?:最多|最好|推荐|比较|对比|几个|入组多少)"
                        r"|(?:最好|最多).*(?:哪家|那家)(?:医院|研究中心|院点)",
                        "site_analysis",
                    ),
                    (r"入排|入选标准|排除标准|问卷", "criteria_request"),
                    (r"筛败率|筛选失败率|怎么规避|如何规避", "criteria_request"),
                )
                if re.search(pattern, normalized, re.IGNORECASE)
            )
            complexity_reasons = tuple(dict.fromkeys(
                (*search.complexity_reasons(), *textual_complexity)
            ))
            medically_complex = bool(complexity_reasons)
            criteria_analysis = any(
                reason in {"eligibility_judgment", "criteria_request"}
                for reason in complexity_reasons
            )
            extracted_patient_facts = bool(
                search.disease
                and (
                    search.age is not None
                    or search.age_min is not None
                    or search.age_max is not None
                    or search.birth_year is not None
                    or search.sex not in {"", "unknown"}
                    or search.blood_type is not None
                )
            )
            patient_matching = bool(
                structured_patient
                or extracted_patient_facts
                or (
                    bool(search.complexity_reasons())
                    and bool(re.search(r"患者|病人|本人|我", normalized))
                )
                or re.search(
                    r"(?:患者|病人|本人|我).{0,20}(?:适合|匹配|推荐|能否|可以参加|入组)"
                    r"|(?:适合|匹配|推荐).{0,12}(?:我的|患者|病人)",
                    normalized,
                    re.IGNORECASE,
                )
            )
            request_mode = (
                RequestMode.CRITERIA_ANALYSIS if criteria_analysis
                else RequestMode.PATIENT_MATCHING if patient_matching
                else RequestMode.INFORMATION_RETRIEVAL
            )
            lock_sources: dict[str, str] = {}

            def lock(source: str, *field_names: str) -> None:
                for field_name in field_names:
                    lock_sources[field_name] = source

            # Only high-confidence source/database entities are immutable. Raw
            # lexical guesses remain unlocked so the mini model may repair them.
            audited_disease = bool(
                disease and any(
                    disease.casefold() == term.casefold() for term in self._known_terms
                )
            )
            if audited_disease:
                lock(
                    "audited_disease_concept",
                    "disease", "raw_disease", "canonical_disease",
                    "query_type", "preserve_specificity",
                )
            if biomarker_terms:
                lock("deterministic_biomarker", "biomarkers")
            if sponsor_terms:
                lock("database_sponsor_entity", "sponsors")
            if site_matches:
                lock("database_site_entity", "site_names")
            if project_codes:
                lock("deterministic_project_code", "project_codes")
            if search.city:
                lock("database_or_deterministic_location", "city")
            if search.province:
                lock("database_or_deterministic_location", "province")
            if search.region:
                lock("deterministic_region", "region")
            if search.age is not None:
                lock("explicit_patient_fact", "age")
            if search.age_min is not None:
                lock("explicit_patient_fact", "age_min")
            if search.age_max is not None:
                lock("explicit_patient_fact", "age_max")
            if search.birth_year is not None:
                lock("explicit_patient_fact", "birth_year")
            if search.sex not in {"", "unknown"}:
                lock("explicit_patient_fact", "sex")
            if search.blood_type:
                lock("explicit_patient_fact", "blood_type")
            if search.lookback_months is not None:
                lock("explicit_time_window", "lookback_months")
            if search.trial_phases:
                lock("explicit_trial_phase", "trial_phases")
            if re.search(
                r"(?:返回|显示|给我|需要)?\s*"
                r"(?:\d{1,2}|[一二两三四五六七八九十]{1,3})\s*(?:条|个(?!月))",
                normalized,
            ):
                lock("explicit_result_limit", "max_results")

            search = replace(
                search,
                request_mode=request_mode,
                locked_fields=tuple(lock_sources),
                field_sources=tuple(sorted(lock_sources.items())),
            )
            return RouteDecision(
                RouteKind.CDE_SEARCH,
                0.98 if search_signal or broad_search_signal else 0.92,
                (
                    f"complex_{request_mode.value}:" + ",".join(complexity_reasons)
                    if medically_complex
                    else f"{request_mode.value}_search"
                ),
                fast_eligible=not medically_complex,
                search=search,
            )
        if search_signal:
            return RouteDecision(RouteKind.CDE_SEARCH, 0.55, "cde_search_missing_disease")
        return RouteDecision(RouteKind.GENERAL, 0.90, "general_or_ambiguous")

    def _extract_disease(self, text: str) -> str | None:
        normalized_match = self._normalizer.extract_disease(text)
        if normalized_match is not None and not is_biomarker_term(normalized_match.raw):
            return normalized_match.raw

        for term in self._known_terms:
            if disease_term_occurs(text, term):
                return term

        # A standalone biomarker/score request is not a disease search. This
        # whole-message guard runs before token fallback so whitespace cannot
        # split "KRAS G12C" and promote the variant token to a disease.
        if is_attribute_only_clause(text):
            return None

        # Explicit disease anchors are stronger evidence than a finite suffix list.
        # Preserve the user's raw term even when it is not in the audited concepts
        # file, then let the read-only database provide the retrieval evidence.
        patterns = (
            r"(?:查询|查找|搜索|寻找)(?:一下)?(.{2,40}?)(?:的)?(?:临床试验|临床研究|试验项目|临床项目)",
            r"(?:患有|患(?!者)|得了|诊断为|确诊为|确诊)(.{2,40}?)(?:的患者|患者|病人|，|,|。|$)",
            r"(?:疾病|适应症)\s*[:：]\s*(.{2,40}?)(?:，|,|。|$)",
            r"diagnosed\s+with\s+(.{2,40}?)(?:，|,|。|\.|;|$)",
        )
        generic = {
            "当前", "相关", "疾病", "适应症", "这个病", "项目", "研究", "试验",
        }
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                candidate = _clean_disease(match.group(1))
                if (
                    len(candidate) >= 2
                    and candidate.casefold() not in generic
                    and re.search(r"[A-Za-z\u4e00-\u9fff]", candidate)
                ):
                    return candidate

        tokens = re.split(r"[\s，,。；;：:、]+", text)
        ignored = ("临床试验", "临床研究", "招募中", "尚未招募", "男性", "女性", "男女不限")
        cleaned_tokens: list[str] = []
        for raw in reversed(tokens):
            candidate = _clean_disease(raw)
            candidate = re.sub(r"^(?:男|女|男性|女性|男女不限)", "", candidate)
            candidate = re.sub(r"^\d{1,3}岁", "", candidate)
            candidate = re.sub(r"^[ABOab]{1,2}型血", "", candidate)
            candidate = candidate.strip(" ，,。；;：:、")
            if not candidate or any(word in candidate for word in ignored):
                continue
            disease_phrase = disease_phrase_from_clause(candidate)
            if disease_phrase:
                return disease_phrase
            if is_attribute_only_clause(candidate):
                continue
            if is_biomarker_term(candidate):
                continue
            cleaned_tokens.append(candidate)
            if DISEASE_SUFFIX.search(candidate):
                return candidate

        # A word does not need to end in 癌/病/症 to be a valid database term
        # (for example 重症肌无力 or 多发性硬化). Prefer the nearest clean clause
        # before the search cue and preserve it verbatim; ambiguous prose still falls
        # through to the mini/main-Agent route.
        lexical_noise = ("帮我", "请问", "请帮", "我想", "想找", "想看", "看看", "咨询", "有没有", "有什么", "目前", "最近")
        regions = set(REGION_ALIASES) | set(REGION_ALIASES.values())
        cities = set(CITIES)
        for candidate in cleaned_tokens:
            if candidate in cities or candidate in regions or candidate.casefold() in generic:
                continue
            if any(word in candidate for word in lexical_noise):
                continue
            if is_attribute_only_clause(candidate):
                continue
            if is_biomarker_term(candidate):
                continue
            if 2 <= len(candidate) <= 24 and re.search(r"[A-Za-z\u4e00-\u9fff]", candidate):
                return candidate
        return None

    @staticmethod
    def _extract_project_codes(text: str) -> tuple[str, ...]:
        return extract_project_codes(text)

    @staticmethod
    def _extract_age(text: str) -> int | None:
        match = re.search(r"(?<!\d)(\d{1,3})\s*岁", text)
        if not match:
            return None
        value = int(match.group(1))
        return value if 0 <= value <= 120 else None

    @staticmethod
    def _extract_sex(text: str) -> str:
        if "男女不限" in text or "性别不限" in text:
            return "unknown"
        if re.search(r"(?:男性|男患者|男，|男,)", text):
            return "male"
        if re.search(r"(?:女性|女患者|女，|女,)", text):
            return "female"
        return "unknown"

    @staticmethod
    def _extract_blood_type(text: str) -> str | None:
        match = re.search(r"(?<![A-Za-z])(AB|A|B|O)\s*型?血", text, flags=re.IGNORECASE)
        return f"{match.group(1).upper()}型血" if match else None

    @staticmethod
    def _extract_lookback(text: str) -> int | None:
        if "近半年" in text or "最近半年" in text:
            return 6
        if "近一年" in text or "最近一年" in text:
            return 12
        match = re.search(r"(?:近|最近)(\d{1,2}|[一二两三四五六七八九十]{1,3})个?月", text)
        if not match:
            return None
        raw = match.group(1)
        value = int(raw) if raw.isdigit() else CHINESE_NUMBERS.get(raw)
        return value if value and 1 <= value <= 24 else None

    @staticmethod
    def _extract_limit(text: str) -> int:
        match = re.search(
            r"(?:返回|显示|给我|需要)?\s*"
            r"(\d{1,2}|[一二两三四五六七八九十]{1,3})\s*(?:条|个(?!月))",
            text,
        )
        if not match:
            return 10
        raw = match.group(1)
        value = int(raw) if raw.isdigit() else CHINESE_NUMBERS.get(raw, 10)
        return min(20, max(1, value))

    @staticmethod
    def _extract_phases(text: str) -> tuple[str, ...]:
        normalized = text.upper().translate(str.maketrans({"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV"}))
        found = []
        for phase in ("I", "II", "III", "IV"):
            if re.search(rf"(?<![IV]){phase}\s*期", normalized):
                found.append(phase)
        return tuple(found)
