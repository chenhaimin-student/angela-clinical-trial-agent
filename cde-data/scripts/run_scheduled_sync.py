from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
for path in (ROOT, PROJECT_ROOT / "cde-demo"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cde_demo.parser import parse_result_page
from cde_store.cde_parser import parse_cde_detail
from cde_store.daily_sync import DailyIncrementalSync, DailySyncPolicy
from cde_store.database import initialize_database
from cde_store.sync_lock import ExclusiveSyncLock, SyncAlreadyRunning
from cde_store.sync_service import CdeSyncService
from cde_store.weekly_sync import WeeklyCatalogReview
from cde_store.worker_client import BrowserWorkerClient, WorkerClientError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_event(path: Path, event: dict) -> None:
    payload = {"timestamp": utc_now(), **event}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one scheduled CDE synchronization policy")
    parser.add_argument("--run-type", choices=("daily", "weekly_review", "monthly_audit"), required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--progress-file", type=Path, required=True)
    parser.add_argument("--worker-url", default="http://127.0.0.1:8766")
    parser.add_argument("--initial-pages", type=int, default=20)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--stop-after-no-new-pages", type=int, default=2)
    parser.add_argument("--detail-chunk", type=int, default=50)
    parser.add_argument("--detail-batch-size", type=int, default=3)
    parser.add_argument("--schedule-date", type=date.fromisoformat)
    args = parser.parse_args()

    if args.run_type == "monthly_audit":
        append_event(args.progress_file, {
            "event": "not_implemented", "run_type": args.run_type,
            "reason": "monthly audit requires explicit approval before implementation",
        })
        return 64

    try:
        with ExclusiveSyncLock(args.lock_file):
            connection = initialize_database(args.database)
            try:
                worker = BrowserWorkerClient(args.worker_url)
                if args.run_type == "daily":
                    planner = DailyIncrementalSync(
                        connection,
                        args.raw_root,
                        worker,
                        parse_result_page,
                        policy=DailySyncPolicy(
                            initial_pages=args.initial_pages,
                            max_pages=args.max_pages,
                            stop_after_no_new_pages=args.stop_after_no_new_pages,
                        ),
                    )
                else:
                    planner = WeeklyCatalogReview(
                        connection,
                        args.raw_root,
                        worker,
                        parse_result_page,
                    )
                run_id = planner.start_or_resume(args.schedule_date)
                run_status = connection.execute(
                    "SELECT status FROM sync_runs WHERE id=?", (run_id,)
                ).fetchone()[0]
                if run_status == "success":
                    append_event(args.progress_file, {
                        "event": "already_complete", **planner.summary(run_id),
                    })
                    return 0
                manual_reason = planner.manual_intervention_reason(run_id)
                if manual_reason:
                    append_event(args.progress_file, {
                        "event": "manual_intervention_required",
                        "run_id": run_id,
                        "reason": manual_reason,
                    })
                    return 0
                health = worker.health()
                if health.get("status") != "ready":
                    planner.mark_partial(run_id, "worker_not_ready")
                    append_event(args.progress_file, {
                        "event": "paused", "run_id": run_id, "reason": "worker_not_ready",
                    })
                    return 2
                append_event(args.progress_file, {"event": "started_or_resumed", **planner.summary(run_id)})
                summary = planner.discover(run_id)
                append_event(args.progress_file, {"event": "discovery_complete", **summary})

                detail = CdeSyncService(
                    connection,
                    args.raw_root,
                    worker,
                    parse_result_page,
                    parse_cde_detail,
                    max_attempts=2,
                )
                while True:
                    result = detail.process(
                        run_id,
                        batch_size=args.detail_batch_size,
                        max_items=args.detail_chunk,
                    )
                    append_event(args.progress_file, {
                        "event": "detail_progress", "run_id": run_id, **result,
                    })
                    if result["pending"] == 0:
                        if result["failed"]:
                            append_event(args.progress_file, {
                                "event": "completed_with_item_failures",
                                "run_id": run_id,
                                "failed": result["failed"],
                                "exit_class": "business_partial",
                            })
                        # Per-record failures are a completed business outcome,
                        # not a process crash. They remain auditable/retryable in
                        # sync_items and must not trigger launchd KeepAlive.
                        return 0
                    if result["processed_this_call"] == 0:
                        error = connection.execute(
                            "SELECT last_error_code FROM sync_items WHERE sync_run_id=? "
                            "AND status IN ('retry','failed') ORDER BY id DESC LIMIT 1",
                            (run_id,),
                        ).fetchone()
                        reason = "retry_backoff_or_no_progress"
                        if error and error[0]:
                            reason = str(error[0])
                        planner.mark_partial(run_id, reason)
                        append_event(args.progress_file, {
                            "event": "paused", "run_id": run_id, "reason": reason,
                        })
                        return 3 if "captcha" in reason else 5
            finally:
                connection.close()
    except SyncAlreadyRunning as error:
        append_event(args.progress_file, {"event": "skipped", "reason": str(error)})
        return 0
    except (WorkerClientError, ValueError, RuntimeError) as error:
        reason = str(error)[:1000]
        append_event(args.progress_file, {
            "event": "failed", "error_type": type(error).__name__, "reason": reason,
        })
        return 3 if "captcha" in reason.lower() else 10


if __name__ == "__main__":
    raise SystemExit(main())
