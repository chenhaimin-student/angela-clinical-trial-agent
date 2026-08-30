from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
for path in (ROOT, PROJECT_ROOT / "cde-demo"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cde_demo.parser import parse_result_page
from cde_store.catalog_sync import CatalogSyncService
from cde_store.cde_parser import parse_cde_detail
from cde_store.database import initialize_database
from cde_store.sync_service import CdeSyncService
from cde_store.sync_lock import ExclusiveSyncLock
from cde_store.worker_client import BrowserWorkerClient


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def append_progress(path: Path, event: dict) -> None:
    event = {"timestamp": utc_now(), **event}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume the production full-catalog CDE collection")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--progress-file", type=Path, required=True)
    parser.add_argument("--lock-file", type=Path)
    parser.add_argument("--worker-url", default="http://127.0.0.1:8766")
    parser.add_argument("--discovery-chunk", type=int, default=50)
    parser.add_argument("--detail-chunk", type=int, default=50)
    parser.add_argument("--detail-batch-size", type=int, default=3)
    args = parser.parse_args()

    lock = ExclusiveSyncLock(args.lock_file) if args.lock_file else nullcontext()
    with lock:
        return run_collection(args)


def run_collection(args: argparse.Namespace) -> int:

    connection = initialize_database(args.database)
    worker = BrowserWorkerClient(args.worker_url)
    started = time.monotonic()
    try:
        health = worker.health()
        if health.get("status") != "ready":
            append_progress(args.progress_file, {"event": "paused", "reason": "worker_not_ready"})
            return 2

        catalog = CatalogSyncService(connection, args.raw_root, worker, parse_result_page)
        run_id = catalog.start_or_resume()
        append_progress(args.progress_file, {
            "event": "started_or_resumed", "run_id": run_id, **catalog.summary(run_id),
        })
        while catalog.summary(run_id)["phase"] == "discovery":
            summary = catalog.discover(run_id, max_pages_this_call=args.discovery_chunk)
            append_progress(args.progress_file, {
                "event": "discovery_progress", **summary,
                "database_bytes": tree_size(args.database),
                "raw_bytes": tree_size(args.raw_root),
                "elapsed_seconds": round(time.monotonic() - started, 1),
            })

        detail = CdeSyncService(
            connection, args.raw_root, worker, parse_result_page, parse_cde_detail,
        )
        while True:
            captcha = connection.execute(
                "SELECT COUNT(*) FROM sync_items WHERE sync_run_id=? "
                "AND status IN ('retry', 'failed') "
                "AND last_error_code LIKE '%captcha%'", (run_id,),
            ).fetchone()[0]
            if captcha:
                append_progress(args.progress_file, {
                    "event": "paused", "run_id": run_id, "reason": "captcha_detected",
                    "captcha_items": captcha,
                })
                return 3
            summary = detail.process(
                run_id, batch_size=args.detail_batch_size, max_items=args.detail_chunk,
            )
            append_progress(args.progress_file, {
                "event": "detail_progress", "run_id": run_id, **summary,
                "database_bytes": tree_size(args.database),
                "raw_bytes": tree_size(args.raw_root),
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "worker_status": worker.health().get("status"),
            })
            if summary["pending"] == 0:
                if summary["failed"]:
                    append_progress(args.progress_file, {
                        "event": "completed_with_failures", "run_id": run_id,
                        "success": summary["success"], "failed": summary["failed"],
                    })
                return 0
            if summary["processed_this_call"] == 0:
                append_progress(args.progress_file, {
                    "event": "paused", "run_id": run_id, "reason": "retry_backoff_or_no_progress",
                })
                return 5
    except Exception as error:
        append_progress(args.progress_file, {
            "event": "paused", "error_type": type(error).__name__, "reason": str(error)[:1000],
        })
        return 10
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
