from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .catalog_sync import parse_catalog_totals
from .daily_sync import DailyIncrementalSync, utc_now
from .raw_store import archive_html
from .worker_client import BrowserWorkerClient, WorkerClientError, WorkerResource


class WeeklyCatalogReview(DailyIncrementalSync):
    """Reconcile every current CDE catalog page before refreshing changed details."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        raw_root: str | Path,
        worker: BrowserWorkerClient,
        parse_result_page: Callable[[str], list[Any]],
        *,
        request_interval_seconds: float = 2.5,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        super().__init__(
            connection,
            raw_root,
            worker,
            parse_result_page,
            request_interval_seconds=request_interval_seconds,
            sleep=sleep,
            monotonic=monotonic,
        )

    @staticmethod
    def week_key(schedule_date: date) -> str:
        monday = schedule_date - timedelta(days=schedule_date.weekday())
        return monday.isoformat()

    def start_or_resume(self, schedule_date: date | None = None) -> int:
        key = self.week_key(schedule_date or date.today())
        rows = self.connection.execute(
            "SELECT id,status,checkpoint_json,error_summary FROM sync_runs "
            "WHERE run_type='weekly_review' ORDER BY id DESC"
        ).fetchall()
        for row in rows:
            checkpoint = json.loads(row["checkpoint_json"] or "{}")
            if checkpoint.get("week_key") != key:
                continue
            if row["status"] == "success":
                return int(row["id"])
            if row["status"] in {"running", "partial"}:
                if row["status"] == "partial" and "captcha" in str(
                    row["error_summary"] or ""
                ).lower():
                    return int(row["id"])
                with self.connection:
                    self.connection.execute(
                        "UPDATE sync_runs SET status='running',finished_at=NULL,error_summary=NULL WHERE id=?",
                        (row["id"],),
                    )
                return int(row["id"])

        active = self.connection.execute(
            "SELECT id,run_type FROM sync_runs WHERE status='running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if active is not None:
            raise RuntimeError(
                f"another database sync is marked running: run={active['id']} type={active['run_type']}"
            )
        checkpoint = {
            "mode": "weekly_catalog_review",
            "week_key": key,
            "phase": "discovery",
            "pass": 1,
            "pass_started_at": utc_now(),
            "next_page": 1,
            "declared_total_pages": None,
            "declared_total_records": None,
            "records_seen": 0,
            "new_ctr": 0,
            "changed_ctr": 0,
            "unchanged_ctr": 0,
            "catalog_restarts": 0,
        }
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO sync_runs(run_type,started_at,status,pages_planned,checkpoint_json)
                VALUES ('weekly_review',?,'running',0,?)
                """,
                (utc_now(), json.dumps(checkpoint, ensure_ascii=False, sort_keys=True)),
            )
        run_id = int(cursor.lastrowid)
        self._carry_forward_weekly_failures(run_id)
        return run_id

    def discover(self, run_id: int) -> dict[str, Any]:
        checkpoint = self._checkpoint(run_id)
        if checkpoint.get("phase") != "discovery":
            return self.summary(run_id)
        while checkpoint["phase"] == "discovery":
            page = int(checkpoint["next_page"])
            result = self._fetch_weekly_page(run_id, page, int(checkpoint["pass"]))
            if result.get("status") != "succeeded" or not result.get("content"):
                code = str(result.get("error_code") or "catalog_page_failed")
                self._record_catalog_page_failure(
                    run_id, page, int(checkpoint["pass"]), code
                )
                self.mark_partial(run_id, code)
                raise WorkerClientError(code)
            totals = parse_catalog_totals(result["content"])
            if totals.current_page != page:
                self._record_catalog_page_failure(
                    run_id, page, int(checkpoint["pass"]), "catalog_page_mismatch"
                )
                self.mark_partial(run_id, "catalog_page_mismatch")
                raise ValueError("CDE returned a different catalog page")
            if checkpoint["declared_total_pages"] is None:
                checkpoint["declared_total_pages"] = totals.total_pages
                checkpoint["declared_total_records"] = totals.total_records
            elif (
                totals.total_pages != checkpoint["declared_total_pages"]
                or totals.total_records != checkpoint["declared_total_records"]
            ):
                if int(checkpoint["catalog_restarts"]) >= 2:
                    self.mark_partial(run_id, "catalog_changed_repeatedly_during_weekly_review")
                    raise RuntimeError("CDE catalog changed repeatedly during weekly review")
                checkpoint.update({
                    "pass": int(checkpoint["pass"]) + 1,
                    "pass_started_at": utc_now(),
                    "next_page": 1,
                    "declared_total_pages": None,
                    "declared_total_records": None,
                    "records_seen": 0,
                    "new_ctr": 0,
                    "changed_ctr": 0,
                    "unchanged_ctr": 0,
                    "catalog_restarts": int(checkpoint["catalog_restarts"]) + 1,
                })
                self._save_weekly_checkpoint(run_id, checkpoint, 0)
                continue

            archive_html(
                self.connection,
                self.raw_root,
                source_type="result_page",
                source_url=result["source_url"],
                content=result["content"],
                fetched_at=result["retrieved_at"],
            )
            records = self.parse_result_page(result["content"])
            expected = (
                20
                if page < totals.total_pages
                else totals.total_records - 20 * (totals.total_pages - 1)
            )
            if len(records) != expected:
                self._record_catalog_page_failure(
                    run_id,
                    page,
                    int(checkpoint["pass"]),
                    f"catalog_page_{page}_record_count_mismatch",
                )
                self.mark_partial(run_id, f"catalog_page_{page}_record_count_mismatch")
                raise ValueError(
                    f"CDE returned {len(records)} records on page {page}, expected {expected}"
                )
            self._record_catalog_page(
                run_id,
                page,
                int(checkpoint["pass"]),
                records,
                result["retrieved_at"],
            )
            self._mark_catalog_seen(records, result["retrieved_at"])
            for record in records:
                reasons = self._change_reasons(record)
                if reasons:
                    self._enqueue(run_id, record, page, reasons)
                    if "new_ctr" in reasons:
                        checkpoint["new_ctr"] += 1
                    else:
                        checkpoint["changed_ctr"] += 1
                else:
                    checkpoint["unchanged_ctr"] += 1
            checkpoint["records_seen"] += len(records)
            checkpoint["next_page"] = page + 1
            if page >= totals.total_pages:
                checkpoint["phase"] = "details"
                self._reconcile_missing_catalog_records(
                    str(checkpoint["pass_started_at"])
                )
            self._save_weekly_checkpoint(run_id, checkpoint, page)
        return self.summary(run_id)

    def summary(self, run_id: int) -> dict[str, Any]:
        checkpoint = self._checkpoint(run_id)
        counts = {
            row["status"]: row["count"]
            for row in self.connection.execute(
                "SELECT status,COUNT(*) AS count FROM sync_items WHERE sync_run_id=? GROUP BY status",
                (run_id,),
            )
        }
        return {
            "run_id": run_id,
            "week_key": checkpoint.get("week_key"),
            "phase": checkpoint.get("phase"),
            "pass": checkpoint.get("pass"),
            "next_page": checkpoint.get("next_page"),
            "total_pages": checkpoint.get("declared_total_pages") or 0,
            "declared_total_records": checkpoint.get("declared_total_records") or 0,
            "records_seen": checkpoint.get("records_seen", 0),
            "new_ctr": checkpoint.get("new_ctr", 0),
            "changed_ctr": checkpoint.get("changed_ctr", 0),
            "unchanged_ctr": checkpoint.get("unchanged_ctr", 0),
            "catalog_restarts": checkpoint.get("catalog_restarts", 0),
            "queued": sum(counts.values()),
            "item_statuses": counts,
        }

    def _fetch_weekly_page(self, run_id: int, page: int, pass_no: int) -> dict[str, Any]:
        now = self.monotonic()
        if self._last_request_at is not None:
            remaining = self.request_interval_seconds - (now - self._last_request_at)
            if remaining > 0:
                self.sleep(remaining)
        self._last_request_at = self.monotonic()
        return self.worker.fetch(
            f"cde-weekly-{run_id}",
            f"cde-weekly-{run_id}-pass-{pass_no}-page-{page}-{uuid4().hex[:8]}",
            [WorkerResource(kind="catalog_page", page=page)],
        )[0]

    def _save_weekly_checkpoint(
        self, run_id: int, checkpoint: dict[str, Any], page: int
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE sync_runs SET pages_planned=?,pages_completed=?,records_discovered=(
                    SELECT COUNT(*) FROM sync_items WHERE sync_run_id=?
                ),records_skipped=?,checkpoint_json=?,status='running',error_summary=NULL
                WHERE id=?
                """,
                (
                    int(checkpoint.get("declared_total_pages") or 0),
                    page,
                    run_id,
                    int(checkpoint.get("unchanged_ctr", 0)),
                    json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )

    def _reconcile_missing_catalog_records(self, pass_started_at: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE trials SET catalog_missing_count=catalog_missing_count+1
                WHERE last_catalog_seen_at IS NULL OR last_catalog_seen_at<?
                """,
                (pass_started_at,),
            )

    def _carry_forward_weekly_failures(self, run_id: int) -> None:
        previous = self.connection.execute(
            "SELECT id FROM sync_runs WHERE run_type='weekly_review' AND id<>? "
            "ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if previous is None:
            return
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO sync_items(
                    sync_run_id,registration_no,source_url,recruitment_status_raw,
                    first_discovered_at,discovery_reason,list_signature,status
                )
                SELECT ?,registration_no,source_url,recruitment_status_raw,?,
                       'carry_forward_retry',list_signature,'pending'
                FROM sync_items WHERE sync_run_id=? AND status IN ('retry','failed')
                """,
                (run_id, utc_now(), previous["id"]),
            )
