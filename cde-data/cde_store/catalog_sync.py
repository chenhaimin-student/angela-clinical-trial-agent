from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from bs4 import BeautifulSoup

from .raw_store import archive_html
from .worker_client import BrowserWorkerClient, WorkerClientError, WorkerResource


CATALOG_TOTALS = re.compile(
    r"当前第\s*(\d+)\s*页，共\s*(\d+)\s*页，共\s*(\d+)\s*条记录"
)


@dataclass(frozen=True)
class CatalogTotals:
    current_page: int
    total_pages: int
    total_records: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_catalog_totals(content: str) -> CatalogTotals:
    text = BeautifulSoup(content, "html.parser").get_text(" ", strip=True)
    match = CATALOG_TOTALS.search(text)
    if match is None:
        raise ValueError("CDE catalog totals are missing")
    values = CatalogTotals(*(int(value) for value in match.groups()))
    if values.total_pages < 1 or values.total_records < 1:
        raise ValueError("CDE catalog totals are invalid")
    expected_pages = (values.total_records + 19) // 20
    if values.total_pages != expected_pages:
        raise ValueError("CDE catalog page and record totals are inconsistent")
    if not 1 <= values.current_page <= values.total_pages:
        raise ValueError("CDE catalog current page is out of range")
    return values


class CatalogSyncService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        raw_root: str | Path,
        worker: BrowserWorkerClient,
        parse_result_page: Callable[[str], list[object]],
        *,
        request_interval_seconds: float = 2.5,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.connection = connection
        self.raw_root = Path(raw_root)
        self.worker = worker
        self.parse_result_page = parse_result_page
        self.request_interval_seconds = request_interval_seconds
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_request_at: float | None = None

    def start_or_resume(self) -> int:
        row = self.connection.execute(
            """
            SELECT id FROM sync_runs WHERE run_type='initial'
              AND status IN ('running', 'partial')
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if row is not None:
            with self.connection:
                self.connection.execute(
                    "UPDATE sync_runs SET status='running', error_summary=NULL WHERE id=?",
                    (int(row["id"]),),
                )
            return int(row["id"])
        checkpoint = {
            "mode": "full_catalog",
            "phase": "discovery",
            "pass": 1,
            "next_page": 1,
            "declared_total_pages": None,
            "declared_total_records": None,
        }
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO sync_runs(
                    run_type, started_at, status, pages_planned, checkpoint_json
                ) VALUES ('initial', ?, 'running', 0, ?)
                """,
                (utc_now(), json.dumps(checkpoint, ensure_ascii=False, sort_keys=True)),
            )
        return int(cursor.lastrowid)

    def discover(self, run_id: int, *, max_pages_this_call: int | None = None) -> dict[str, int | str]:
        checkpoint = self._checkpoint(run_id)
        if checkpoint.get("phase") != "discovery":
            return self.summary(run_id)
        processed = 0
        while max_pages_this_call is None or processed < max_pages_this_call:
            page = int(checkpoint["next_page"])
            result = self._fetch_page(run_id, page, int(checkpoint["pass"]))
            if result.get("status") != "succeeded" or not result.get("content"):
                code = str(result.get("error_code") or "catalog_page_failed")
                self._pause(run_id, code)
                raise WorkerClientError(code)
            totals = parse_catalog_totals(result["content"])
            if totals.current_page != page:
                self._pause(run_id, "catalog_page_mismatch")
                raise ValueError("CDE returned a different catalog page")
            if checkpoint["declared_total_pages"] is None:
                checkpoint["declared_total_pages"] = totals.total_pages
                checkpoint["declared_total_records"] = totals.total_records
            elif (
                totals.total_pages != checkpoint["declared_total_pages"]
                or totals.total_records != checkpoint["declared_total_records"]
            ):
                # The live catalog changed during traversal. Finish the current
                # pass, then reconciliation starts from the new first page.
                checkpoint["catalog_changed_during_pass"] = True

            archive_html(
                self.connection,
                self.raw_root,
                source_type="result_page",
                source_url=result["source_url"],
                content=result["content"],
                fetched_at=result["retrieved_at"],
            )
            records = self.parse_result_page(result["content"])
            expected_on_page = 20 if page < totals.total_pages else totals.total_records - 20 * (totals.total_pages - 1)
            if len(records) != expected_on_page:
                self._pause(run_id, f"catalog_page_{page}_record_count_mismatch")
                raise ValueError(f"catalog page {page} returned {len(records)} records, expected {expected_on_page}")
            self._enqueue(run_id, records, page=page, pass_number=int(checkpoint["pass"]))
            processed += 1
            if page >= int(checkpoint["declared_total_pages"]):
                unique_count = self._item_count(run_id)
                declared = int(checkpoint["declared_total_records"])
                if unique_count >= declared and not checkpoint.get("catalog_changed_during_pass"):
                    checkpoint["phase"] = "details"
                    checkpoint["discovery_completed_at"] = utc_now()
                    self._save_checkpoint(run_id, checkpoint, page)
                    break
                checkpoint["pass"] = int(checkpoint["pass"]) + 1
                checkpoint["next_page"] = 1
                checkpoint["declared_total_pages"] = None
                checkpoint["declared_total_records"] = None
                checkpoint["catalog_changed_during_pass"] = False
                self._save_checkpoint(run_id, checkpoint, 0)
                continue
            checkpoint["next_page"] = page + 1
            self._save_checkpoint(run_id, checkpoint, page)
        result = self.summary(run_id)
        result["pages_processed_this_call"] = processed
        return result

    def summary(self, run_id: int) -> dict[str, int | str]:
        checkpoint = self._checkpoint(run_id)
        return {
            "run_id": run_id,
            "phase": str(checkpoint["phase"]),
            "pass": int(checkpoint["pass"]),
            "next_page": int(checkpoint["next_page"]),
            "total_pages": int(checkpoint["declared_total_pages"] or 0),
            "declared_total_records": int(checkpoint["declared_total_records"] or 0),
            "unique_records": self._item_count(run_id),
        }

    def _fetch_page(self, run_id: int, page: int, pass_number: int) -> dict:
        now = self.monotonic()
        if self._last_request_at is not None:
            remaining = self.request_interval_seconds - (now - self._last_request_at)
            if remaining > 0:
                self.sleep(remaining)
        self._last_request_at = self.monotonic()
        return self.worker.fetch(
            f"cde-full-catalog-{run_id}",
            f"cde-full-catalog-{run_id}-pass-{pass_number}-page-{page}-{uuid4().hex[:8]}",
            [WorkerResource(kind="catalog_page", page=page)],
        )[0]

    def _enqueue(
        self,
        run_id: int,
        records: list[object],
        *,
        page: int,
        pass_number: int,
    ) -> None:
        with self.connection:
            for record in records:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO sync_items(
                        sync_run_id, registration_no, source_url,
                        recruitment_status_raw, status, discovered_page,
                        discovery_pass, first_discovered_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        run_id,
                        record.registration_no,
                        record.detail_url,
                        str(record.description).split(" | ", 1)[0].strip() or None,
                        page,
                        pass_number,
                        utc_now(),
                    ),
                )
            self.connection.execute(
                """
                UPDATE sync_runs SET records_discovered=(
                    SELECT COUNT(*) FROM sync_items WHERE sync_run_id=?
                ) WHERE id=?
                """,
                (run_id, run_id),
            )

    def _checkpoint(self, run_id: int) -> dict:
        row = self.connection.execute(
            "SELECT checkpoint_json FROM sync_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("sync run does not exist")
        return json.loads(row[0])

    def _save_checkpoint(self, run_id: int, checkpoint: dict, completed_page: int) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE sync_runs SET pages_planned=?, pages_completed=?,
                    checkpoint_json=?, status='running', error_summary=NULL
                WHERE id=?
                """,
                (
                    int(checkpoint.get("declared_total_pages") or 0),
                    completed_page,
                    json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )

    def _pause(self, run_id: int, error: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE sync_runs SET status='partial', error_summary=? WHERE id=?",
                (error, run_id),
            )

    def _item_count(self, run_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM sync_items WHERE sync_run_id=?", (run_id,)
        ).fetchone()[0])
