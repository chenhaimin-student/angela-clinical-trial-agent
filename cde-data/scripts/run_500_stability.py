from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
for path in (ROOT, PROJECT_ROOT / "cde-demo"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cde_demo.parser import parse_result_page
from cde_store.cde_parser import parse_cde_detail
from cde_store.database import initialize_database
from cde_store.sync_service import CdeSyncService
from cde_store.worker_client import BrowserWorkerClient


def tree_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume the bounded 500-record CDE stability run")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--worker-url", default="http://127.0.0.1:8766")
    parser.add_argument("--query", default="试验")
    parser.add_argument("--pages", type=int, default=25)
    parser.add_argument("--target", type=int, default=500)
    parser.add_argument("--batch-limit", type=int, default=50)
    parser.add_argument("--progress-file", type=Path, required=True)
    args = parser.parse_args()

    connection = initialize_database(args.database)
    worker = BrowserWorkerClient(args.worker_url)
    service = CdeSyncService(
        connection,
        args.raw_root,
        worker,
        parse_result_page,
        parse_cde_detail,
    )
    started = time.monotonic()
    args.progress_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        if worker.health().get("status") != "ready":
            raise SystemExit("batch worker is not ready")
        run_id = service.start_or_resume(
            query_term=args.query, pages=args.pages, target=args.target
        )
        service.discover(run_id, query_term=args.query, pages=args.pages, target=args.target)
        while True:
            before = connection.execute(
                "SELECT COUNT(*) FROM sync_items WHERE sync_run_id=? AND status='success'",
                (run_id,),
            ).fetchone()[0]
            if before >= args.target:
                return 0
            summary = service.process(
                run_id,
                batch_size=5,
                max_items=args.batch_limit,
            )
            after = summary["success"]
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "run_id": run_id,
                "batch_number": (after + args.batch_limit - 1) // args.batch_limit,
                "processed_this_batch": summary["processed_this_call"],
                "success": after,
                "failed": summary["failed"],
                "pending": summary["pending"],
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "database_bytes": tree_size(args.database),
                "raw_bytes": tree_size(args.raw_root),
                "worker_status": worker.health().get("status"),
            }
            line = json.dumps(record, ensure_ascii=False)
            with args.progress_file.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
            print(line, flush=True)
            if summary["failed"] > 0:
                return 2
            if summary["pending"] == 0:
                return 0
            if summary["processed_this_call"] == 0:
                return 3
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
