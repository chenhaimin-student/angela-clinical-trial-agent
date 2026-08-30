from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from angela_orchestration.eligibility import EligibilityGuard, TrialDisposition
from angela_orchestration.models import SearchParameters
from angela_orchestration.normalization import extract_project_codes
from angela_orchestration.patient_facts import PatientFactsBuilder


CTR_RE = re.compile(r"(?<![A-Za-z0-9_])CTR\d{6,}(?![A-Za-z0-9_])", re.I)
RECRUITABLE = {"recruiting", "not_yet_recruiting"}
NO_RESULT_RE = re.compile(r"暂未找到|未找到|没有找到|未检索到|(?<!\d)0\s*[条项个]")
UNKNOWN_RE = re.compile(
    r"不(?:能|足以)|无法|需要.{0,12}(?:核对|确认|补充)|"
    r"请(?:先)?(?:补充|确认)|未知|暂未"
)
OPERATIONAL_ERROR_RE = re.compile(
    r"API call failed|HTTP\s+(?:401|403|429|5\d\d)|auth_unavailable|"
    r"authentication token is expired|Traceback|Internal Server Error",
    re.I,
)


class Verdict(str, Enum):
    PASS_FAST = "PASS_FAST"
    PASS_COMPLEX = "PASS_COMPLEX"
    UNKNOWN_EVIDENCE = "UNKNOWN_EVIDENCE"
    FAIL = "FAIL"


class Mode(str, Enum):
    INFORMATION_RETRIEVAL = "information_retrieval"
    PATIENT_MATCHING = "patient_matching"
    CRITERIA_ANALYSIS = "criteria_analysis"


DISEASE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"局限期小细胞肺癌", ("局限期小细胞肺癌", "小细胞肺癌", "SCLC")),
    (r"肺腺癌", ("肺腺癌", "非小细胞肺癌", "NSCLC")),
    (r"非小细胞肺癌|\bNSCLC\b", ("非小细胞肺癌", "NSCLC")),
    (r"肺癌", ("肺癌", "非小细胞肺癌", "小细胞肺癌", "NSCLC", "SCLC")),
    (r"慢淋|小淋|CLL|SLL", ("慢性淋巴细胞白血病", "小淋巴细胞淋巴瘤", "CLL", "SLL")),
    (r"边缘区淋巴瘤", ("边缘区淋巴瘤",)),
    (r"套细胞淋巴瘤|\bMCL\b", ("套细胞淋巴瘤", "MCL")),
    (r"多发性骨髓瘤|骨髓瘤", ("多发性骨髓瘤", "骨髓瘤")),
    (r"系统性红斑狼疮|红斑狼疮|\bSLE\b", ("系统性红斑狼疮", "红斑狼疮", "狼疮", "SLE")),
    (r"重性抑郁|抑郁症|major depressive disorder|\bMDD\b", ("重性抑郁障碍", "抑郁症", "MDD")),
    (r"转移性去势抵抗性前列腺癌|\bmCRPC\b", ("转移性去势抵抗性前列腺癌", "mCRPC")),
    (r"高级别圆细胞肉瘤", ("高级别圆细胞肉瘤",)),
    (r"子宫内膜癌", ("子宫内膜癌",)),
    (r"乳腺癌", ("乳腺癌",)),
    (r"胰腺癌", ("胰腺癌",)),
    (r"甲状腺髓样癌", ("甲状腺髓样癌",)),
    (r"肝细胞癌|肝癌", ("肝细胞癌", "肝癌")),
    (r"食管鳞癌|食管麟癌|食管癌", ("食管鳞癌", "食管癌")),
    (r"结直肠癌|大肠癌|肠癌", ("结直肠癌", "结肠癌", "直肠癌", "大肠癌")),
    (r"胃癌", ("胃癌",)),
    (r"2型糖尿病", ("2型糖尿病",)),
    (r"帕金森", ("帕金森",)),
    (r"哮喘", ("哮喘",)),
    (r"慢性阻塞性肺|(?<![A-Za-z0-9])COPD(?![A-Za-z0-9])", ("慢性阻塞性肺", "COPD")),
    (r"慢性炎性脱髓鞘|\bCIDP\b", ("慢性炎性脱髓鞘", "CIDP")),
    (r"精神分裂|精分", ("精神分裂",)),
)

MARKER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("KRAS G12C", r"(?<![A-Za-z0-9])KRAS\s*G?12C(?![A-Za-z0-9])|(?<![A-Za-z0-9])G12C(?![A-Za-z0-9])"),
    ("KRAS G12D", r"(?<![A-Za-z0-9])KRAS\s*G?12D(?![A-Za-z0-9])|(?<![A-Za-z0-9])G-?12D(?![A-Za-z0-9])"),
    ("EGFR L858R", r"(?<![A-Za-z0-9])EGFR\s*L858R(?![A-Za-z0-9])|(?<![A-Za-z0-9])L858R(?![A-Za-z0-9])"),
    ("ERBB/HER2 exon20 insertion", r"(?:ERBB|HER2|EGFR).{0,12}(?:20|exon\s*20).{0,12}(?:插入|ins)"),
    ("BRAF", r"\bBRAF\b"),
    ("HRAS", r"\bHRAS\b"),
    ("PD-L1", r"\bPD\s*-?\s*L1\b"),
    ("HER2", r"\bHER\s*2\b|HER2"),
    ("EGFR", r"(?<![A-Za-z0-9])EGFR(?![A-Za-z0-9-])"),
)

ENTITY_TERMS = (
    "南岭示例药业有限公司", "星河", "云舟", "青岚", "澄明", "松岳医药",
)
LOCATION_TERMS = ("上海", "福州", "浙江", "天津", "广东", "贵阳", "湖南", "川渝")
BROAD_TERMS = ("风免", "风湿", "眼科疾病", "肿瘤领域")
SPECIFIC_SITES = ("上海交通大学附属瑞金医院", "孟超肝胆病医院")


@dataclass(frozen=True)
class IndependentContract:
    mode: Mode
    disease_terms: tuple[str, ...] = ()
    biomarkers: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    site: str | None = None
    location: str | None = None
    project_codes: tuple[str, ...] = ()
    broad_query: bool = False


@dataclass(frozen=True)
class ReviewResult:
    verdict: Verdict
    expected_mode: str
    actual_mode: str
    execution_route: str
    returned_ctrs: tuple[str, ...]
    issues: tuple[str, ...]
    warnings: tuple[str, ...]
    database_evidence: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["verdict"] = self.verdict.value
        return payload


def _compact(value: str) -> str:
    return re.sub(r"[\s()（）\-_/]+", "", value or "").casefold()


def _site_compact(value: str) -> str:
    return _compact(value).replace("医学院", "")


def _affirmative_ctrs(answer: str) -> tuple[str, ...]:
    """Return trial identifiers presented as evidence, excluding explicit rejection examples."""
    accepted: list[str] = []
    for match in CTR_RE.finditer(answer or ""):
        start = max(0, match.start() - 80)
        end = min(len(answer), match.end() + 120)
        after = answer[match.end():end]
        before = answer[start:match.start()]
        explicitly_rejected = bool(
            re.search(
                r"(?:并非|不是|不属于).{0,24}(?:匹配|候选|该标志物|该项目)|"
                r"(?:不应|不能|不可).{0,16}(?:作为|列为|视为).{0,12}(?:候选|匹配)|"
                r"(?:应当|需要|已被?).{0,8}(?:排除|剔除)",
                after,
            )
            or re.search(
                r"(?:排除|剔除|错误引用|否定引用|非候选).{0,24}$",
                before,
            )
        )
        if not explicitly_rejected:
            accepted.append(match.group(0).upper())
    return tuple(dict.fromkeys(accepted))


def _mode(question: str) -> Mode:
    if re.search(r"入排|入选标准|排除标准|问卷|筛败率.*规避", question, re.I):
        return Mode.CRITERIA_ANALYSIS
    if re.search(
        r"患者|病人|老太太|老汉|男生|女生|男孩|女孩|女性|男性|"
        r"\d{1,3}\s*岁|(?:19|20)\d{2}\s*年\s*出生|"
        r"可以参加|能参加|适合|推荐|有项目可以筛|有什么项目可以入组",
        question,
        re.I,
    ):
        return Mode.PATIENT_MATCHING
    return Mode.INFORMATION_RETRIEVAL


def independent_contract(question: str) -> IndependentContract:
    diseases: list[str] = []
    for pattern, terms in DISEASE_PATTERNS:
        if re.search(pattern, question, re.I):
            diseases.extend(terms)
    markers = [name for name, pattern in MARKER_PATTERNS if re.search(pattern, question, re.I)]
    if re.search(r"PD\s*-?\s*L1\s*[（(]?\s*[-−]\s*[)）]?|PD\s*-?\s*L1.{0,8}阴性", question, re.I):
        markers = [value for value in markers if value != "PD-L1"]
    entities = [term for term in ENTITY_TERMS if term.casefold() in question.casefold()]
    locations = [term for term in LOCATION_TERMS if term in question]
    site = next((value for value in SPECIFIC_SITES if value in question), None)
    codes = extract_project_codes(question)
    return IndependentContract(
        mode=_mode(question),
        disease_terms=tuple(dict.fromkeys(diseases)),
        biomarkers=tuple(markers),
        entities=tuple(entities),
        site=site,
        location=locations[0] if locations else None,
        project_codes=codes,
        broad_query=any(term in question for term in BROAD_TERMS),
    )


def _marker_match(text: str, marker: str) -> bool:
    if marker == "EGFR":
        # Renal-function text conventionally uses the mixed-case abbreviation
        # eGFR.  Treat only the oncogenic all-upper-case token as EGFR evidence.
        return re.search(r"(?<![A-Za-z0-9])EGFR(?![A-Za-z0-9-])", text or "") is not None
    pattern = dict(MARKER_PATTERNS)[marker]
    return re.search(pattern, text or "", re.I) is not None


def _disease_match(text: str, terms: tuple[str, ...]) -> bool:
    if not terms:
        return True
    normalized = text.casefold()
    if terms[0] == "局限期小细胞肺癌":
        return bool(re.search(r"局限期.{0,8}(?<!非)小细胞肺癌|(?<!非)小细胞肺癌.{0,8}局限期", text, re.I))
    return any(term.casefold() in normalized for term in terms)


class CurrentContractValidator:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser().resolve()

    def validate(self, record: dict[str, Any]) -> ReviewResult:
        question = str(record.get("question") or "")
        answer = str(record.get("answer") or "")
        stages = record.get("stage_timings") or {}
        route = str(stages.get("execution_route") or "waiting_user")
        actual_search = record.get("current_search") or {}
        actual_mode = str(actual_search.get("request_mode") or "missing")
        contract = independent_contract(question)
        returned = _affirmative_ctrs(answer)
        requested_ctrs = tuple(dict.fromkeys(x.upper() for x in CTR_RE.findall(question)))
        evidence_ctrs = returned
        if (
            not evidence_ctrs
            and requested_ctrs
            and contract.mode == Mode.CRITERIA_ANALYSIS
            and answer.strip()
            and not OPERATIONAL_ERROR_RE.search(answer)
        ):
            # A questionnaire turn may answer one question at a time without
            # repeating the CTR already supplied by the user.
            evidence_ctrs = requested_ctrs
        issues: list[str] = []
        warnings: list[str] = []
        if OPERATIONAL_ERROR_RE.search(answer):
            issues.append("operational_error_exposed_as_answer")
        if actual_mode != contract.mode.value:
            warnings.append(f"mode_mismatch:{actual_mode}->{contract.mode.value}")
        if contract.mode == Mode.CRITERIA_ANALYSIS and route == "local_cde_query":
            issues.append("criteria_analysis_routed_to_list_only")

        rows = self._rows(evidence_ctrs)
        evidence: list[dict[str, Any]] = []
        for ctr in evidence_ctrs:
            row = rows.get(ctr)
            if row is None:
                issues.append(f"missing_ctr:{ctr}")
                continue
            if row["recruitment_status_normalized"] not in RECRUITABLE:
                issues.append(f"non_recruitable:{ctr}")
            primary = "\n".join(str(row.get(k) or "") for k in (
                "indication_raw", "public_title", "professional_title", "drug_name_raw"
            ))
            public = primary + "\n" + str(row.get("inclusion_criteria_raw") or "")
            sites = str(row.get("sites") or "")
            if contract.disease_terms and not _disease_match(primary, contract.disease_terms):
                issues.append(f"disease_mismatch:{ctr}")
            for marker in contract.biomarkers:
                if not _marker_match(public, marker):
                    ambiguous_erbb = bool(
                        marker == "ERBB/HER2 exon20 insertion"
                        and re.search(r"ERBB\s*20", question, re.I)
                        and not re.search(r"ERBB\s*2|HER\s*2", question, re.I)
                        and UNKNOWN_RE.search(answer)
                    )
                    if ambiguous_erbb:
                        warnings.append(f"ambiguous_erbb_gene_requires_confirmation:{ctr}")
                    else:
                        issues.append(f"biomarker_mismatch:{ctr}:{marker}")
            for entity in contract.entities:
                if entity in {"澄明", "松岳医药"}:
                    warnings.append(f"internal_entity_not_a_cde_sponsor:{entity}")
                elif _compact(entity) not in _compact(str(row.get("sponsor_name_raw") or "") + public):
                    issues.append(f"entity_mismatch:{ctr}:{entity}")
            if contract.site and _site_compact(contract.site) not in _site_compact(sites):
                issues.append(f"site_mismatch:{ctr}:{contract.site}")
            if contract.location and contract.location not in {"川渝"} and contract.location not in sites:
                issues.append(f"location_mismatch:{ctr}:{contract.location}")
            for code in contract.project_codes:
                if _compact(code) not in _compact(public + str(row.get("sponsor_name_raw") or "")):
                    issues.append(f"project_code_mismatch:{ctr}:{code}")
            age_match = re.search(r"(?<!\d)(\d{1,3})\s*岁", question)
            birth_year_match = re.search(
                r"(?<!\d)((?:19|20)\d{2})\s*年\s*(?:出生|生人|生的)",
                question,
            )
            sex = "female" if re.search(r"女性|女患者|老太太|女生|女孩", question) else (
                "male" if re.search(r"男性|男患者|老汉|男生|男孩", question) else "unknown"
            )
            if age_match or birth_year_match or sex != "unknown":
                search = SearchParameters(
                    disease=contract.disease_terms[0] if contract.disease_terms else "",
                    age=int(age_match.group(1)) if age_match else None,
                    birth_year=int(birth_year_match.group(1)) if birth_year_match else None,
                    sex=sex,
                )
                facts = PatientFactsBuilder().build(question, search)
                evaluation = EligibilityGuard().evaluate(facts, row)
                if evaluation.disposition == TrialDisposition.EXPLICIT_CONFLICT:
                    failed_categories = {
                        finding.category for finding in evaluation.findings
                        if str(finding.state.value) == "FAIL"
                    }
                    inclusion = str(row.get("inclusion_criteria_raw") or "")
                    both_sexes_have_requirements = bool(
                        re.search(r"女性(?:受试者)?|有生育能力的女性", inclusion)
                        and re.search(r"男性受试者", inclusion)
                    )
                    if failed_categories == {"sex"} and both_sexes_have_requirements:
                        warnings.append(f"sex_guard_reproductive_wording_false_positive:{ctr}")
                    else:
                        issues.append(f"explicit_patient_conflict:{ctr}")
            evidence.append({
                "registration_no": ctr,
                "status": row["recruitment_status_normalized"],
                "indication": row.get("indication_raw"),
                "title": row.get("public_title"),
                "sponsor": row.get("sponsor_name_raw"),
                "sites": row.get("sites"),
            })

        if not returned and not issues:
            if route == "waiting_user":
                if UNKNOWN_RE.search(answer):
                    warnings.append("waiting_for_conflict_or_required_detail_confirmation")
                elif contract.mode == Mode.PATIENT_MATCHING and not contract.disease_terms:
                    warnings.append("waiting_for_required_disease_detail")
                elif contract.disease_terms or contract.biomarkers or contract.entities or contract.project_codes:
                    issues.append("supported_constraints_sent_to_missing_disease_prompt")
                else:
                    warnings.append("waiting_for_required_detail")
            elif contract.broad_query:
                warnings.append("broad_query_needs_clarification")
            elif evidence_ctrs and contract.mode == Mode.CRITERIA_ANALYSIS:
                # Evidence was resolved from the CTR in the user's question;
                # the answer does not need to repeat it on every questionnaire turn.
                pass
            elif contract.mode == Mode.INFORMATION_RETRIEVAL and contract.disease_terms:
                if re.search(r"一线|二线|三线|初治|经治|耐药|进展|术前|术后|用过", question):
                    warnings.append("treatment_line_or_history_needs_criteria_evidence")
                else:
                    count = self._broad_candidate_count(contract)
                    if count and NO_RESULT_RE.search(answer):
                        issues.append(f"false_empty_information_retrieval:candidates={count}")
            elif contract.mode != Mode.INFORMATION_RETRIEVAL and (NO_RESULT_RE.search(answer) or UNKNOWN_RE.search(answer)):
                warnings.append("complex_conditions_not_fully_auditable")
            elif not NO_RESULT_RE.search(answer) and not UNKNOWN_RE.search(answer):
                issues.append("answer_has_no_database_result_or_evidence_boundary")

        issues = list(dict.fromkeys(issues))
        warnings = list(dict.fromkeys(warnings))
        if issues:
            verdict = Verdict.FAIL
        elif not evidence_ctrs and (
            warnings or contract.mode != Mode.INFORMATION_RETRIEVAL
        ):
            verdict = Verdict.UNKNOWN_EVIDENCE
        elif route == "local_cde_query":
            verdict = Verdict.PASS_FAST
        else:
            verdict = Verdict.PASS_COMPLEX
        return ReviewResult(
            verdict, contract.mode.value, actual_mode, route, returned,
            tuple(issues), tuple(warnings), tuple(evidence),
        )

    def _rows(self, ctrs: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        if not ctrs:
            return {}
        connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        marks = ",".join("?" for _ in ctrs)
        data = connection.execute(
            "SELECT t.registration_no,t.recruitment_status_normalized,t.indication_raw,"
            "t.public_title,t.professional_title,t.drug_name_raw,t.sponsor_name_raw,"
            "v.inclusion_criteria_raw,v.exclusion_criteria_raw,"
            "GROUP_CONCAT(s.site_name_raw||' '||COALESCE(s.province,'')||' '||COALESCE(s.city,''),' | ') sites "
            "FROM trials t JOIN trial_versions v ON v.id=t.current_version_id "
            "LEFT JOIN trial_sites s ON s.trial_version_id=t.current_version_id "
            f"WHERE t.registration_no IN ({marks}) GROUP BY t.registration_no",
            ctrs,
        ).fetchall()
        connection.close()
        return {str(row["registration_no"]).upper(): dict(row) for row in data}

    def _broad_candidate_count(self, contract: IndependentContract) -> int:
        connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
        sql = (
            "SELECT t.indication_raw,t.public_title,t.professional_title,t.drug_name_raw,"
            "t.sponsor_name_raw,v.inclusion_criteria_raw,"
            "GROUP_CONCAT(s.site_name_raw||' '||COALESCE(s.province,'')||' '||COALESCE(s.city,''),' | ') sites "
            "FROM trials t JOIN trial_versions v ON v.id=t.current_version_id "
            "LEFT JOIN trial_sites s ON s.trial_version_id=t.current_version_id "
            "WHERE t.recruitment_status_normalized IN ('recruiting','not_yet_recruiting') "
            "GROUP BY t.registration_no"
        )
        rows = connection.execute(sql).fetchall()
        connection.close()
        count = 0
        for row in rows:
            primary = "\n".join(str(v or "") for v in row[:4])
            public = primary + "\n" + str(row[5] or "")
            sponsor = str(row[4] or "")
            sites = str(row[6] or "")
            if contract.disease_terms and not _disease_match(primary, contract.disease_terms):
                continue
            if any(not _marker_match(public, marker) for marker in contract.biomarkers):
                continue
            external_entities = [value for value in contract.entities if value not in {"澄明", "松岳医药"}]
            if any(_compact(value) not in _compact(sponsor + public) for value in external_entities):
                continue
            if contract.site and _site_compact(contract.site) not in _site_compact(sites):
                continue
            if contract.location:
                if contract.location == "川渝":
                    if not any(value in sites for value in ("四川", "重庆", "成都")):
                        continue
                elif contract.location not in sites:
                    continue
            if any(_compact(code) not in _compact(public + sponsor) for code in contract.project_codes):
                continue
            count += 1
        return count
