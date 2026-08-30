from __future__ import annotations

import argparse
import json
import sys
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import a bounded public CDE sample")
    parser.add_argument("--database", type=Path, default=ROOT / "database/cde_trials.sqlite3")
    parser.add_argument("--raw-root", type=Path, default=ROOT / "raw")
    parser.add_argument("--worker-url", default="http://127.0.0.1:8766")
    parser.add_argument("--query", default="试验")
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--target", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument(
        "--process-limit",
        type=int,
        help="process at most this many detail items in the current invocation",
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="record result-page candidates and exit before detail processing",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 1 <= args.pages <= 100:
        raise SystemExit("--pages must be between 1 and 100")
    if not 1 <= args.target <= 1000:
        raise SystemExit("--target must be between 1 and 1000")
    if not 1 <= args.batch_size <= 10:
        raise SystemExit("--batch-size must be between 1 and 10")
    if args.process_limit is not None and not 1 <= args.process_limit <= 1000:
        raise SystemExit("--process-limit must be between 1 and 1000")

    connection = initialize_database(args.database)
    try:
        worker = BrowserWorkerClient(args.worker_url)
        health = worker.health()
        if health.get("status") != "ready":
            raise SystemExit("batch worker is not ready")
        service = CdeSyncService(
            connection,
            args.raw_root,
            worker,
            parse_result_page,
            parse_cde_detail,
        )
        run_id = service.start_or_resume(
            query_term=args.query, pages=args.pages, target=args.target
        )
        discovered = service.discover(
            run_id, query_term=args.query, pages=args.pages, target=args.target
        )
        if args.discover_only:
            print(json.dumps({
                "run_id": run_id,
                "discovered_now": discovered,
                "status": "running",
            }, ensure_ascii=False))
            return 0
        summary = service.process(
            run_id,
            batch_size=args.batch_size,
            max_items=args.process_limit,
        )
        print(json.dumps({"run_id": run_id, "discovered_now": discovered, **summary}, ensure_ascii=False))
        return 0 if summary["failed"] == 0 and summary["pending"] == 0 else 2
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
