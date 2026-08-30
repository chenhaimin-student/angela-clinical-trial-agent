from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def write_reports(report: dict[str, Any], output_dir: str | Path) -> tuple[Path, Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "quality-report.json"
    markdown_path = root / "quality-report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def write_annotation_csv(bundle: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "suite_id", "database_fingerprint", "case_id", "prompt", "review_status",
        "reviewer", "reviewed_at", "registration_no", "title", "indication", "phase",
        "status", "first_publication_date", "detail_url", "guard_disposition",
        "inclusion_excerpt", "exclusion_excerpt", "relevant", "expected_disposition",
        "expected_findings_json", "review_notes",
    )
    fingerprint = str(bundle["database_snapshot"]["logical_fingerprint"])
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for case in bundle["cases"]:
            for candidate in case["candidates"]:
                guard = candidate.get("guard") or {}
                writer.writerow({
                    "suite_id": bundle["suite_id"],
                    "database_fingerprint": fingerprint,
                    "case_id": case["case_id"],
                    "prompt": case["prompt"],
                    "review_status": case["review_status"],
                    "reviewer": case.get("reviewer") or "",
                    "reviewed_at": case.get("reviewed_at") or "",
                    "registration_no": candidate["registration_no"],
                    "title": candidate.get("title") or "",
                    "indication": candidate.get("indication") or "",
                    "phase": candidate.get("phase") or "",
                    "status": candidate.get("status") or "",
                    "first_publication_date": candidate.get("first_publication_date") or "",
                    "detail_url": candidate.get("detail_url") or "",
                    "guard_disposition": guard.get("disposition") or "",
                    "inclusion_excerpt": candidate.get("inclusion_excerpt") or "",
                    "exclusion_excerpt": candidate.get("exclusion_excerpt") or "",
                    "relevant": "",
                    "expected_disposition": "",
                    "expected_findings_json": "",
                    "review_notes": "",
                })
    return target


def render_markdown(report: dict[str, Any]) -> str:
    snapshot = report["database_snapshot"]
    aggregate = report["aggregate"]
    metrics = aggregate["metrics"]
    lines = [
        f"# CDE质量验收报告：{report['suite_id']}",
        "",
        f"- 总状态：`{aggregate['overall_status']}`",
        f"- 数据库指纹：`{snapshot['logical_fingerprint']}`",
        f"- 试验总数：{snapshot['trial_count']}",
        f"- 可招募试验：{snapshot['recruitable_trial_count']}",
        f"- 数据最新更新时间：{snapshot['latest_updated_at']}",
        f"- 固定评测参考日期：{report['reference_date']}",
        f"- 自动验收问题：{metrics['total_case_count']} 个",
        f"- 可选人工抽查：{metrics['reviewed_case_count']} 个（不影响自动验收状态）",
        "",
        "## 验收门槛",
        "",
        "| 指标 | 当前值 | 门槛 | 状态 |",
        "|---|---:|---:|---|",
    ]
    for name, gate in aggregate["gates"].items():
        value = gate["value"]
        shown = "—" if value is None else (f"{value:.4f}" if isinstance(value, float) else str(value))
        lines.append(f"| {name} | {shown} | {gate['threshold']} | {gate['status']} |")
    lines.extend([
        "",
        "## 问题明细",
        "",
        "| Case | 候选池 | 返回数 | 查询层总数 | 参考查询器总数 | Precision | Recall@K |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for case in report["cases"]:
        lines.append(
            f"| {case['case_id']} | {case['candidate_pool_size']} | {len(case['returned_ids'])} | "
            f"{case['raw_total_count']} | {case['oracle']['raw_total_count']} | "
            f"{case['retrieval']['precision'] if case['retrieval']['precision'] is not None else '—'} | "
            f"{case['retrieval']['recall_at_k'] if case['retrieval']['recall_at_k'] is not None else '—'} |"
        )
    lines.extend(["", "## 自动发现的差异", ""])
    differences = 0
    for case in report["cases"]:
        items = []
        if case["raw_total_count"] != case["oracle"]["raw_total_count"]:
            items.append(
                f"总匹配数：查询层 {case['raw_total_count']}，参考查询器 "
                f"{case['oracle']['raw_total_count']}"
            )
        unexpected = case["retrieval"].get("unexpected_returned_ids") or []
        missed = case["retrieval"].get("missed_top_k_ids") or []
        if unexpected:
            items.append("不符合参考契约的返回：" + "、".join(unexpected))
        if missed:
            items.append("遗漏的参考前K条：" + "、".join(missed))
        field_diffs = [item for item in case["source_field_checks"] if not item["match"]]
        if field_diffs:
            items.append(
                "来源字段不一致：" + "、".join(
                    f"{item['registration_no']}.{item['field']}" for item in field_diffs
                )
            )
        if items:
            differences += 1
            lines.append(f"- `{case['case_id']}`：" + "；".join(items) + "。")
    if not differences:
        lines.append("- 未发现差异。")
    lines.extend([
        "",
        "## 指标解释",
        "",
        "- `retrieval_precision`：返回结果中，有多少满足独立参考查询器实现的检索契约。",
        "- `retrieval_recall_at_k`：独立参考查询器前K条中，有多少进入实际返回结果。",
        "- `raw_total_count_accuracy`：实际总匹配数与独立参考查询器完全一致的问题比例。",
        "- `source_field_fidelity`：标题、适应症、分期、状态、日期和链接与正式库原值一致的比例。",
        "- `fact_trace_rate`：问题里明确给出的患者条件，被PatientFacts正确保留的比例。",
        "- `explicit_conflict_leakage_rate`：与用户明确筛选条件冲突的试验仍进入结果的比例，必须为0。",
        "",
        "本报告只评价信息解析、检索、过滤和字段返回是否正确，不评价患者最终能否入组。人工抽查仅用于审计，不是放行门槛。",
        "",
    ])
    return "\n".join(lines)
