from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
for path in (ROOT, PROJECT_ROOT / "cde-demo"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cde_demo.parser import parse_result_page
from cde_store.worker_client import BrowserWorkerClient, WorkerResource
from cde_store.database import initialize_database
from cde_store.raw_store import archive_html


def status_of(description: str) -> str:
    raw = description.split(" | ", 1)[0].replace(" ", "")
    for label in ("尚未招募", "招募完成", "招募中", "已完成", "暂停", "终止", "撤销"):
        if label in raw:
            return label
    return raw or "未知"


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan CDE result pages by recruitment status")
    parser.add_argument("--worker-url", default="http://127.0.0.1:8766")
    parser.add_argument("--query", default="试验")
    parser.add_argument("--pages", required=True, help="comma-separated result page numbers")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-status", type=int, default=10)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--request-interval", type=float, default=2.5)
    args = parser.parse_args()
    pages = list(dict.fromkeys(int(value) for value in args.pages.split(",")))
    if not pages or any(page < 1 or page > 100 for page in pages):
        raise SystemExit("pages must be between 1 and 100")

    worker = BrowserWorkerClient(args.worker_url)
    if worker.health().get("status") != "ready":
        raise SystemExit("batch worker is not ready")
    job_id = f"cde-layer-scan-{uuid4().hex}"
    samples: dict[str, list[dict[str, str]]] = {}
    totals: Counter[str] = Counter()
    completed = []
    connection = None
    if (args.database is None) != (args.raw_root is None):
        raise SystemExit("--database and --raw-root must be provided together")
    if args.request_interval < 0:
        raise SystemExit("--request-interval must not be negative")
    if args.database is not None:
        connection = initialize_database(args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for page_index, page in enumerate(pages):
        if page_index > 0 and args.request_interval:
            time.sleep(args.request_interval)
        result = worker.fetch(
            job_id,
            f"{job_id}-page-{page}",
            [WorkerResource(kind="result_page", query_term=args.query, page=page)],
        )[0]
        if result.get("status") != "succeeded":
            print(f"PAGE {page}: FAILED {result.get('error_code')}", flush=True)
            break
        if connection is not None:
            archive_html(
                connection,
                args.raw_root,
                source_type="result_page",
                source_url=result["source_url"],
                content=result["content"],
                fetched_at=result["retrieved_at"],
            )
        records = parse_result_page(result["content"])
        page_counts: Counter[str] = Counter()
        for record in records:
            status = status_of(record.description)
            page_counts[status] += 1
            totals[status] += 1
            bucket = samples.setdefault(status, [])
            if len(bucket) < args.per_status and all(
                item["registration_no"] != record.registration_no for item in bucket
            ):
                bucket.append({
                    "registration_no": record.registration_no,
                    "detail_id": record.detail_id,
                    "detail_url": record.detail_url,
                    "recruitment_status_raw": record.description.split(" | ", 1)[0],
                    "source_page": str(page),
                })
        completed.append(page)
        payload = {
            "query": args.query,
            "completed_pages": completed,
            "observed_counts": dict(totals),
            "samples": samples,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"PAGE {page}: records={len(records)} statuses="
            + json.dumps(dict(page_counts), ensure_ascii=False, sort_keys=True)
            + " cumulative="
            + json.dumps(dict(totals), ensure_ascii=False, sort_keys=True),
            flush=True,
        )
    print("SAMPLES " + json.dumps({key: len(value) for key, value in samples.items()}, ensure_ascii=False, sort_keys=True), flush=True)
    if connection is not None:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
