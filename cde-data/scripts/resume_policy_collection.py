from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def reopen_policy_collection(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    expected_skipped: int,
) -> dict[str, object]:
    connection.row_factory = sqlite3.Row
    run = connection.execute(
        "SELECT * FROM sync_runs WHERE id=?", (run_id,)
    ).fetchone()
    if run is None or run["run_type"] != "initial":
        raise RuntimeError("the requested initial collection does not exist")
    skipped = connection.execute(
        "SELECT COUNT(*) FROM sync_items WHERE sync_run_id=? AND status='skipped_policy'",
        (run_id,),
    ).fetchone()[0]
    if skipped == 0 and run["status"] in {"running", "partial", "success"}:
        return {
            "event": "already_resumed_or_complete",
            "run_id": run_id,
            "status": run["status"],
        }
    if run["status"] != "policy_complete" or skipped != expected_skipped:
        raise RuntimeError(
            f"unexpected policy state: status={run['status']} skipped={skipped}"
        )
    checkpoint = json.loads(run["checkpoint_json"] or "{}")
    completion_policy = checkpoint.pop("completion_policy", None)
    if not isinstance(completion_policy, dict) or not completion_policy.get("reversible"):
        raise RuntimeError("the completion policy is not marked reversible")
    resumed_at = utc_now()
    checkpoint["resumed_completion_policy"] = {
        **completion_policy,
        "resumed_at": resumed_at,
    }
    checkpoint["phase"] = "details"
    with connection:
        connection.execute(
            """
            UPDATE sync_items SET status='pending',finished_at=NULL,
                last_error_code=NULL,last_error_message=NULL,next_retry_at=NULL
            WHERE sync_run_id=? AND status='skipped_policy'
            """,
            (run_id,),
        )
        connection.execute(
            """
            UPDATE sync_runs SET status='partial',finished_at=NULL,
                records_skipped=0,checkpoint_json=?,error_summary=?
            WHERE id=?
            """,
            (
                json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                "policy_completion_reopened_for_full_detail_collection",
                run_id,
            ),
        )
    return {
        "event": "policy_completion_reopened",
        "run_id": run_id,
        "restored_pending": skipped,
        "resumed_at": resumed_at,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reopen a reversibly policy-completed CDE collection"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--expected-skipped", type=int, required=True)
    args = parser.parse_args()

    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        result = reopen_policy_collection(
            connection,
            run_id=args.run_id,
            expected_skipped=args.expected_skipped,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
