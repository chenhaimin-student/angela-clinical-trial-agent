from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from cde_store.catalog_sync import CatalogSyncService, CatalogTotals, parse_catalog_totals
from cde_store.database import initialize_database
from cde_store.worker_client import WorkerClientError


@dataclass(frozen=True)
class Record:
    registration_no: str
    detail_url: str
    description: str = "进行中 招募中 | 示例药物"


def catalog_html(page: int, pages: int, records: int, token: str = "") -> str:
    return f"<html><body>当前第 {page} 页，共 {pages} 页，共 {records} 条记录 {token}</body></html>"


class CatalogWorker:
    def __init__(self, pages: dict[int, dict]):
        self.pages = pages
        self.calls = []

    def fetch(self, _job_id, _request_id, resources):
        resource = resources[0]
        self.calls.append(resource.page)
        return [self.pages[resource.page]]


class CatalogSyncTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.connection = initialize_database(root / "catalog.sqlite3")
        self.raw_root = root / "raw"

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    @staticmethod
    def result(page: int, pages: int, total: int, token: str = "") -> dict:
        return {
            "status": "succeeded",
            "content": catalog_html(page, pages, total, token or f"PAGE:{page}"),
            "source_url": f"https://example.test/catalog?page={page}",
            "retrieved_at": "2026-08-12T00:00:00+00:00",
        }

    @staticmethod
    def records(content: str) -> list[Record]:
        page = int(content.split("PAGE:", 1)[1].split("<", 1)[0])
        count = 5 if "共 25 条记录" in content and page == 2 else 20
        start = (page - 1) * 20
        return [
            Record(f"CTR{start + index:08d}", f"https://example.test/detail?id={start + index:032x}")
            for index in range(1, count + 1)
        ]

    def service(self, worker) -> CatalogSyncService:
        return CatalogSyncService(
            self.connection, self.raw_root, worker, self.records,
            request_interval_seconds=0,
        )

    def test_totals_parser_validates_page_math(self):
        self.assertEqual(
            parse_catalog_totals(catalog_html(2, 2, 25)),
            CatalogTotals(2, 2, 25),
        )
        with self.assertRaises(ValueError):
            parse_catalog_totals(catalog_html(1, 3, 25))

    def test_discovery_tracks_source_page_and_enters_details(self):
        worker = CatalogWorker({
            1: self.result(1, 2, 25),
            2: self.result(2, 2, 25),
        })
        service = self.service(worker)
        run_id = service.start_or_resume()
        summary = service.discover(run_id)
        self.assertEqual(summary["phase"], "details")
        self.assertEqual(summary["unique_records"], 25)
        row = self.connection.execute(
            "SELECT discovered_page, discovery_pass, first_discovered_at FROM sync_items "
            "WHERE sync_run_id=? ORDER BY id DESC LIMIT 1", (run_id,),
        ).fetchone()
        self.assertEqual((row[0], row[1]), (2, 1))
        self.assertTrue(row[2])

    def test_bounded_call_resumes_from_next_page(self):
        worker = CatalogWorker({
            1: self.result(1, 2, 25),
            2: self.result(2, 2, 25),
        })
        service = self.service(worker)
        run_id = service.start_or_resume()
        first = service.discover(run_id, max_pages_this_call=1)
        self.assertEqual(first["next_page"], 2)
        self.connection.execute(
            "UPDATE sync_runs SET status='partial', error_summary='disconnect' WHERE id=?", (run_id,)
        )
        self.connection.commit()
        self.assertEqual(service.start_or_resume(), run_id)
        status = self.connection.execute("SELECT status FROM sync_runs WHERE id=?", (run_id,)).fetchone()[0]
        self.assertEqual(status, "running")
        second = service.discover(run_id)
        self.assertEqual(second["phase"], "details")
        self.assertEqual(worker.calls, [1, 2])

    def test_failed_page_keeps_auditable_partial_checkpoint(self):
        worker = CatalogWorker({1: {
            "status": "failed", "content": None, "error_code": "captcha_detected",
        }})
        service = self.service(worker)
        run_id = service.start_or_resume()
        with self.assertRaises(WorkerClientError):
            service.discover(run_id)
        row = self.connection.execute(
            "SELECT status, error_summary, checkpoint_json FROM sync_runs WHERE id=?", (run_id,)
        ).fetchone()
        self.assertEqual((row[0], row[1]), ("partial", "captcha_detected"))
        self.assertIn('"next_page": 1', row[2])


if __name__ == "__main__":
    unittest.main()
