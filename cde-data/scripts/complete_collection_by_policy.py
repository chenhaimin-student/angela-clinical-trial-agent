from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description="Complete a sync run at an explicit retention cutoff")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--cutoff-date", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--expected-pending", type=int, required=True)
    args = parser.parse_args()

    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        run = connection.execute("SELECT * FROM sync_runs WHERE id=?", (args.run_id,)).fetchone()
        if run is None or run["status"] != "running":
            raise RuntimeError("run is missing or no longer running")
        pending = connection.execute(
            "SELECT COUNT(*) FROM sync_items WHERE sync_run_id=? AND status='pending'",
            (args.run_id,),
        ).fetchone()[0]
        retry = connection.execute(
            "SELECT COUNT(*) FROM sync_items WHERE sync_run_id=? AND status='retry'",
            (args.run_id,),
        ).fetchone()[0]
        if pending != args.expected_pending or retry:
            raise RuntimeError(
                f"queue changed: expected pending={args.expected_pending}, actual pending={pending}, retry={retry}"
            )
        checkpoint = json.loads(run["checkpoint_json"] or "{}")
        checkpoint.update({
            "phase": "policy_complete",
            "completion_policy": {
                "type": "first_publication_date_cutoff",
                "cutoff_date": args.cutoff_date,
                "reason": args.reason,
                "skipped_items": pending,
                "completed_at": utc_now(),
                "reversible": True,
            },
        })
        finished = utc_now()
        with connection:
            connection.execute(
                """
                UPDATE sync_items
                SET status='skipped_policy',finished_at=?,last_error_code=NULL,
                    last_error_message=NULL,next_retry_at=NULL
                WHERE sync_run_id=? AND status='pending'
                """,
                (finished, args.run_id),
            )
            connection.execute(
                """
                UPDATE sync_runs
                SET status='policy_complete',finished_at=?,records_skipped=?,
                    checkpoint_json=?,error_summary=NULL
                WHERE id=?
                """,
                (finished, pending, json.dumps(checkpoint, ensure_ascii=False, sort_keys=True), args.run_id),
            )
        print(json.dumps({
            "run_id": args.run_id,
            "status": "policy_complete",
            "records_skipped": pending,
            "cutoff_date": args.cutoff_date,
            "finished_at": finished,
        }, ensure_ascii=False))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
