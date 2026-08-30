from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cde_store.daily_sync import (
    DailyIncrementalSync,
    DailySyncPolicy,
    _summary_equivalent,
)
from cde_store.database import initialize_database
from cde_store.sync_lock import ExclusiveSyncLock, SyncAlreadyRunning


@dataclass(frozen=True)
class Record:
    registration_no: str
    detail_url: str
    detail_id: str
    title: str = "公开研究"
    description: str = "进行中 招募中 | 示例药物 | 肺癌"


def catalog_html(page: int, pages: int, records: int) -> str:
    return f"<html><body>当前第 {page} 页，共 {pages} 页，共 {records} 条记录 PAGE:{page}</body></html>"


class Worker:
    def __init__(self, pages: int):
        self.pages = pages
        self.calls: list[int] = []

    def fetch(self, _job_id, _request_id, resources):
        page = resources[0].page
        self.calls.append(page)
        return [{
            "status": "succeeded",
            "content": catalog_html(page, self.pages, self.pages * 20),
            "source_url": f"https://example.test/catalog?page={page}",
            "retrieved_at": "2026-08-14T00:00:00+00:00",
        }]


class DailySyncTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.connection = initialize_database(self.root / "daily.sqlite3")
        self.records_by_page = {
            page: [self.record(page, index) for index in range(1, 21)]
            for page in range(1, 21)
        }

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    @staticmethod
    def record(page: int, index: int) -> Record:
        number = (page - 1) * 20 + index
        detail_id = f"{number:032x}"
        return Record(
            registration_no=f"CTR2026{number:04d}",
            detail_url=f"https://example.test/detail?id={detail_id}",
            detail_id=detail_id,
        )

    def seed(self, record: Record, *, status: str = "recruiting") -> None:
        now = "2026-08-13T00:00:00+00:00"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO trials(
                    registration_no,detail_id,detail_url,public_title,professional_title,
                    indication_raw,drug_name_raw,recruitment_status_normalized,
                    first_seen_at,last_seen_at,content_hash,parser_version,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.registration_no,record.detail_id,record.detail_url,record.title,record.title,
                    "肺癌","示例药物",status,now,now,record.registration_no,"test",now,now,
                ),
            )

    def parser(self, content: str) -> list[Record]:
        page = int(content.split("PAGE:", 1)[1].split("<", 1)[0])
        return self.records_by_page[page]

    def service(self, worker: Worker) -> DailyIncrementalSync:
        return DailyIncrementalSync(
            self.connection,
            self.root / "raw",
            worker,
            self.parser,
            policy=DailySyncPolicy(initial_pages=5, max_pages=7, stop_after_no_new_pages=2),
            request_interval_seconds=0,
        )

    def test_page_five_new_ctr_extends_until_two_pages_have_no_new_ctr(self):
        for page, records in self.records_by_page.items():
            for record in records:
                if not (page == 5 and record is records[0]):
                    self.seed(record)
        worker = Worker(7)
        service = self.service(worker)
        run_id = service.start_or_resume(date(2026, 8, 14))
        result = service.discover(run_id)
        self.assertEqual(worker.calls, [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(result["new_ctr"], 1)
        queued = self.connection.execute(
            "SELECT registration_no,discovery_reason FROM sync_items WHERE sync_run_id=?",
            (run_id,),
        ).fetchall()
        self.assertEqual([(row[0], row[1]) for row in queued], [("CTR20260081", "new_ctr")])

    def test_unchanged_fifth_page_stops_without_scanning_extension(self):
        for records in self.records_by_page.values():
            for record in records:
                self.seed(record)
        worker = Worker(7)
        service = self.service(worker)
        run_id = service.start_or_resume(date(2026, 8, 14))
        service.discover(run_id)
        self.assertEqual(worker.calls, [1, 2, 3, 4, 5])

    def test_default_daily_policy_always_scans_twenty_pages(self):
        for records in self.records_by_page.values():
            for record in records:
                self.seed(record)
        worker = Worker(20)
        service = DailyIncrementalSync(
            self.connection,
            self.root / "raw",
            worker,
            self.parser,
            policy=DailySyncPolicy(),
            request_interval_seconds=0,
        )
        run_id = service.start_or_resume(date(2026, 8, 14))
        result = service.discover(run_id)
        self.assertEqual(worker.calls, list(range(1, 21)))
        self.assertEqual(result["new_ctr"], 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM catalog_page_scans WHERE sync_run_id=?",
                (run_id,),
            ).fetchone()[0],
            20,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM trials WHERE last_catalog_seen_at IS NOT NULL"
            ).fetchone()[0],
            400,
        )

    def test_changed_status_and_detail_id_are_audited(self):
        for records in self.records_by_page.values():
            for record in records:
                self.seed(record)
        changed = self.records_by_page[0 + 1][0]
        self.records_by_page[1][0] = Record(
            registration_no=changed.registration_no,
            detail_url="https://example.test/detail?id=" + "f" * 32,
            detail_id="f" * 32,
            description="进行中 尚未招募 | 示例药物 | 肺癌",
        )
        service = self.service(Worker(7))
        run_id = service.start_or_resume(date(2026, 8, 14))
        service.discover(run_id)
        reason = self.connection.execute(
            "SELECT discovery_reason FROM sync_items WHERE sync_run_id=? AND registration_no=?",
            (run_id, changed.registration_no),
        ).fetchone()[0]
        self.assertEqual(reason, "detail_id_changed,recruitment_status_changed")

    def test_same_ctr_with_replaced_url_keeps_one_current_run_item(self):
        service = self.service(Worker(7))
        run_id = service.start_or_resume(date(2026, 8, 14))
        original = self.records_by_page[1][0]
        replacement = Record(
            registration_no=original.registration_no,
            detail_url="https://example.test/detail?id=" + "f" * 32,
            detail_id="f" * 32,
        )
        service._enqueue(run_id, original, 1, ["new_ctr"])
        service._enqueue(run_id, replacement, 2, ["detail_id_changed"])
        rows = self.connection.execute(
            "SELECT source_url,discovered_page,discovery_reason FROM sync_items "
            "WHERE sync_run_id=? AND registration_no=?",
            (run_id, original.registration_no),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["source_url"].endswith("f" * 32))
        self.assertEqual(
            (rows[0]["discovered_page"], rows[0]["discovery_reason"]),
            (2, "detail_id_changed"),
        )

    def test_same_day_run_resumes_and_completed_run_is_idempotent(self):
        service = self.service(Worker(7))
        run_id = service.start_or_resume(date(2026, 8, 14))
        self.assertEqual(service.start_or_resume(date(2026, 8, 14)), run_id)
        with self.connection:
            self.connection.execute("UPDATE sync_runs SET status='success' WHERE id=?", (run_id,))
        self.assertEqual(service.start_or_resume(date(2026, 8, 14)), run_id)

    def test_process_lock_rejects_second_writer(self):
        lock_path = self.root / "run" / "sync.lock"
        with ExclusiveSyncLock(lock_path):
            with self.assertRaises(SyncAlreadyRunning):
                with ExclusiveSyncLock(lock_path):
                    pass

    def test_schema_migration_adds_daily_audit_columns(self):
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(sync_items)")
        }
        versions = {
            row[0] for row in self.connection.execute("SELECT version FROM schema_migrations")
        }
        self.assertTrue({"discovery_reason", "list_signature"}.issubset(columns))
        self.assertTrue({
            "processing_outcome", "before_version_id", "after_version_id",
            "old_detail_id", "new_detail_id", "changed_field_count", "review_required",
        }.issubset(columns))
        self.assertIn(8, versions)

    def test_compact_catalog_value_matches_annotated_detail_value(self):
        self.assertTrue(_summary_equivalent("示例药物", "示例药物 曾用名:"))
        self.assertFalse(_summary_equivalent("示例药物", "另一种药物"))

    def test_previous_day_crash_is_closed_and_does_not_block_new_daily_run(self):
        service = self.service(Worker(7))
        previous = service.start_or_resume(date(2026, 8, 13))
        current = service.start_or_resume(date(2026, 8, 14))
        self.assertNotEqual(current, previous)
        old = self.connection.execute(
            "SELECT status,error_summary FROM sync_runs WHERE id=?", (previous,)
        ).fetchone()
        self.assertEqual(tuple(old), ("partial", "stale_previous_daily_recovered"))

    def test_previous_daily_failures_are_carried_forward_for_retry(self):
        service = self.service(Worker(7))
        previous = service.start_or_resume(date(2026, 8, 13))
        record = self.records_by_page[1][0]
        with self.connection:
            self.connection.execute(
                "INSERT INTO sync_items(sync_run_id,registration_no,source_url,status) "
                "VALUES (?,?,?,'failed')",
                (previous, record.registration_no, record.detail_url),
            )
            self.connection.execute(
                "UPDATE sync_runs SET status='partial',finished_at='2026-08-13T03:00:00Z' WHERE id=?",
                (previous,),
            )
        current = service.start_or_resume(date(2026, 8, 14))
        carried = self.connection.execute(
            "SELECT status,discovery_reason FROM sync_items WHERE sync_run_id=?",
            (current,),
        ).fetchone()
        self.assertEqual(tuple(carried), ("pending", "carry_forward_retry"))

    def test_captcha_pause_requires_manual_intervention_same_day(self):
        service = self.service(Worker(7))
        run_id = service.start_or_resume(date(2026, 8, 14))
        service.mark_partial(run_id, "captcha_detected")
        self.assertEqual(service.start_or_resume(date(2026, 8, 14)), run_id)
        self.assertEqual(service.manual_intervention_reason(run_id), "captcha_detected")
        status = self.connection.execute(
            "SELECT status FROM sync_runs WHERE id=?", (run_id,)
        ).fetchone()[0]
        self.assertEqual(status, "partial")


if __name__ == "__main__":
    unittest.main()
