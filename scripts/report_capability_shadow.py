from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import fields
from pathlib import Path
from typing import Any

from angela_orchestration.capability_router import CapabilityRouter
from angela_orchestration.models import RequestMode, RouteDecision, RouteKind, SearchParameters
from angela_orchestration.router import IntentRouter


TUPLE_FIELDS = {
    "trial_phases",
    "prior_treatments",
    "biomarkers",
    "sponsors",
    "site_names",
    "project_codes",
    "scores",
    "comorbidities",
    "disease_statuses",
    "organ_function_findings",
    "reproductive_statuses",
    "allergies",
}


def _search(payload: dict[str, Any]) -> SearchParameters:
    allowed = {item.name for item in fields(SearchParameters)}
    values = {key: value for key, value in payload.items() if key in allowed}
    for key in TUPLE_FIELDS:
        if key in values:
            values[key] = tuple(values[key] or ())
    mode = values.get("request_mode")
    if mode:
        values["request_mode"] = RequestMode(mode)
    return SearchParameters(**values)


def review(
    report_path: Path,
    mode: str = "shadow",
    *,
    database_path: Path | None = None,
) -> dict[str, Any]:
    source = json.loads(report_path.read_text(encoding="utf-8"))
    router = CapabilityRouter(mode)
    project_root = Path(__file__).resolve().parents[1]
    deterministic_router = IntentRouter(
        project_root / "cde-data/config/disease_concepts.json",
        database_path=database_path,
    )
    rows: list[dict[str, Any]] = []
    tasks: Counter[str] = Counter()
    plans: Counter[str] = Counter()
    pre_model_plans: Counter[str] = Counter()
    legacy_routes: Counter[str] = Counter()
    for record in source.get("records", []):
        question = str(record.get("question") or "")
        deterministic = deterministic_router.classify(question)
        pre_model_plan = router.plan(
            question,
            deterministic,
            deterministic_decision=deterministic,
        )
        pre_model_plans[pre_model_plan.execution.value] += 1
        search = _search(record.get("current_search") or {})
        stage = record.get("stage_timings") or {}
        legacy_route = stage.get("execution_route") or "unknown"
        legacy_fast = legacy_route == "local_cde_query"
        decision = RouteDecision(
            RouteKind.CDE_SEARCH,
            0.0,
            "historical_replay",
            fast_eligible=legacy_fast,
            search=search,
        )
        plan = router.plan(
            question,
            decision,
            deterministic_decision=deterministic,
        )
        tasks[plan.task.value] += 1
        plans[plan.execution.value] += 1
        legacy_routes[legacy_route] += 1
        if plan.differs_from_legacy:
            rows.append({
                "case": record.get("case"),
                "legacy_route": legacy_route,
                "shadow_task": plan.task.value,
                "shadow_execution": plan.execution.value,
                "ready": plan.ready,
                "pre_model_execution": pre_model_plan.execution.value,
                "pre_model_ready": pre_model_plan.ready,
                "reasons": list(plan.reasons),
                "constraint_fields": [item.field for item in plan.constraints],
            })
    ready_main_to_local = sum(
        1 for row in rows
        if row["legacy_route"] == "main_agent"
        and str(row["shadow_execution"]).startswith("local_")
        and row["ready"]
    )
    planned_main_to_local = sum(
        1 for row in rows
        if row["legacy_route"] == "main_agent"
        and str(row["shadow_execution"]).startswith("local_")
        and not row["ready"]
    )
    local_to_main = sum(
        1 for row in rows
        if row["legacy_route"] == "local_cde_query"
        and row["shadow_execution"] == "main_agent"
    )
    pre_model_ready_promotions = sum(
        1 for row in rows
        if row["legacy_route"] == "main_agent"
        and row["pre_model_ready"]
        and str(row["pre_model_execution"]).startswith("local_")
    )
    return {
        "schema_version": 1,
        "source": str(report_path),
        "mode": mode,
        "total": len(source.get("records", [])),
        "legacy_routes": dict(legacy_routes),
        "shadow_tasks": dict(tasks),
        "shadow_execution": dict(plans),
        "pre_model_execution": dict(pre_model_plans),
        "differences": len(rows),
        "ready_main_agent_to_local_candidates": ready_main_to_local,
        "planned_main_agent_to_local_candidates": planned_main_to_local,
        "pre_model_ready_main_agent_to_local_candidates": pre_model_ready_promotions,
        "local_to_main_safety_candidates": local_to_main,
        "changed_cases": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Review capability routing in shadow mode")
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--mode", default="shadow", choices=("off", "shadow", "partial", "on"))
    args = parser.parse_args()
    result = review(args.report, args.mode, database_path=args.database)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
