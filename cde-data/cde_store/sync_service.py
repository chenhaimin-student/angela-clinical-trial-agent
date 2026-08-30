from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from .raw_store import archive_html
from .repository import TrialRepository
from .worker_client import BrowserWorkerClient, WorkerClientError, WorkerResource


ParseResultPage = Callable[[str], list[Any]]
ParseTrialPage = Callable[[str, str, Optional[str]], Any]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CdeSyncService:
    """Auditable, resumable importer backed by sync_runs and sync_items."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        raw_root: str | Path,
        worker: BrowserWorkerClient,
        parse_result_page: ParseResultPage,
        parse_trial_page: ParseTrialPage,
        *,
        max_attempts: int = 3,
        request_interval_seconds: float = 2.5,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.connection = connection
        self.raw_root = Path(raw_root)
        self.worker = worker
        self.parse_result_page = parse_result_page
        self.parse_trial_page = parse_trial_page
        self.repository = TrialRepository(connection)
        self.max_attempts = max_attempts
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds must not be negative")
        self.request_interval_seconds = request_interval_seconds
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_worker_request_at: float | None = None

    def _throttled_fetch(
        self,
        job_id: str,
        request_id: str,
        resources: list[WorkerResource],
    ) -> list[dict[str, Any]]:
        now = self.monotonic()
        if self._last_worker_request_at is not None:
            remaining = self.request_interval_seconds - (now - self._last_worker_request_at)
            if remaining > 0:
                self.sleep(remaining)
        # Mark launch time immediately before the request. This protects the
        # CDE origin even when each Worker API call contains only one resource.
        self._last_worker_request_at = self.monotonic()
        return self.worker.fetch(job_id, request_id, resources)

    def start_or_resume(self, *, query_term: str, pages: int, target: int) -> int:
        checkpoint = {"query_term": query_term, "pages": pages, "target": target}
        encoded = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True)
        existing = self.connection.execute(
            """
            SELECT id FROM sync_runs
            WHERE run_type = 'test' AND status IN ('running', 'partial')
              AND checkpoint_json = ? ORDER BY id DESC LIMIT 1
            """,
            (encoded,),
        ).fetchone()
        if existing is not None:
            with self.connection:
                self.connection.execute(
                    "UPDATE sync_runs SET status = 'running', finished_at = NULL WHERE id = ?",
                    (existing["id"],),
                )
            return int(existing["id"])
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO sync_runs(
                    run_type, started_at, status, pages_planned, checkpoint_json
                ) VALUES ('test', ?, 'running', ?, ?)
                """,
                (utc_now(), pages, encoded),
            )
        return int(cursor.lastrowid)

    def discover(self, run_id: int, *, query_term: str, pages: int, target: int) -> int:
        discovered = 0
        if self._total_items(run_id) >= target:
            return 0
        completed_row = self.connection.execute(
            "SELECT pages_completed FROM sync_runs WHERE id = ?", (run_id,)
        ).fetchone()
        first_page = int(completed_row[0] or 0) + 1 if completed_row is not None else 1
        for page in range(first_page, pages + 1):
            result = self._throttled_fetch(
                f"cde-import-{run_id}",
                f"cde-import-{run_id}-result-{page}-{uuid4().hex[:8]}",
                [WorkerResource(kind="result_page", query_term=query_term, page=page)],
            )[0]
            if result.get("status") != "succeeded" or not result.get("content"):
                self._run_error(run_id, result.get("error_code") or "result_page_failed")
                break
            archive_html(
                self.connection,
                self.raw_root,
                source_type="result_page",
                source_url=result["source_url"],
                content=result["content"],
                fetched_at=result["retrieved_at"],
            )
            records = self.parse_result_page(result["content"])
            with self.connection:
                for record in records:
                    if self._total_items(run_id) >= target:
                        break
                    exists = self.connection.execute(
                        "SELECT 1 FROM sync_items WHERE sync_run_id = ? AND source_url = ?",
                        (run_id, record.detail_url),
                    ).fetchone()
                    if exists is None:
                        self.connection.execute(
                            """
                            INSERT INTO sync_items(
                                sync_run_id, registration_no, source_url,
                                recruitment_status_raw, status
                            ) VALUES (?, ?, ?, ?, 'pending')
                            """,
                            (
                                run_id,
                                record.registration_no,
                                record.detail_url,
                                self._record_recruitment_status(record),
                            ),
                        )
                        discovered += 1
                self.connection.execute(
                    """
                    UPDATE sync_runs SET pages_completed = ?, records_discovered =
                        (SELECT COUNT(*) FROM sync_items WHERE sync_run_id = ?)
                    WHERE id = ?
                    """,
                    (page, run_id, run_id),
                )
            if self._total_items(run_id) >= target:
                break
        return discovered

    def enqueue_samples(self, run_id: int, samples: list[dict[str, str]]) -> int:
        """Add already-discovered, auditable result-page samples to a run."""
        added = 0
        with self.connection:
            for sample in samples:
                exists = self.connection.execute(
                    "SELECT 1 FROM sync_items WHERE sync_run_id = ? AND source_url = ?",
                    (run_id, sample["detail_url"]),
                ).fetchone()
                if exists is not None:
                    continue
                self.connection.execute(
                    """
                    INSERT INTO sync_items(
                        sync_run_id, registration_no, source_url,
                        recruitment_status_raw, status
                    ) VALUES (?, ?, ?, ?, 'pending')
                    """,
                    (
                        run_id,
                        sample["registration_no"],
                        sample["detail_url"],
                        sample["recruitment_status_raw"],
                    ),
                )
                added += 1
            self.connection.execute(
                """
                UPDATE sync_runs SET records_discovered =
                    (SELECT COUNT(*) FROM sync_items WHERE sync_run_id = ?)
                WHERE id = ?
                """,
                (run_id, run_id),
            )
        return added

    def process(
        self,
        run_id: int,
        *,
        batch_size: int = 5,
        max_items: int | None = None,
    ) -> dict[str, int]:
        if max_items is not None and max_items < 1:
            raise ValueError("max_items must be positive when provided")
        processed_this_call = 0
        while True:
            if max_items is not None and processed_this_call >= max_items:
                break
            current_batch_size = batch_size
            if max_items is not None:
                current_batch_size = min(batch_size, max_items - processed_this_call)
            rows = self.connection.execute(
                """
                SELECT * FROM sync_items
                WHERE sync_run_id = ? AND status IN ('pending', 'retry')
                  AND attempt_count < ?
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY id LIMIT ?
                """,
                (run_id, self.max_attempts, utc_now(), current_batch_size),
            ).fetchall()
            if not rows:
                break
            resources = [
                WorkerResource(kind="detail", detail_id=self._detail_id(row["source_url"]))
                for row in rows
            ]
            try:
                results = self._throttled_fetch(
                    f"cde-import-{run_id}",
                    f"cde-import-{run_id}-detail-{rows[0]['id']}-{uuid4().hex[:8]}",
                    resources,
                )
            except WorkerClientError as error:
                for row in rows:
                    self._retry_item(row, "worker_unavailable", str(error))
                break
            for row, result in zip(rows, results):
                if result.get("error_code") == "cde_detail_invalid":
                    result = self._refresh_stale_detail(row, result)
                self._process_result(row, result)
                processed_this_call += 1
        summary = self.finish(run_id)
        summary["processed_this_call"] = processed_this_call
        return summary

    def _refresh_stale_detail(self, item: sqlite3.Row, original: dict[str, Any]) -> dict[str, Any]:
        """Resolve a replaced CDE detail id by exact CTR search, then retry once.

        CDE can keep a CTR record in search results while replacing its opaque
        detail id. Old URLs return an HTTP-200 page containing “获取信息失败”.
        Exact search is the authoritative way to refresh that unstable id.
        """
        registration_no = str(item["registration_no"] or "").strip()
        if not registration_no:
            return original
        try:
            search = self._throttled_fetch(
                f"cde-import-{item['sync_run_id']}",
                f"cde-refresh-{item['id']}-{uuid4().hex[:8]}",
                [WorkerResource(kind="result_page", query_term=registration_no, page=1)],
            )[0]
            if search.get("status") != "succeeded" or not search.get("content"):
                return search if search.get("error_code") == "captcha_detected" else original
            records = self.parse_result_page(search["content"])
            record = next(
                (candidate for candidate in records
                 if str(getattr(candidate, "registration_no", "")).strip() == registration_no),
                None,
            )
            if record is None:
                return original
            new_url = str(getattr(record, "detail_url", "") or "")
            new_id = self._detail_id(new_url)
            old_id = self._detail_id(str(item["source_url"]))
            refreshed_status = self._record_recruitment_status(record)
            with self.connection:
                self.connection.execute(
                    "UPDATE sync_items SET source_url=?, recruitment_status_raw=? WHERE id=?",
                    (new_url, refreshed_status, item["id"]),
                )
            if new_id == old_id:
                return original
            return self._throttled_fetch(
                f"cde-import-{item['sync_run_id']}",
                f"cde-refreshed-detail-{item['id']}-{uuid4().hex[:8]}",
                [WorkerResource(kind="detail", detail_id=new_id)],
            )[0]
        except (ValueError, WorkerClientError):
            return original

    def _process_result(self, item: sqlite3.Row, result: dict[str, Any]) -> None:
        if result.get("status") != "succeeded" or not result.get("content"):
            self._retry_item(
                item,
                result.get("error_code") or "detail_failed",
                result.get("error_message") or "detail fetch failed",
            )
            return
        try:
            previous = self.connection.execute(
                "SELECT id,current_version_id,detail_id FROM trials WHERE registration_no=?",
                (item["registration_no"],),
            ).fetchone()
            archived = archive_html(
                self.connection,
                self.raw_root,
                source_type="detail_page",
                source_url=result["source_url"],
                registration_no=item["registration_no"],
                content=result["content"],
                fetched_at=result["retrieved_at"],
                retry_count=item["attempt_count"],
            )
            parsed = self.parse_trial_page(
                result["content"],
                result["source_url"],
                self._result_status(item["sync_run_id"], item["registration_no"]),
            )
            parsed.snapshot["detail_id"] = self._detail_id(result["source_url"])
            outcome = self.repository.upsert_snapshot(
                parsed.snapshot,
                raw_document_id=archived.document_id,
                observed_at=result["retrieved_at"],
                parse_status="success",
                criteria=parsed.criteria,
                sites=parsed.sites,
                parties=parsed.parties,
                contacts=parsed.contacts,
            )
        except Exception as error:
            self._retry_item(item, "parse_or_store_failed", str(error))
            return
        with self.connection:
            changed_field_count = self.connection.execute(
                "SELECT COUNT(*) FROM trial_changes WHERE to_version_id=?",
                (outcome["version_id"],),
            ).fetchone()[0]
            review_required = self.connection.execute(
                """
                SELECT EXISTS(
                    SELECT 1 FROM trial_changes
                    WHERE to_version_id=? AND importance='high' AND review_status='pending'
                )
                """,
                (outcome["version_id"],),
            ).fetchone()[0]
            if outcome["change"] == "created":
                processing_outcome = "created"
            elif outcome["change"] == "updated":
                processing_outcome = "business_updated"
            elif outcome.get("technical_change"):
                processing_outcome = "technical_only"
            else:
                processing_outcome = "unchanged"
            self.connection.execute(
                """
                UPDATE sync_items SET status = 'success', attempt_count = attempt_count + 1,
                    last_error_code = NULL, last_error_message = NULL,
                    next_retry_at = NULL, finished_at = ?,processing_outcome=?,
                    before_version_id=?,after_version_id=?,old_detail_id=?,new_detail_id=?,
                    changed_field_count=?,review_required=? WHERE id = ?
                """,
                (
                    utc_now(),
                    processing_outcome,
                    previous["current_version_id"] if previous is not None else None,
                    outcome["version_id"],
                    previous["detail_id"] if previous is not None else None,
                    parsed.snapshot.get("detail_id"),
                    changed_field_count,
                    review_required,
                    item["id"],
                ),
            )
            if item["first_discovered_at"]:
                self.connection.execute(
                    """
                    UPDATE trials SET last_catalog_seen_at=?,catalog_missing_count=0
                    WHERE id=?
                    """,
                    (item["first_discovered_at"], outcome["trial_id"]),
                )
            column = {
                "created": "trials_created",
                "updated": "trials_updated",
                "unchanged": "trials_unchanged",
            }[outcome["change"]]
            self.connection.execute(
                f"UPDATE sync_runs SET {column} = {column} + 1 WHERE id = ?",
                (item["sync_run_id"],),
            )

    def _retry_item(self, item: sqlite3.Row, code: str, message: str) -> None:
        attempts = int(item["attempt_count"]) + 1
        final = attempts >= self.max_attempts
        next_retry = None if final else (
            datetime.now(timezone.utc) + timedelta(seconds=min(300, 5 * (2 ** (attempts - 1))))
        ).isoformat(timespec="seconds")
        with self.connection:
            self.connection.execute(
                """
                UPDATE sync_items SET status = ?, attempt_count = ?, last_error_code = ?,
                    last_error_message = ?, next_retry_at = ?, finished_at = ? WHERE id = ?
                """,
                (
                    "failed" if final else "retry",
                    attempts,
                    code,
                    message[:2000],
                    next_retry,
                    utc_now() if final else None,
                    item["id"],
                ),
            )

    def finish(self, run_id: int) -> dict[str, int]:
        counts = {
            row["status"]: row["count"]
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM sync_items WHERE sync_run_id = ? GROUP BY status",
                (run_id,),
            )
        }
        pending = counts.get("pending", 0) + counts.get("retry", 0)
        failed = counts.get("failed", 0)
        status = "running" if pending else ("partial" if failed else "success")
        with self.connection:
            self.connection.execute(
                """
                UPDATE sync_runs SET status = ?, finished_at = ?, records_failed = ?,
                    retry_count = (SELECT COALESCE(SUM(MAX(attempt_count - 1, 0)), 0)
                                   FROM sync_items WHERE sync_run_id = ?)
                WHERE id = ?
                """,
                (status, None if status == "running" else utc_now(), failed, run_id, run_id),
            )
        return {"success": counts.get("success", 0), "failed": failed, "pending": pending}

    def _run_error(self, run_id: int, message: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE sync_runs SET status = 'partial', error_summary = ? WHERE id = ?",
                (message[:2000], run_id),
            )

    def _result_status(self, run_id: int, registration_no: str) -> str | None:
        row = self.connection.execute(
            "SELECT recruitment_status_raw FROM sync_items WHERE sync_run_id = ? AND registration_no = ?",
            (run_id, registration_no),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _record_recruitment_status(record: Any) -> str | None:
        description = str(getattr(record, "description", "") or "")
        value = description.split(" | ", 1)[0].strip()
        return value or None

    @staticmethod
    def _detail_id(url: str) -> str:
        import re
        from urllib.parse import parse_qs, urlparse

        value = parse_qs(urlparse(url).query).get("id", [""])[0]
        if not re.fullmatch(r"(?:[a-fA-F0-9]{32}(?:_p)?|[0-9]{1,20}_p)", value):
            raise ValueError("CDE detail URL is missing a valid detail id")
        return value

    def _total_items(self, run_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM sync_items WHERE sync_run_id = ?", (run_id,)
        ).fetchone()[0])
