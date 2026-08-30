from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from .cde_client import CdeBrowserWorkerClient, CdeWorkerError
from .models import InputPrivacyError, PatientProfile, reject_direct_identifiers
from .service import CdeLookupService


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a privacy-minimising CDE trial lookup")
    parser.add_argument("--input", required=True, help="Path to a de-identified JSON request")
    parser.add_argument("--worker-url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    try:
        raw = _load(Path(args.input))
        reject_direct_identifiers(raw)
        profile = PatientProfile.from_dict(_dict(raw.get("patient"), "patient"))
        query_terms = tuple(_query_terms(raw.get("query_terms")))
        max_results = raw.get("max_results", 3)
        if not isinstance(max_results, int):
            raise ValueError("max_results must be an integer")
        result = CdeLookupService(CdeBrowserWorkerClient(args.worker_url)).lookup(
            profile,
            query_terms,
            max_results,
            _published_after(raw.get("published_after")),
        )
    except (ValueError, InputPrivacyError, CdeWorkerError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read input JSON: {exc}") from exc
    return _dict(value, "request")


def _dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _query_terms(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError("query_terms must be a non-empty list of strings")
    return list(dict.fromkeys(item.strip() for item in value))[:3]


def _published_after(value: Any) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("published_after must be an ISO date such as 2026-05-06")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("published_after must be an ISO date such as 2026-05-06") from exc


if __name__ == "__main__":
    raise SystemExit(main())
