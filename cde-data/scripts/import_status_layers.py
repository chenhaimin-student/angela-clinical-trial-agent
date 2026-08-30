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


TARGET_STATUSES = ("尚未招募", "招募中", "招募完成", "已完成", "暂停", "终止")


def load_samples(paths: list[Path], per_status: int) -> dict[str, list[dict[str, str]]]:
    result = {status: [] for status in TARGET_STATUSES}
    seen = {status: set() for status in TARGET_STATUSES}
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for status in TARGET_STATUSES:
            for item in data.get("samples", {}).get(status, []):
                registration_no = item["registration_no"]
                if registration_no in seen[status] or len(result[status]) >= per_status:
                    continue
                result[status].append(item)
                seen[status].add(registration_no)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Import stratified CDE recruitment-status samples")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--worker-url", default="http://127.0.0.1:8766")
    parser.add_argument("--scan", type=Path, action="append", required=True)
    parser.add_argument("--per-status", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.per_status <= 20:
        raise SystemExit("--per-status must be between 1 and 20")

    samples = load_samples(args.scan, args.per_status)
    missing = [status for status, items in samples.items() if not items]
    if missing:
        raise SystemExit("no samples found for: " + ", ".join(missing))

    connection = initialize_database(args.database)
    worker = BrowserWorkerClient(args.worker_url)
    service = CdeSyncService(
        connection,
        args.raw_root,
        worker,
        parse_result_page,
        parse_cde_detail,
    )
    failed_total = 0
    try:
        for status in TARGET_STATUSES:
            layer = samples[status]
            run_id = service.start_or_resume(
                query_term=f"status-layer:{status}", pages=0, target=len(layer)
            )
            added = service.enqueue_samples(run_id, layer)
            print(
                f"LAYER {status}: run_id={run_id} selected={len(layer)} "
                f"newly_queued={added} processing...",
                flush=True,
            )
            summary = service.process(run_id, batch_size=1)
            failed_total += summary["failed"] + summary["pending"]
            coverage = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(EXISTS(SELECT 1 FROM trial_criteria c WHERE c.trial_version_id=t.current_version_id)) AS criteria,
                       SUM(EXISTS(SELECT 1 FROM trial_sites s WHERE s.trial_version_id=t.current_version_id)) AS sites,
                       SUM(EXISTS(SELECT 1 FROM trial_parties p WHERE p.trial_version_id=t.current_version_id)) AS parties,
                       SUM(EXISTS(SELECT 1 FROM trial_contacts x WHERE x.trial_version_id=t.current_version_id)) AS contacts
                FROM trials t
                WHERE t.registration_no IN (
                    SELECT registration_no FROM sync_items WHERE sync_run_id = ?
                )
                """,
                (run_id,),
            ).fetchone()
            print(
                f"LAYER {status}: success={summary['success']} failed={summary['failed']} "
                f"pending={summary['pending']} coverage="
                + json.dumps(dict(coverage), ensure_ascii=False, sort_keys=True),
                flush=True,
            )
        return 0 if failed_total == 0 else 2
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
