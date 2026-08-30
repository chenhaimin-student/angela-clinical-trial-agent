from __future__ import annotations

from typing import Any

from .models import FastPathResponse, RequestMode, SearchParameters


FIELD_LABELS = {
    "indication_raw": "适应症",
    "public_title": "标题",
    "professional_title": "标题",
    "drug_name_raw": "药物",
    "inclusion_criteria_raw": "入选标准",
    "exclusion_criteria_raw": "排除标准",
    "sponsor_name_raw": "申办方",
    "trial_parties": "申办/申请主体",
}

GUARD_CATEGORY_LABELS = {
    "participant_type": "研究对象类型",
    "age": "年龄",
    "sex": "性别",
    "disease": "疾病或适应症",
    "disease_stage": "疾病分期或状态",
    "biomarker": "基因或生物标志物",
    "score": "评分或量表",
    "prior_treatment": "既往治疗",
    "treatment_line": "治疗线数",
    "disease_duration": "病程",
    "disease_status": "复发、进展或难治状态",
    "comorbidity": "合并疾病",
    "organ_function": "器官功能或化验",
    "reproductive": "妊娠、生育或避孕条件",
    "allergy": "过敏史",
}


def _patient_condition_trace(search: SearchParameters) -> tuple[str, tuple[str, ...]]:
    """Build a deterministic, user-visible trace of supplied screening facts."""
    labels: list[str] = []
    required_terms: list[str] = []

    def add(label: str, values: tuple[str, ...] | list[str]) -> None:
        cleaned = tuple(str(value).strip() for value in values if str(value).strip())
        if not cleaned:
            return
        labels.append(f"{label}：{'、'.join(cleaned)}")
        required_terms.extend(cleaned)

    if search.birth_year is not None:
        add("出生年份", [f"{search.birth_year}年"])
    if search.age is not None:
        add("年龄", [f"{search.age}岁"])
    elif search.age_min is not None or search.age_max is not None:
        lower = str(search.age_min) if search.age_min is not None else "未注明"
        upper = str(search.age_max) if search.age_max is not None else "未注明"
        add("年龄范围", [f"{lower}–{upper}岁"])
    if search.sex in {"male", "female"}:
        add("性别", ["男性" if search.sex == "male" else "女性"])
    add("疾病", [search.raw_disease or search.disease] if (search.raw_disease or search.disease) else [])
    add("公司/申办方", list(search.sponsors))
    if search.disease_duration_months is not None:
        add("病程", [f"{search.disease_duration_months}个月"])
    add("疾病分期", [search.disease_stage] if search.disease_stage else [])
    add("治疗线数", [search.treatment_line] if search.treatment_line else [])
    add("既往治疗", list(search.prior_treatments))
    add("生物标志物", list(search.biomarkers))
    add("评分", list(search.scores))
    add("合并疾病", list(search.comorbidities))
    add("疾病状态", list(search.disease_statuses))
    add("器官功能", list(search.organ_function_findings))
    add("生育相关", list(search.reproductive_statuses))
    add("过敏史", list(search.allergies))
    add("城市", [search.city] if search.city else [])
    add("地区", [search.region] if search.region else [])
    if search.lookback_months is not None:
        add("时间范围", [f"近{search.lookback_months}个月"])
    if search.blood_type:
        add("血型（暂不参与匹配）", [search.blood_type])

    prefix = (
        "已纳入本次查询条件："
        if search.request_mode == RequestMode.INFORMATION_RETRIEVAL
        else "已纳入本次分析的患者条件："
    )
    text = prefix + "；".join(labels) + "。"
    return text, tuple(dict.fromkeys(required_terms))


def _short_evidence(items: list[dict[str, Any]]) -> str:
    labels: list[str] = []
    for item in items:
        term = str(item.get("term") or "").strip()
        field = FIELD_LABELS.get(str(item.get("field") or ""), "公开登记文本")
        label = f"{field}命中“{term}”" if term else field
        if label not in labels:
            labels.append(label)
        if len(labels) == 3:
            break
    return "；".join(labels) or "疾病相关公开文本匹配"


def _retrieval_metadata(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "data_source": result.get("data_source", "local_sqlite"),
        "retrieval_policy": "lexical_evidence_only_no_vector",
        "query_terms": tuple(
            {
                "term": str(item.get("term") or ""),
                "kind": str(item.get("kind") or ""),
            }
            for item in (result.get("query") or {}).get("expanded_terms") or ()
            if str(item.get("term") or "").strip()
        ),
    }


def _constraint_review_lines(item: dict[str, Any]) -> list[str]:
    review = item.get("constraint_review") or {}
    findings = list(review.get("findings") or [])
    passed = []
    unknown = []
    for finding in findings:
        field = str(finding.get("field") or "")
        label = GUARD_CATEGORY_LABELS.get(field, field)
        value = str(finding.get("value") or "").strip()
        state = str(finding.get("state") or "")
        evidence = str(finding.get("evidence") or "").strip()
        if state == "PASS":
            passed.append(f"{label}“{value}”有公开原文证据" + (f"（{evidence}）" if evidence else ""))
        elif state == "UNKNOWN":
            unknown.append(label)
    lines = []
    if passed:
        lines.append(f"   条件证据：{'；'.join(passed)}")
    if unknown:
        lines.append(f"   待核对：{'、'.join(dict.fromkeys(unknown))}")
    return lines


def render_search(result: dict[str, Any], search: SearchParameters) -> FastPathResponse:
    total = int(result.get("total_count") or 0)
    records = list(result.get("results") or [])
    returned = min(len(records), int(result.get("returned_count") or len(records)))
    if not records:
        subject = search.disease or "、".join((*search.sponsors, *search.biomarkers)) or "当前条件"
        text = (
            f"本地CDE数据库中暂未找到符合“{subject}”及当前筛选条件的招募中或尚未招募试验。\n\n"
            "这不代表不存在合适项目；可以补充病理分型、既往治疗、城市或放宽时间范围后继续查询。"
        )
        return FastPathResponse(
            text=text,
            total_count=0,
            returned_count=0,
            metadata=_retrieval_metadata(result),
        )

    evaluated = int(result.get("guard_evaluated_count") or 0)
    information_retrieval = search.request_mode == RequestMode.INFORMATION_RETRIEVAL
    evidence_filter_active = bool(result.get("evidence_filter_active"))
    condition_trace, required_condition_terms = _patient_condition_trace(search)
    summary = (
        f"本地CDE数据库匹配 {total} 条，以下展示 {returned} 条："
        if information_retrieval else
        f"本地CDE数据库初步匹配 {total} 条，以下展示资格守卫核验后保留的 {returned} 条："
    )
    lines = [condition_trace, summary]
    if evidence_filter_active:
        lines.append(
            "已按公开标题、适应症和入选标准核对治疗阶段等条件；"
            "没有公开原文证据的条件保留为待核对。"
        )
    elif evaluated:
        lines.append(f"已对相关度靠前的 {evaluated} 条公开入排标准执行条件级核验。")
    ids: list[str] = []
    for index, item in enumerate(records[:returned], 1):
        ctr = str(item.get("registration_no") or "未知编号")
        ids.append(ctr)
        title = item.get("public_title") or item.get("professional_title") or "未公开标题"
        status = item.get("recruitment_status_raw") or item.get("overall_status_raw") or "状态待核对"
        lines.extend(
            [
                "",
                f"{index}. {ctr}",
                f"   标题：{title}",
                f"   适应症：{item.get('indication_raw') or '未公开'}",
                f"   药物：{item.get('drug_name_raw') or '未公开'}",
                f"   分期：{item.get('trial_phase_raw') or '未注明'}",
                f"   状态：{status}",
                f"   首次公示：{item.get('first_publication_date') or '未注明'}",
                f"   匹配依据：{_short_evidence(item.get('match_evidence') or [])}",
                f"   链接：{item.get('detail_url') or '未提供'}",
            ]
        )
        if search.sponsors:
            lines.append(f"   申办方：{item.get('sponsor_name_raw') or '公开登记中未注明'}")
        if evidence_filter_active:
            lines.extend(_constraint_review_lines(item))
        elif not information_retrieval:
            pending = item.get("needs_confirmation") or []
            if pending:
                labels = [GUARD_CATEGORY_LABELS.get(str(value), str(value)) for value in pending]
                lines.append(f"   待核对：{'、'.join(dict.fromkeys(labels))}")
            guard = item.get("eligibility_guard") or {}
            disposition = guard.get("disposition")
            if disposition == "POTENTIAL_MATCH":
                lines.append("   条件级结论：未发现已公开条件中的明确冲突")
            elif disposition == "NEEDS_CONFIRMATION":
                lines.append("   条件级结论：存在信息不足或需要研究中心确认的条件")
    if search.blood_type:
        lines.extend(["", f"备注：已接收{search.blood_type}，但当前版本不将血型用于CDE匹配。"])
    if search.scores:
        lines.extend(
            [
                "",
                f"备注：已接收评分条件“{'、'.join(search.scores)}”；只有公开原文能够核实时才判定，"
                "否则保持待核对，不视为已经符合。",
            ]
        )
    lines.extend(["", (
        "以上结果来自本地保存的CDE公开登记数据；如需确认当前官网状态，可继续要求实时核验。"
        if information_retrieval else
        "以上是基于CDE公开登记数据的初步筛选，不构成诊断、治疗建议或最终入组结论；最终以研究医生和研究中心核对为准。"
    )])
    return FastPathResponse(
        text="\n".join(lines),
        total_count=total,
        returned_count=returned,
        result_ids=tuple(ids),
        metadata={
            **_retrieval_metadata(result),
            "guard_summary": result.get("guard_summary") or {},
            "guard_evaluated_count": evaluated,
            "guard_scope": result.get("guard_scope"),
            "evidence_filter_active": evidence_filter_active,
            "evidence_filter_fields": tuple(result.get("evidence_filter_fields") or ()),
            "patient_facts_schema": result.get("patient_facts_schema"),
            "condition_trace_text": condition_trace,
            "required_condition_terms": required_condition_terms,
        },
    )
