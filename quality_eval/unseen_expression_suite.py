from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from angela_orchestration.models import RouteKind
from angela_orchestration.router import IntentRouter


@dataclass(frozen=True)
class ExpressionCase:
    case_id: str
    category: str
    kind: str
    prompt: str
    search: dict[str, Any]
    required_terms: tuple[str, ...] = ()


AUDITED_DISEASES = (
    ("sle", "系统性红斑狼疮", ("SLE", "systemic lupus erythematosus", "系统性红斑狼疮")),
    ("lung", "肺癌", ("肺癌", "肺恶性肿瘤", "肺部恶性肿瘤")),
    ("nsclc", "非小细胞肺癌", ("非小细胞肺癌", "non-small cell lung cancer")),
    ("luad", "肺腺癌", ("肺腺癌", "lung adenocarcinoma")),
    ("depression", "抑郁症", ("抑郁症", "抑郁障碍")),
    ("mdd", "重性抑郁障碍", ("重性抑郁障碍", "major depressive disorder")),
    ("bipolar", "双相情感障碍", ("双相情感障碍", "bipolar disorder", "躁郁症")),
    ("asthma", "哮喘", ("哮喘", "asthma")),
    ("ra", "类风湿关节炎", ("类风湿关节炎", "rheumatoid arthritis", "RA")),
    ("copd", "慢性阻塞性肺疾病", ("慢阻肺", "COPD", "chronic obstructive pulmonary disease")),
    ("t2dm", "2型糖尿病", ("2型糖尿病", "T2DM", "type 2 diabetes mellitus")),
    ("her2_breast", "HER2阳性乳腺癌", ("HER2阳性乳腺癌", "HER2-positive breast cancer")),
)


BASIC_TEMPLATES = (
    ("请查{disease}目前有哪些临床试验，男患者，47岁。", {"age": 47, "sex": "male"}),
    ("{disease}，女性，32岁，想看当前招募研究。", {"age": 32, "sex": "female"}),
    ("患有{disease}，五十来岁，男性，有项目吗？", {"age_min": 50, "age_max": 59, "sex": "male"}),
    ("2014年出生的女孩，诊断为{disease}，请找临床研究。", {"birth_year": 2014, "sex": "female"}),
    ("{disease} patient, male, 61-year-old, show 4 clinical trials.", {"age": 61, "sex": "male"}),
    ("女性，29岁，{disease}，魔都，最近半年，给我3条临床试验。", {"age": 29, "sex": "female", "city": "上海", "lookback_months": 6, "max_results": 3}),
    ("男，44岁，{disease}，北京，近三个月，返回6条项目。", {"age": 44, "sex": "male", "city": "北京", "lookback_months": 3, "max_results": 6}),
    ("性别不限，65岁，{disease}，华东地区，查招募项目。", {"age": 65, "sex": "unknown", "region": "华东"}),
    ("{disease}，女患者，38岁，广州，最近12个月，显示2条研究。", {"age": 38, "sex": "female", "city": "广州", "lookback_months": 12, "max_results": 2}),
    ("患者确诊{disease}，男，52岁，A型血，查询临床试验。", {"age": 52, "sex": "male", "blood_type": "A型血"}),
    ("想找{disease}的II期临床研究，女性，41岁，给我5条。", {"age": 41, "sex": "female", "trial_phases": ("II",), "max_results": 5}),
    ("{disease}，男性，70岁，西南地区，近一年有哪些招募中的试验？", {"age": 70, "sex": "male", "region": "西南", "lookback_months": 12}),
    ("female, 36-year-old, {disease}, studies in Shanghai, return 7 trials.", {"age": 36, "sex": "female", "city": "上海"}),
    ("四十来岁男性，{disease}，想看临床项目。", {"age_min": 40, "age_max": 49, "sex": "male"}),
    ("出生于2008年，男性，{disease}，请给我临床研究线索。", {"birth_year": 2008, "sex": "male"}),
)


UNAUDITED_DISEASES = (
    "胃癌", "结直肠癌", "肝细胞癌", "胰腺癌", "前列腺癌", "卵巢癌", "宫颈癌", "食管癌", "鼻咽癌", "甲状腺癌",
    "胶质瘤", "黑色素瘤", "白血病", "淋巴瘤", "肾癌", "膀胱癌", "骨肉瘤", "胆管癌", "帕金森病", "阿尔茨海默病",
    "癫痫", "银屑病", "特应性皮炎", "克罗恩病", "溃疡性结肠炎", "强直性脊柱炎", "多发性硬化", "重症肌无力", "甲状腺功能亢进症", "甲状腺功能减退症",
    "高血压", "冠心病", "心力衰竭", "慢性肾病", "乙型肝炎", "丙型肝炎", "艾滋病", "偏头痛", "骨质疏松症", "子宫内膜异位症",
)


COMPLEX_SPECS = (
    ("sle_score", "SLE", "系统性红斑狼疮", 46, "female", "SLEDAI-2K大于10分", ("SLEDAI",)),
    ("luad_egfr", "肺腺癌", "肺腺癌", 57, "male", "EGFR 19外显子缺失，奥希替尼后进展", ("EGFR", "奥希替尼")),
    ("nsclc_kras", "非小细胞肺癌", "非小细胞肺癌", 62, "male", "KRAS G12C突变，含铂化疗后进展", ("KRAS G12C", "含铂化疗")),
    ("her2_treated", "HER2阳性乳腺癌", "HER2阳性乳腺癌", 43, "female", "既往曲妥珠单抗治疗，肝功能异常", ("HER2", "曲妥珠单抗", "肝功能")),
    ("mdd_treated", "重性抑郁障碍", "重性抑郁障碍", 34, "female", "两种抗抑郁药效果不佳", ("抗抑郁药",)),
    ("myeloma_lines", "多发性骨髓瘤", "多发性骨髓瘤", 69, "male", "经过三线治疗仍复发", ("三线治疗",)),
    ("copd_htn", "COPD", "慢性阻塞性肺疾病", 66, "male", "合并控制中的高血压", ("高血压",)),
    ("ra_mtx", "类风湿关节炎", "类风湿关节炎", 51, "female", "甲氨蝶呤治疗效果不理想", ("甲氨蝶呤",)),
    ("t2dm_renal", "T2DM", "2型糖尿病", 59, "female", "合并慢性肾病，肌酐偏高", ("慢性肾病", "肌酐")),
    ("bipolar_lithium", "双相情感障碍", "双相情感障碍", 39, "female", "既往使用锂盐，近期病情恶化", ("锂盐",)),
)

SPECIFIC_CANONICAL_DISEASES = {
    "非小细胞肺癌",
    "肺腺癌",
    "重性抑郁障碍",
    "HER2阳性乳腺癌",
    "胰腺癌",
    "前列腺癌",
    "甲状腺癌",
    "淋巴瘤",
    "骨肉瘤",
    "多发性骨髓瘤",
}


def _base_search(raw: str, canonical: str, expected: dict[str, Any]) -> dict[str, Any]:
    return {
        "disease": raw,
        "raw_disease": raw,
        "canonical_disease": canonical,
        "preserve_specificity": canonical in SPECIFIC_CANONICAL_DISEASES,
        "max_results": 10,
        **expected,
    }


def build_cases() -> tuple[ExpressionCase, ...]:
    cases: list[ExpressionCase] = []
    for disease_key, canonical, terms in AUDITED_DISEASES:
        for index, (template, expected) in enumerate(BASIC_TEMPLATES, 1):
            raw = terms[(index - 1) % len(terms)]
            cases.append(ExpressionCase(
                case_id=f"audited_{disease_key}_{index:02d}",
                category="audited_multilingual",
                kind="fast",
                prompt=template.format(disease=raw),
                search=_base_search(raw, canonical, expected),
            ))

    cities = ("杭州", "南京", "成都", "武汉")
    for index, disease in enumerate(UNAUDITED_DISEASES):
        age = 22 + index % 55
        mode = index % 4
        if mode == 0:
            city = cities[index % len(cities)]
            prompt = f"请查询{disease}的临床试验，男性，{age}岁，{city}，给我5条。"
            expected = {"age": age, "sex": "male", "city": city, "max_results": 5}
        elif mode == 1:
            prompt = f"女性，{age}岁，患有{disease}，华南地区，最近半年有哪些临床研究？"
            expected = {"age": age, "sex": "female", "region": "华南", "lookback_months": 6}
        elif mode == 2:
            year = 2009 + index % 7
            prompt = f"{year}年出生的男孩，诊断为{disease}，请找当前招募项目。"
            expected = {"birth_year": year, "sex": "male"}
        else:
            prompt = f"female, {age}-year-old, diagnosed with {disease}, show 4 clinical trials in shanghai."
            expected = {"age": age, "sex": "female", "city": "上海"}
        cases.append(ExpressionCase(
            case_id=f"unaudited_{index + 1:02d}",
            category="unaudited_raw_disease",
            kind="fast",
            prompt=prompt,
            search=_base_search(disease, disease, expected),
        ))

    for key, raw, canonical, age, sex, detail, terms in COMPLEX_SPECS:
        sex_text = "男性" if sex == "male" else "女性"
        prompts = (
            f"{sex_text}，{age}岁，{raw}，{detail}，请判断是否可能入组并列出5条公开研究。",
            f"患者情况：{raw}，{sex_text}，{age}岁，{detail}。请按入排条件分析是否适合，返回5条。",
        )
        for variant, prompt in enumerate(prompts, 1):
            cases.append(ExpressionCase(
                case_id=f"complex_{key}_{variant}",
                category="complex_conditions",
                kind="complex",
                prompt=prompt,
                search=_base_search(raw, canonical, {"age": age, "sex": sex, "max_results": 5}),
                required_terms=terms,
            ))
    return tuple(cases)


def evaluate_contract(project_root: Path) -> dict[str, Any]:
    cases = build_cases()
    router = IntentRouter(project_root / "cde-data/config/disease_concepts.json")
    failures: list[dict[str, Any]] = []
    route_pass = 0
    field_pass = 0
    for case in cases:
        decision = router.classify(case.prompt)
        route_ok = decision.kind == RouteKind.CDE_SEARCH and decision.search is not None
        route_pass += int(route_ok)
        mismatch: dict[str, Any] = {}
        if route_ok:
            actual = asdict(decision.search)
            for field, expected in case.search.items():
                observed = actual.get(field)
                if isinstance(expected, tuple):
                    observed = tuple(observed or ())
                if observed != expected:
                    mismatch[field] = {"expected": expected, "actual": observed}
        else:
            mismatch["route"] = {"expected": "cde_search", "actual": decision.kind.value}
        fast_ok = decision.fast_eligible == (case.kind == "fast")
        if not fast_ok:
            mismatch["fast_eligible"] = {
                "expected": case.kind == "fast",
                "actual": decision.fast_eligible,
            }
        field_pass += int(not mismatch)
        if mismatch:
            failures.append({
                "case_id": case.case_id,
                "category": case.category,
                "prompt": case.prompt,
                "mismatch": mismatch,
            })
    return {
        "schema_version": 1,
        "case_count": len(cases),
        "unique_prompt_count": len({case.prompt for case in cases}),
        "route_pass_count": route_pass,
        "field_pass_count": field_pass,
        "route_pass_rate": route_pass / len(cases),
        "field_pass_rate": field_pass / len(cases),
        "failure_count": len(failures),
        "failures": failures,
    }
