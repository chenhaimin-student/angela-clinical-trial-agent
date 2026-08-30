from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from .catalog_sync import parse_catalog_totals
from .raw_store import archive_html
from .repository import normalize_recruitment_status
from .worker_client import BrowserWorkerClient, WorkerClientError, WorkerResource


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _summary_equivalent(left: Any, right: Any) -> bool:
    """Tolerate detail-page annotations absent from the compact catalog."""
    first, second = _clean(left), _clean(right)
    if not first or not second:
        return False
    return first == second or first in second or second in first


def _detail_id(record: Any) -> str:
    value = str(getattr(record, "detail_id", "") or "").strip()
    if value:
        return value
    return parse_qs(urlparse(str(record.detail_url)).query).get("id", [""])[0]


def _description_parts(record: Any) -> tuple[str, str, str]:
    values = [value.strip() for value in str(getattr(record, "description", "") or "").split("|")]
    values.extend([""] * (3 - len(values)))
    return values[0], values[1], values[2]


def _list_signature(record: Any) -> str:
    status, drug, indication = _description_parts(record)
    payload = {
        "registration_no": str(record.registration_no).strip().upper(),
        "detail_id": _detail_id(record),
        "status": status,
        "drug": drug,
        "indication": indication,
        "title": str(getattr(record, "title", "") or "").strip(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DailySyncPolicy:
    initial_pages: int = 20
    max_pages: int = 20
    stop_after_no_new_pages: int = 2

    def __post_init__(self) -> None:
        if self.initial_pages < 1:
            raise ValueError("initial_pages must be positive")
        if self.max_pages < self.initial_pages:
            raise ValueError("max_pages must be at least initial_pages")
        if self.stop_after_no_new_pages < 1:
            raise ValueError("stop_after_no_new_pages must be positive")


class DailyIncrementalSync:
    """Plan auditable daily detail refreshes from lightweight catalog pages."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        raw_root: str | Path,
        worker: BrowserWorkerClient,
        parse_result_page: Callable[[str], list[Any]],
        *,
        policy: DailySyncPolicy = DailySyncPolicy(),
        request_interval_seconds: float = 2.5,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.connection = connection
        self.raw_root = Path(raw_root)
        self.worker = worker
        self.parse_result_page = parse_result_page
        self.policy = policy
        self.request_interval_seconds = request_interval_seconds
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_request_at: float | None = None

    def start_or_resume(self, schedule_date: date | None = None) -> int:
        day = (schedule_date or date.today()).isoformat()
        rows = self.connection.execute(
            "SELECT id,status,checkpoint_json,error_summary FROM sync_runs "
            "WHERE run_type='daily' ORDER BY id DESC"
        ).fetchall()
        for row in rows:
            checkpoint = json.loads(row["checkpoint_json"] or "{}")
            if checkpoint.get("schedule_date") != day:
                continue
            if row["status"] == "success":
                return int(row["id"])
            if row["status"] in {"running", "partial"}:
                if row["status"] == "partial" and "captcha" in str(row["error_summary"] or "").lower():
                    return int(row["id"])
                with self.connection:
                    self.connection.execute(
                        "UPDATE sync_runs SET status='running',finished_at=NULL,error_summary=NULL WHERE id=?",
                        (row["id"],),
                    )
                return int(row["id"])

        active = self.connection.execute(
            "SELECT id,run_type,checkpoint_json FROM sync_runs "
            "WHERE status='running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if active is not None:
            active_checkpoint = json.loads(active["checkpoint_json"] or "{}")
            if (
                active["run_type"] == "daily"
                and str(active_checkpoint.get("schedule_date") or "") < day
            ):
                with self.connection:
                    self.connection.execute(
                        "UPDATE sync_runs SET status='partial',finished_at=?,error_summary=? WHERE id=?",
                        (utc_now(), "stale_previous_daily_recovered", active["id"]),
                    )
            else:
                raise RuntimeError(
                    f"another database sync is marked running: run={active['id']} type={active['run_type']}"
                )

        checkpoint = {
            "mode": "daily_incremental",
            "schedule_date": day,
            "phase": "discovery",
            "next_page": 1,
            "initial_pages": self.policy.initial_pages,
            "max_pages": self.policy.max_pages,
            "stop_after_no_new_pages": self.policy.stop_after_no_new_pages,
            "extension_enabled": False,
            "consecutive_pages_without_new_ctr": 0,
            "records_seen": 0,
            "new_ctr": 0,
            "changed_ctr": 0,
            "unchanged_ctr": 0,
        }
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO sync_runs(run_type,started_at,status,pages_planned,checkpoint_json) "
                "VALUES ('daily',?,'running',?,?)",
                (
                    utc_now(),
                    self.policy.initial_pages,
                    json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                ),
            )
        run_id = int(cursor.lastrowid)
        self._carry_forward_failures(run_id)
        return run_id

    def discover(self, run_id: int) -> dict[str, Any]:
        checkpoint = self._checkpoint(run_id)
        if checkpoint.get("phase") != "discovery":
            return self.summary(run_id)

        while checkpoint["phase"] == "discovery":
            page = int(checkpoint["next_page"])
            result = self._fetch_page(run_id, page)
            if result.get("status") != "succeeded" or not result.get("content"):
                code = str(result.get("error_code") or "catalog_page_failed")
                self._record_catalog_page_failure(
                    run_id, page, int(checkpoint.get("pass", 1)), code
                )
                self.mark_partial(run_id, code)
                raise WorkerClientError(code)
            totals = parse_catalog_totals(result["content"])
            if totals.current_page != page:
                self._record_catalog_page_failure(
                    run_id, page, int(checkpoint.get("pass", 1)), "catalog_page_mismatch"
                )
                self.mark_partial(run_id, "catalog_page_mismatch")
                raise ValueError("CDE returned a different catalog page")
            archive_html(
                self.connection,
                self.raw_root,
                source_type="result_page",
                source_url=result["source_url"],
                content=result["content"],
                fetched_at=result["retrieved_at"],
            )
            records = self.parse_result_page(result["content"])
            expected = 20 if page < totals.total_pages else totals.total_records - 20 * (totals.total_pages - 1)
            if len(records) != expected:
                self._record_catalog_page_failure(
                    run_id,
                    page,
                    int(checkpoint.get("pass", 1)),
                    f"catalog_page_{page}_record_count_mismatch",
                )
                self.mark_partial(run_id, f"catalog_page_{page}_record_count_mismatch")
                raise ValueError(f"CDE returned {len(records)} records on page {page}, expected {expected}")
            self._record_catalog_page(
                run_id,
                page,
                int(checkpoint.get("pass", 1)),
                records,
                result["retrieved_at"],
            )
            self._mark_catalog_seen(records, result["retrieved_at"])

            page_new = 0
            page_changed = 0
            page_unchanged = 0
            for record in records:
                reasons = self._change_reasons(record)
                if reasons:
                    self._enqueue(run_id, record, page, reasons)
                    if "new_ctr" in reasons:
                        page_new += 1
                    else:
                        page_changed += 1
                else:
                    page_unchanged += 1

            checkpoint["records_seen"] += len(records)
            checkpoint["new_ctr"] += page_new
            checkpoint["changed_ctr"] += page_changed
            checkpoint["unchanged_ctr"] += page_unchanged
            if page_new:
                checkpoint["consecutive_pages_without_new_ctr"] = 0
            else:
                checkpoint["consecutive_pages_without_new_ctr"] += 1

            page_limit = min(int(checkpoint["max_pages"]), totals.total_pages)
            initial_limit = min(int(checkpoint["initial_pages"]), totals.total_pages)
            if page >= initial_limit and not checkpoint["extension_enabled"]:
                checkpoint["extension_enabled"] = page_new > 0 and page < page_limit
                if not checkpoint["extension_enabled"]:
                    checkpoint["phase"] = "details"
            if checkpoint["extension_enabled"] and (
                checkpoint["consecutive_pages_without_new_ctr"]
                >= int(checkpoint["stop_after_no_new_pages"])
                or page >= page_limit
            ):
                checkpoint["phase"] = "details"
            if page >= page_limit:
                checkpoint["phase"] = "details"
            checkpoint["next_page"] = page + 1
            self._save_checkpoint(run_id, checkpoint, page, page_limit)
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
            "schedule_date": checkpoint.get("schedule_date"),
            "phase": checkpoint.get("phase"),
            "next_page": checkpoint.get("next_page"),
            "records_seen": checkpoint.get("records_seen", 0),
            "new_ctr": checkpoint.get("new_ctr", 0),
            "changed_ctr": checkpoint.get("changed_ctr", 0),
            "unchanged_ctr": checkpoint.get("unchanged_ctr", 0),
            "queued": sum(counts.values()),
            "item_statuses": counts,
        }

    def manual_intervention_reason(self, run_id: int) -> str | None:
        row = self.connection.execute(
            "SELECT status,error_summary FROM sync_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None or row["status"] != "partial":
            return None
        reason = str(row["error_summary"] or "")
        return reason if "captcha" in reason.lower() else None

    def mark_partial(self, run_id: int, reason: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE sync_runs SET status='partial',finished_at=?,error_summary=? WHERE id=?",
                (utc_now(), reason[:2000], run_id),
            )

    def _change_reasons(self, record: Any) -> list[str]:
        ctr = str(record.registration_no).strip().upper()
        trial = self.connection.execute(
            "SELECT detail_id,public_title,professional_title,indication_raw,drug_name_raw,"
            "recruitment_status_normalized FROM trials WHERE registration_no=?",
            (ctr,),
        ).fetchone()
        if trial is None:
            return ["new_ctr"]
        reasons = []
        if _detail_id(record) and _clean(_detail_id(record)) != _clean(trial["detail_id"]):
            reasons.append("detail_id_changed")
        status, drug, indication = _description_parts(record)
        if str(normalize_recruitment_status(status)) != str(trial["recruitment_status_normalized"]):
            reasons.append("recruitment_status_changed")
        if drug and not _summary_equivalent(drug, trial["drug_name_raw"]):
            reasons.append("drug_changed")
        if indication and not _summary_equivalent(indication, trial["indication_raw"]):
            reasons.append("indication_changed")
        title = _clean(getattr(record, "title", ""))
        known_titles = {_clean(trial["public_title"]), _clean(trial["professional_title"])} - {""}
        if title and known_titles and not any(
            _summary_equivalent(title, known_title) for known_title in known_titles
        ):
            reasons.append("title_changed")
        return reasons

    def _enqueue(self, run_id: int, record: Any, page: int, reasons: list[str]) -> None:
        status, _drug, _indication = _description_parts(record)
        registration_no = str(record.registration_no).strip().upper()
        with self.connection:
            existing = self.connection.execute(
                "SELECT id FROM sync_items WHERE sync_run_id=? AND registration_no=? "
                "ORDER BY id LIMIT 1",
                (run_id, registration_no),
            ).fetchone()
            values = (
                str(record.detail_url),
                status or None,
                page,
                ",".join(reasons),
                _list_signature(record),
            )
            if existing is None:
                self.connection.execute(
                    """
                    INSERT INTO sync_items(
                        sync_run_id,registration_no,source_url,recruitment_status_raw,
                        discovered_page,discovery_pass,first_discovered_at,
                        discovery_reason,list_signature,status
                    ) VALUES (?,?,?,?,?,1,?,?,?,'pending')
                    """,
                    (
                        run_id,
                        registration_no,
                        values[0],
                        values[1],
                        values[2],
                        utc_now(),
                        values[3],
                        values[4],
                    ),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE sync_items SET source_url=?,recruitment_status_raw=?,
                        discovered_page=?,discovery_reason=?,list_signature=?
                    WHERE id=?
                    """,
                    (*values, existing["id"]),
                )

    def _mark_catalog_seen(self, records: list[Any], observed_at: str) -> None:
        with self.connection:
            self.connection.executemany(
                """
                UPDATE trials SET last_catalog_seen_at=?,catalog_missing_count=0
                WHERE registration_no=?
                """,
                [
                    (observed_at, str(record.registration_no).strip().upper())
                    for record in records
                ],
            )

    def _record_catalog_page(
        self,
        run_id: int,
        page: int,
        pass_no: int,
        records: list[Any],
        scanned_at: str,
    ) -> None:
        ctrs = [str(record.registration_no).strip().upper() for record in records]
        signature_payload = [_list_signature(record) for record in records]
        page_signature = hashlib.sha256(
            json.dumps(signature_payload, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO catalog_page_scans(
                    sync_run_id,pass_no,page_no,scanned_at,first_ctr,last_ctr,
                    page_signature,record_count,result_status
                ) VALUES (?,?,?,?,?,?,?,?,'success')
                ON CONFLICT(sync_run_id,pass_no,page_no) DO UPDATE SET
                    scanned_at=excluded.scanned_at,first_ctr=excluded.first_ctr,
                    last_ctr=excluded.last_ctr,page_signature=excluded.page_signature,
                    record_count=excluded.record_count,result_status='success',error_code=NULL
                """,
                (
                    run_id,
                    pass_no,
                    page,
                    scanned_at,
                    ctrs[0] if ctrs else None,
                    ctrs[-1] if ctrs else None,
                    page_signature,
                    len(records),
                ),
            )

    def _record_catalog_page_failure(
        self, run_id: int, page: int, pass_no: int, error_code: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO catalog_page_scans(
                    sync_run_id,pass_no,page_no,scanned_at,record_count,
                    result_status,error_code
                ) VALUES (?,?,?,?,0,'failed',?)
                ON CONFLICT(sync_run_id,pass_no,page_no) DO UPDATE SET
                    scanned_at=excluded.scanned_at,record_count=0,
                    result_status='failed',error_code=excluded.error_code
                """,
                (run_id, pass_no, page, utc_now(), error_code[:500]),
            )

    def _carry_forward_failures(self, run_id: int) -> None:
        previous = self.connection.execute(
            "SELECT id FROM sync_runs WHERE run_type='daily' AND id<>? ORDER BY id DESC LIMIT 1",
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

    def _fetch_page(self, run_id: int, page: int) -> dict[str, Any]:
        now = self.monotonic()
        if self._last_request_at is not None:
            remaining = self.request_interval_seconds - (now - self._last_request_at)
            if remaining > 0:
                self.sleep(remaining)
        self._last_request_at = self.monotonic()
        return self.worker.fetch(
            f"cde-daily-{run_id}",
            f"cde-daily-{run_id}-page-{page}-{uuid4().hex[:8]}",
            [WorkerResource(kind="catalog_page", page=page)],
        )[0]

    def _checkpoint(self, run_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT checkpoint_json FROM sync_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("sync run does not exist")
        return json.loads(row[0])

    def _save_checkpoint(self, run_id: int, checkpoint: dict[str, Any], page: int, planned: int) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE sync_runs SET pages_planned=?,pages_completed=?,records_discovered=(
                    SELECT COUNT(*) FROM sync_items WHERE sync_run_id=?
                ),records_skipped=?,checkpoint_json=?,status='running',error_summary=NULL
                WHERE id=?
                """,
                (
                    planned,
                    page,
                    run_id,
                    int(checkpoint.get("unchanged_ctr", 0)),
                    json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )
