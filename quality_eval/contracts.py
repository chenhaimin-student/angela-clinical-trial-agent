from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ALLOWED_REVIEW_STATES = {"pending", "in_review", "complete"}


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    prompt: str
    search: dict[str, Any]
    expected_facts: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


@dataclass(frozen=True)
class EvaluationSuite:
    schema_version: int
    suite_id: str
    description: str
    reference_date: str
    thresholds: dict[str, float]
    cases: tuple[EvaluationCase, ...]


def load_suite(path: str | Path) -> EvaluationSuite:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("quality suite schema_version must be 1")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("quality suite must contain at least one case")
    seen: set[str] = set()
    cases: list[EvaluationCase] = []
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise ValueError("each quality case must be an object")
        case_id = str(raw.get("case_id") or "").strip()
        prompt = str(raw.get("prompt") or "").strip()
        search = raw.get("search")
        if not case_id or case_id in seen:
            raise ValueError(f"invalid or duplicate case_id: {case_id!r}")
        if not prompt or not isinstance(search, dict) or not search.get("disease"):
            raise ValueError(f"case {case_id} requires prompt and search.disease")
        seen.add(case_id)
        cases.append(EvaluationCase(
            case_id=case_id,
            prompt=prompt,
            search=dict(search),
            expected_facts=dict(raw.get("expected_facts") or {}),
            notes=str(raw.get("notes") or ""),
        ))
    thresholds = {
        str(key): float(value)
        for key, value in dict(payload.get("thresholds") or {}).items()
    }
    return EvaluationSuite(
        schema_version=1,
        suite_id=str(payload.get("suite_id") or Path(path).stem),
        description=str(payload.get("description") or ""),
        reference_date=str(payload.get("reference_date") or ""),
        thresholds=thresholds,
        cases=tuple(cases),
    )


def load_annotations(path: str | Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    source = Path(path)
    if source.suffix.casefold() == ".csv":
        return _load_annotations_csv(source)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("annotation schema_version must be 1")
    output: dict[str, dict[str, Any]] = {}
    for item in payload.get("cases") or []:
        case_id = str(item.get("case_id") or "").strip()
        state = str(item.get("review_status") or "pending")
        if not case_id or state not in ALLOWED_REVIEW_STATES:
            raise ValueError("annotation case has invalid id or review_status")
        candidates = item.get("candidates") or []
        labels: dict[str, bool] = {}
        guard_labels: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            ctr = str(candidate.get("registration_no") or "").strip().upper()
            relevant = candidate.get("relevant")
            if ctr and isinstance(relevant, bool):
                labels[ctr] = relevant
            disposition = candidate.get("expected_disposition")
            findings = candidate.get("expected_findings")
            if ctr and (isinstance(disposition, str) or isinstance(findings, dict)):
                guard_labels[ctr] = {
                    "disposition": disposition,
                    "findings": dict(findings or {}),
                }
        output[case_id] = {
            "review_status": state,
            "labels": labels,
            "guard_labels": guard_labels,
            "reviewer": item.get("reviewer"),
            "reviewed_at": item.get("reviewed_at"),
        }
    return output


def _load_annotations_csv(path: Path) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            case_id = str(row.get("case_id") or "").strip()
            ctr = str(row.get("registration_no") or "").strip().upper()
            state = str(row.get("review_status") or "pending").strip()
            if not case_id or not ctr or state not in ALLOWED_REVIEW_STATES:
                raise ValueError("annotation CSV has invalid case_id, CTR, or review_status")
            item = output.setdefault(case_id, {
                "review_status": state,
                "labels": {},
                "guard_labels": {},
                "reviewer": row.get("reviewer") or None,
                "reviewed_at": row.get("reviewed_at") or None,
            })
            if item["review_status"] != state:
                raise ValueError(f"annotation CSV case {case_id} has mixed review_status values")
            relevant = str(row.get("relevant") or "").strip().casefold()
            if relevant in {"true", "1", "yes", "是"}:
                item["labels"][ctr] = True
            elif relevant in {"false", "0", "no", "否"}:
                item["labels"][ctr] = False
            disposition = str(row.get("expected_disposition") or "").strip() or None
            findings_text = str(row.get("expected_findings_json") or "").strip()
            findings = json.loads(findings_text) if findings_text else {}
            if disposition or findings:
                item["guard_labels"][ctr] = {
                    "disposition": disposition,
                    "findings": findings,
                }
    return output


def annotation_metadata(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}
    source = Path(path)
    if source.suffix.casefold() == ".csv":
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle), {})
        return {
            "suite_id": str(row.get("suite_id") or ""),
            "logical_fingerprint": str(row.get("database_fingerprint") or ""),
        }
    payload = json.loads(source.read_text(encoding="utf-8"))
    return {
        "suite_id": str(payload.get("suite_id") or ""),
        "logical_fingerprint": str(
            (payload.get("database_snapshot") or {}).get("logical_fingerprint") or ""
        ),
    }
