from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cde_store.database import initialize_database
from cde_store.weekly_sync import WeeklyCatalogReview
from cde_store.worker_client import WorkerClientError


@dataclass(frozen=True)
class Record:
    registration_no: str
    detail_url: str
    detail_id: str
    title: str = "公开研究"
    description: str = "进行中 招募中 | 示例药物 | 肺癌"


def catalog_html(page: int, pages: int, records: int) -> str:
    return (
        f"<html><body>当前第 {page} 页，共 {pages} 页，共 {records} 条记录"
        f" PAGE:{page}</body></html>"
    )


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
            "retrieved_at": f"2026-08-16T00:{page:02d}:00+00:00",
        }]


class WeeklySyncTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.connection = initialize_database(self.root / "weekly.sqlite3")
        self.records_by_page = {
            page: [self.record(page, index) for index in range(1, 21)]
            for page in range(1, 4)
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

    def seed(self, record: Record) -> None:
        now = "2026-08-01T00:00:00+00:00"
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
                    record.registration_no,record.detail_id,record.detail_url,
                    record.title,record.title,"肺癌","示例药物","recruiting",
                    now,now,record.registration_no,"test",now,now,
                ),
            )

    def parser(self, content: str) -> list[Record]:
        page = int(content.split("PAGE:", 1)[1].split("<", 1)[0])
        return self.records_by_page[page]

    def service(self, worker: Worker) -> WeeklyCatalogReview:
        return WeeklyCatalogReview(
            self.connection,
            self.root / "raw",
            worker,
            self.parser,
            request_interval_seconds=0,
        )

    def test_full_catalog_review_records_coverage_and_missing_candidates(self):
        new_record = self.records_by_page[2][4]
        for records in self.records_by_page.values():
            for record in records:
                if record != new_record:
                    self.seed(record)
        missing = Record(
            registration_no="CTR20250001",
            detail_url="https://example.test/detail?id=" + "f" * 32,
            detail_id="f" * 32,
        )
        self.seed(missing)
        worker = Worker(3)
        service = self.service(worker)
        run_id = service.start_or_resume(date(2026, 8, 16))
        summary = service.discover(run_id)

        self.assertEqual(worker.calls, [1, 2, 3])
        self.assertEqual(summary["records_seen"], 60)
        self.assertEqual(summary["new_ctr"], 1)
        self.assertEqual(summary["phase"], "details")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM catalog_page_scans WHERE sync_run_id=?", (run_id,)
            ).fetchone()[0],
            3,
        )
        queued = self.connection.execute(
            "SELECT registration_no,discovery_reason FROM sync_items WHERE sync_run_id=?",
            (run_id,),
        ).fetchall()
        self.assertEqual([(row[0], row[1]) for row in queued], [
            (new_record.registration_no, "new_ctr")
        ])
        self.assertEqual(
            self.connection.execute(
                "SELECT catalog_missing_count FROM trials WHERE registration_no=?",
                (missing.registration_no,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM trials WHERE last_catalog_seen_at IS NOT NULL"
            ).fetchone()[0],
            59,
        )

    def test_same_week_completed_review_is_idempotent(self):
        service = self.service(Worker(3))
        run_id = service.start_or_resume(date(2026, 8, 16))
        with self.connection:
            self.connection.execute(
                "UPDATE sync_runs SET status='success' WHERE id=?", (run_id,)
            )
        self.assertEqual(service.start_or_resume(date(2026, 8, 15)), run_id)

    def test_different_week_creates_a_new_review(self):
        service = self.service(Worker(3))
        first = service.start_or_resume(date(2026, 8, 16))
        with self.connection:
            self.connection.execute(
                "UPDATE sync_runs SET status='success' WHERE id=?", (first,)
            )
        second = service.start_or_resume(date(2026, 8, 23))
        self.assertNotEqual(first, second)

    def test_failed_page_is_recorded_for_targeted_resume(self):
        class FailedWorker(Worker):
            def fetch(inner_self, _job_id, _request_id, resources):
                page = resources[0].page
                inner_self.calls.append(page)
                if page == 2:
                    return [{
                        "status": "failed",
                        "content": None,
                        "error_code": "captcha_detected",
                    }]
                return [{
                    "status": "succeeded",
                    "content": catalog_html(page, 3, 60),
                    "source_url": f"https://example.test/catalog?page={page}",
                    "retrieved_at": f"2026-08-16T00:{page:02d}:00+00:00",
                }]

        worker = FailedWorker(3)
        service = self.service(worker)
        run_id = service.start_or_resume(date(2026, 8, 16))
        with self.assertRaises(WorkerClientError):
            service.discover(run_id)
        failed = self.connection.execute(
            """
            SELECT page_no,result_status,error_code FROM catalog_page_scans
            WHERE sync_run_id=? AND page_no=2
            """,
            (run_id,),
        ).fetchone()
        self.assertEqual(tuple(failed), (2, "failed", "captcha_detected"))
        status = self.connection.execute(
            "SELECT status,error_summary FROM sync_runs WHERE id=?", (run_id,)
        ).fetchone()
        self.assertEqual(tuple(status), ("partial", "captcha_detected"))


if __name__ == "__main__":
    unittest.main()
