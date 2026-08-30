from __future__ import annotations

import gzip
import tempfile
import unittest
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cde_store import DateFilterType, SortField, TrialQuery, TrialRepository
from cde_store.database import initialize_database
from cde_store.raw_store import archive_html
from cde_store.cde_parser import ParsedCdePage
from cde_store.cde_parser import parse_cde_detail
from cde_store.sync_service import CdeSyncService
from cde_store.worker_client import WorkerResource
from scripts.scan_status_layers import status_of


def snapshot(
    registration_no: str,
    *,
    publication_date: str,
    recruitment_status: str = "进行中 招募中",
    indication: str = "肺癌",
) -> dict[str, object]:
    return {
        "registration_no": registration_no,
        "detail_id": registration_no.lower(),
        "detail_url": f"https://www.chinadrugtrials.org.cn/detail?id={registration_no}",
        "public_title": f"{indication}公开研究",
        "professional_title": f"{indication}专业研究",
        "indication_raw": indication,
        "drug_name_raw": "示例药物",
        "sponsor_name_raw": "示例申办方",
        "trial_phase_raw": "III期",
        "trial_phase_normalized": "III",
        "trial_scope_raw": "国内试验",
        "overall_status_raw": "进行中",
        "recruitment_status_raw": recruitment_status,
        "first_publication_date": publication_date,
        "source_update_date": publication_date,
        "inclusion_criteria_raw": "年龄18-75岁",
        "exclusion_criteria_raw": "严重感染",
    }


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.connection = initialize_database(root / "database.sqlite3")
        self.raw_root = root / "raw"
        self.repository = TrialRepository(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    def test_schema_contains_audit_tables_and_fts(self) -> None:
        names = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        self.assertTrue(
            {"trials", "trial_versions", "trial_changes", "raw_documents", "sync_runs", "trial_search_fts"}.issubset(names)
        )

    def test_result_status_labels_are_parsed_in_safe_order(self) -> None:
        self.assertEqual(status_of("进行中 尚未招募 | 药物"), "尚未招募")
        self.assertEqual(status_of("进行中 招募中 | 药物"), "招募中")
        self.assertEqual(status_of("招募完成 | 药物"), "招募完成")
        self.assertEqual(status_of("暂停 | 药物"), "暂停")

    def test_catalog_resource_omits_query_term(self) -> None:
        self.assertEqual(
            WorkerResource(kind="catalog_page", page=101).to_dict(),
            {"kind": "catalog_page", "page": 101},
        )

    def test_sync_service_accepts_official_p_suffixed_detail_id(self) -> None:
        url = "https://www.chinadrugtrials.org.cn/detail?id=2457661b7c934cbbb45a6bfe91a97cfc_p"
        self.assertEqual(
            CdeSyncService._detail_id(url),
            "2457661b7c934cbbb45a6bfe91a97cfc_p",
        )

    def test_sync_service_accepts_official_legacy_numeric_detail_id(self) -> None:
        url = "https://www.chinadrugtrials.org.cn/detail?id=2128_p"
        self.assertEqual(CdeSyncService._detail_id(url), "2128_p")

    def test_raw_html_is_gzipped_and_deduplicated(self) -> None:
        first = archive_html(
            self.connection,
            self.raw_root,
            source_type="detail_page",
            source_url="https://example.test/trial",
            content="<html>证据</html>",
            registration_no="CTR20260001",
            fetched_at="2026-08-12T00:00:00+00:00",
        )
        second = archive_html(
            self.connection,
            self.raw_root,
            source_type="detail_page",
            source_url="https://example.test/trial",
            content="<html>证据</html>",
            registration_no="CTR20260001",
            fetched_at="2026-08-12T01:00:00+00:00",
        )
        self.assertFalse(first.deduplicated)
        self.assertTrue(second.deduplicated)
        self.assertEqual(first.document_id, second.document_id)
        with gzip.open(first.storage_path, "rt", encoding="utf-8") as stream:
            self.assertEqual(stream.read(), "<html>证据</html>")

    def test_unchanged_snapshot_does_not_create_duplicate_version(self) -> None:
        item = snapshot("CTR20260001", publication_date="2026-08-01")
        first = self.repository.upsert_snapshot(item, raw_document_id=None, observed_at="2026-08-12T00:00:00+00:00")
        second = self.repository.upsert_snapshot(item, raw_document_id=None, observed_at="2026-08-13T00:00:00+00:00")
        self.assertEqual(first["change"], "created")
        self.assertEqual(second["change"], "unchanged")
        count = self.connection.execute("SELECT COUNT(*) FROM trial_versions").fetchone()[0]
        self.assertEqual(count, 1)
        observations = self.connection.execute(
            "SELECT observation_type FROM trial_observations ORDER BY id"
        ).fetchall()
        self.assertEqual([row[0] for row in observations], ["created", "unchanged"])

    def test_a_b_a_restores_existing_version_and_records_timeline(self) -> None:
        first = snapshot("CTR20260020", publication_date="2026-08-01")
        a = self.repository.upsert_snapshot(
            first, raw_document_id=None, observed_at="2026-08-12T00:00:00+00:00"
        )
        changed = dict(first)
        changed["inclusion_criteria_raw"] = "年龄18-65岁"
        b = self.repository.upsert_snapshot(
            changed, raw_document_id=None, observed_at="2026-08-13T00:00:00+00:00"
        )
        restored = self.repository.upsert_snapshot(
            first, raw_document_id=None, observed_at="2026-08-14T00:00:00+00:00"
        )

        self.assertEqual(a["version_id"], restored["version_id"])
        self.assertNotEqual(a["version_id"], b["version_id"])
        self.assertTrue(restored["restored_version"])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM trial_versions WHERE trial_id=?", (a["trial_id"],)
            ).fetchone()[0],
            2,
        )
        timeline = self.connection.execute(
            "SELECT observation_type,trial_version_id FROM trial_observations "
            "WHERE trial_id=? ORDER BY observed_at",
            (a["trial_id"],),
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in timeline],
            [("created", a["version_id"]), ("updated", b["version_id"]),
             ("restored", a["version_id"])],
        )

    def test_technical_detail_change_updates_locator_without_business_version(self) -> None:
        original = snapshot("CTR20260019", publication_date="2026-08-01")
        original["detail_id"] = "a" * 32
        original["detail_url"] = "https://example.test/detail?id=" + "a" * 32
        first = self.repository.upsert_snapshot(original, raw_document_id=None)
        moved = dict(original)
        moved["detail_id"] = "b" * 32
        moved["detail_url"] = "https://example.test/detail?id=" + "b" * 32
        second = self.repository.upsert_snapshot(moved, raw_document_id=None)
        stored = self.connection.execute(
            "SELECT detail_id,detail_url FROM trials WHERE registration_no=?",
            ("CTR20260019",),
        ).fetchone()
        self.assertEqual(first["change"], "created")
        self.assertEqual(second["change"], "unchanged")
        self.assertTrue(second["technical_change"])
        self.assertEqual(stored["detail_id"], "b" * 32)
        self.assertTrue(stored["detail_url"].endswith("b" * 32))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM trial_versions WHERE trial_id=?",
                (second["trial_id"],),
            ).fetchone()[0],
            1,
        )

    def test_status_change_creates_version_and_high_importance_change(self) -> None:
        original = snapshot("CTR20260002", publication_date="2026-07-01")
        self.repository.upsert_snapshot(original, raw_document_id=None, observed_at="2026-08-12T00:00:00+00:00")
        changed = dict(original)
        changed["recruitment_status_raw"] = "进行中 招募完成"
        result = self.repository.upsert_snapshot(changed, raw_document_id=None, observed_at="2026-08-13T00:00:00+00:00")
        self.assertEqual(result["change"], "updated")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM trial_versions").fetchone()[0], 2)
        change = self.connection.execute(
            "SELECT change_type, importance FROM trial_changes WHERE field_name = 'recruitment_status_normalized'"
        ).fetchone()
        self.assertEqual(tuple(change), ("status", "high"))

    def test_status_change_does_not_falsely_report_unchanged_criteria(self) -> None:
        original = snapshot("CTR20260012", publication_date="2026-07-01")
        self.repository.upsert_snapshot(original, raw_document_id=None)
        changed = dict(original)
        changed["recruitment_status_raw"] = "进行中 招募完成"
        self.repository.upsert_snapshot(changed, raw_document_id=None)
        fields = {
            row[0]
            for row in self.connection.execute("SELECT field_name FROM trial_changes")
        }
        self.assertNotIn("inclusion_criteria_raw", fields)
        self.assertNotIn("exclusion_criteria_raw", fields)

    def test_real_criteria_change_is_audited(self) -> None:
        original = snapshot("CTR20260013", publication_date="2026-07-01")
        self.repository.upsert_snapshot(original, raw_document_id=None)
        changed = dict(original)
        changed["inclusion_criteria_raw"] = "年龄18-65岁"
        self.repository.upsert_snapshot(changed, raw_document_id=None)
        change = self.connection.execute(
            "SELECT old_value, new_value, importance FROM trial_changes "
            "WHERE field_name = 'inclusion_criteria_raw'"
        ).fetchone()
        self.assertEqual(tuple(change), ("年龄18-75岁", "年龄18-65岁", "high"))

    def test_recent_three_months_are_filtered_and_sorted_by_sql(self) -> None:
        self.repository.upsert_snapshot(snapshot("CTR20260003", publication_date="2026-06-01"), raw_document_id=None)
        self.repository.upsert_snapshot(snapshot("CTR20260004", publication_date="2026-08-01"), raw_document_id=None)
        self.repository.upsert_snapshot(snapshot("CTR20250005", publication_date="2025-01-01"), raw_document_id=None)
        results = self.repository.search(
            TrialQuery(
                date_filter_type=DateFilterType.FIRST_PUBLICATION,
                date_from=date(2026, 5, 12),
                date_to=date(2026, 8, 12),
                sort_by=SortField.FIRST_PUBLICATION_DATE,
                sort_order="desc",
            )
        )
        self.assertEqual([item["registration_no"] for item in results], ["CTR20260004", "CTR20260003"])

    def test_fts_search_and_restricted_fields(self) -> None:
        self.repository.upsert_snapshot(snapshot("CTR20260006", publication_date="2026-08-01", indication="系统性红斑狼疮"), raw_document_id=None)
        public = self.repository.search(TrialQuery(text="系统性红斑狼疮"))
        restricted = self.repository.search(TrialQuery(text="系统性红斑狼疮", include_restricted_fields=True))
        self.assertEqual(public[0]["registration_no"], "CTR20260006")
        self.assertNotIn("sponsor_name_raw", public[0])
        self.assertEqual(restricted[0]["sponsor_name_raw"], "示例申办方")

    def test_chinese_substring_search_handles_long_and_two_character_terms(self) -> None:
        item = snapshot(
            "CTR20260014",
            publication_date="2026-08-01",
            indication="局部晚期或转移性非小细胞肺癌患者",
        )
        self.repository.upsert_snapshot(item, raw_document_id=None)
        self.assertEqual(
            self.repository.search(TrialQuery(text="非小细胞肺癌"))[0]["registration_no"],
            "CTR20260014",
        )
        self.assertEqual(
            self.repository.search(TrialQuery(text="肺癌"))[0]["registration_no"],
            "CTR20260014",
        )

    def test_fts_operator_and_unclosed_quote_are_treated_as_literal_text(self) -> None:
        self.repository.upsert_snapshot(
            snapshot("CTR20260015", publication_date="2026-08-01", indication="肺癌 OR SLE"),
            raw_document_id=None,
        )
        self.assertEqual(
            self.repository.search(TrialQuery(text="肺癌 OR SLE"))[0]["registration_no"],
            "CTR20260015",
        )
        self.assertEqual(self.repository.search(TrialQuery(text='未闭合"引号')), [])

    def test_site_contact_and_parties_are_stored_but_not_in_public_results(self) -> None:
        self.repository.upsert_snapshot(
            snapshot("CTR20260016", publication_date="2026-08-01"),
            raw_document_id=None,
            sites=[{
                "site_name_raw": "示例研究中心",
                "city": "上海",
                "principal_investigator": "示例研究者",
                "contact_raw": "公开联系人",
            }],
            parties=[{"role": "sponsor", "name_raw": "示例主体"}],
        )
        public = self.repository.search(TrialQuery(text="肺癌"))[0]
        self.assertFalse({"contact_raw", "principal_investigator", "name_raw"}.intersection(public))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM trial_sites").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM trial_parties").fetchone()[0], 1)
        default_detail = self.repository.get_trial("CTR20260016")
        restricted_detail = self.repository.get_trial(
            "CTR20260016", include_restricted_fields=True
        )
        self.assertNotIn("sites", default_detail)
        self.assertNotIn("parties", default_detail)
        self.assertEqual(restricted_detail["sites"][0]["city"], "上海")
        self.assertEqual(restricted_detail["sites"][0]["contact_raw"], "公开联系人")
        self.assertEqual(restricted_detail["parties"][0]["name_raw"], "示例主体")

    def test_missing_source_dates_remain_null(self) -> None:
        item = snapshot("CTR20260017", publication_date="2026-08-01")
        item["source_update_date"] = None
        self.repository.upsert_snapshot(item, raw_document_id=None)
        stored = self.connection.execute(
            "SELECT source_update_date, status_changed_at FROM trials WHERE registration_no = ?",
            ("CTR20260017",),
        ).fetchone()
        self.assertIsNone(stored["source_update_date"])
        self.assertIsNone(stored["status_changed_at"])

    def test_cde_parser_extracts_child_tables_and_keeps_contacts_restricted(self) -> None:
        content = """
        <html><table>
          <tr><th>登记号</th><td>CTR20260018</td><th>试验状态</th><td>进行中</td></tr>
          <tr><th>申请人名称</th><td>示例申请人</td><th>申请人联系人</th><td>联系人甲</td></tr>
          <tr><th>首次公示信息日期</th><td>2026-08-01</td></tr>
          <tr><th>适应症</th><td>肺癌</td><th>试验通俗题目</th><td>公开研究</td></tr>
        </table>
        <table>
          <tr><th>入选标准</th><td><table>
            <tr><td>1</td><td>年龄18岁以上</td></tr>
            <tr><td>2</td><td>确诊肺癌</td></tr>
          </table></td></tr>
          <tr><th>排除标准</th><td><table>
            <tr><td>1</td><td>严重感染</td></tr>
          </table></td></tr>
        </table>
        <table>
          <tr><th>序号</th><th>机构名称</th><th>主要研究者</th><th>国家或地区</th><th>省（州）</th><th>城市</th></tr>
          <tr><td>1</td><td>示例中心</td><td>研究者乙</td><td>中国</td><td>上海</td><td>上海</td></tr>
        </table>
        <table>
          <tr><th>联系人姓名</th><td>联系人丙</td><th>联系人手机号</th><td>13000000000</td></tr>
          <tr><th>联系人Email</th><td>public@example.test</td><th>单位名称</th><td>示例单位</td></tr>
        </table></html>
        """
        parsed = parse_cde_detail(
            content,
            "https://www.chinadrugtrials.org.cn/detail?id=00000000000000000000000000000018",
            "招募中",
        )
        self.assertEqual(len(parsed.criteria), 3)
        self.assertEqual(parsed.criteria[0]["raw_text"], "年龄18岁以上")
        self.assertEqual(parsed.sites[0]["city"], "上海")
        self.assertEqual(parsed.parties[0]["name_raw"], "示例申请人")
        self.assertEqual(len(parsed.contacts), 2)
        result = self.repository.upsert_snapshot(
            {**parsed.snapshot, "detail_id": "0" * 30 + "18"},
            raw_document_id=None,
            criteria=parsed.criteria,
            sites=parsed.sites,
            parties=parsed.parties,
            contacts=parsed.contacts,
        )
        public = self.repository.get_trial("CTR20260018")
        restricted = self.repository.get_trial("CTR20260018", include_restricted_fields=True)
        self.assertNotIn("contacts", public)
        self.assertEqual(restricted["contacts"][1]["email_raw"], "public@example.test")

    def test_existing_database_receives_additive_sync_migration(self) -> None:
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(sync_items)")
        }
        versions = {
            row[0] for row in self.connection.execute("SELECT version FROM schema_migrations")
        }
        self.assertIn("recruitment_status_raw", columns)
        self.assertIn("processing_outcome", columns)
        self.assertIn(8, versions)

    def test_sync_schema_supports_policy_completion_and_monthly_audit(self) -> None:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO sync_runs(run_type,started_at,status) "
                "VALUES ('monthly_audit','2026-08-13T00:00:00Z','policy_complete')"
            )
            self.connection.execute(
                "INSERT INTO sync_items(sync_run_id,source_url,status) "
                "VALUES (?,?,'skipped_policy')",
                (cursor.lastrowid, "https://example.test/skipped"),
            )
        run = self.connection.execute(
            "SELECT records_skipped,status FROM sync_runs WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()
        self.assertEqual(tuple(run), (0, "policy_complete"))
        self.assertIn(
            6,
            {row[0] for row in self.connection.execute("SELECT version FROM schema_migrations")},
        )


@dataclass(frozen=True)
class FakeRecord:
    registration_no: str
    detail_url: str
    detail_id: str
    description: str = "招募中 | 示例药物 | III期"


@dataclass(frozen=True)
class FakeTrial:
    registration_no: str
    status: str | None
    recruitment_status: str | None
    first_publication_date: str | None
    sponsor_name: str | None
    drug_name: str | None
    indication: str | None
    public_title: str | None
    professional_title: str | None
    trial_phase: str | None
    trial_scope: str | None
    inclusion_criteria: str | None
    exclusion_criteria: str | None
    detail_url: str
    raw_text: str


class FakeWorker:
    def __init__(self, records: list[FakeRecord], failed_detail_ids: set[str] | None = None):
        self.records = records
        self.failed_detail_ids = failed_detail_ids or set()
        self.calls: list[list[object]] = []

    def fetch(self, job_id, request_id, resources):
        self.calls.append(resources)
        results = []
        for resource in resources:
            if resource.kind == "result_page":
                results.append({
                    "status": "succeeded",
                    "content": "RESULTS",
                    "source_url": "https://www.chinadrugtrials.org.cn/results",
                    "retrieved_at": "2026-08-12T00:00:00+00:00",
                })
            elif resource.detail_id in self.failed_detail_ids:
                results.append({
                    "status": "failed",
                    "content": None,
                    "source_url": f"https://www.chinadrugtrials.org.cn/detail?id={resource.detail_id}",
                    "retrieved_at": "2026-08-12T00:00:00+00:00",
                    "error_code": "captcha_detected",
                    "error_message": "captcha",
                })
            else:
                results.append({
                    "status": "succeeded",
                    "content": f"DETAIL:{resource.detail_id}",
                    "source_url": f"https://www.chinadrugtrials.org.cn/detail?id={resource.detail_id}",
                    "retrieved_at": "2026-08-12T00:00:00+00:00",
                })
        return results


class SyncServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.connection = initialize_database(root / "database.sqlite3")
        self.raw_root = root / "raw"
        self.records = [
            FakeRecord(
                registration_no=f"CTR2026{index:04d}",
                detail_url=f"https://www.chinadrugtrials.org.cn/detail?id={index:032x}",
                detail_id=f"{index:032x}",
            )
            for index in range(1, 4)
        ]

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    def make_service(
        self,
        worker: FakeWorker,
        max_attempts: int = 3,
        *,
        request_interval_seconds: float = 0,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    ) -> CdeSyncService:
        def parse_results(content: str):
            self.assertEqual(content, "RESULTS")
            return self.records

        def parse_trial(content: str, url: str, recruitment_status: str | None):
            detail_id = content.split(":", 1)[1]
            index = int(detail_id, 16)
            trial = FakeTrial(
                registration_no=f"CTR2026{index:04d}",
                status="进行中",
                recruitment_status=None,
                first_publication_date="2026-08-01",
                sponsor_name="示例申办方",
                drug_name="示例药物",
                indication="肺癌",
                public_title="公开研究",
                professional_title="专业研究",
                trial_phase="III期",
                trial_scope="国内试验",
                inclusion_criteria="18-75岁",
                exclusion_criteria="严重感染",
                detail_url=url,
                raw_text="raw",
            )
            return ParsedCdePage(
                snapshot={
                    "registration_no": trial.registration_no,
                    "detail_url": trial.detail_url,
                    "public_title": trial.public_title,
                    "professional_title": trial.professional_title,
                    "indication_raw": trial.indication,
                    "drug_name_raw": trial.drug_name,
                    "sponsor_name_raw": trial.sponsor_name,
                    "trial_phase_raw": trial.trial_phase,
                    "trial_scope_raw": trial.trial_scope,
                    "overall_status_raw": trial.status,
                    "recruitment_status_raw": recruitment_status,
                    "first_publication_date": trial.first_publication_date,
                    "source_update_date": None,
                    "inclusion_criteria_raw": trial.inclusion_criteria,
                    "exclusion_criteria_raw": trial.exclusion_criteria,
                },
                criteria=[],
                sites=[],
                parties=[],
                contacts=[],
            )

        return CdeSyncService(
            self.connection,
            self.raw_root,
            worker,
            parse_results,
            parse_trial,
            max_attempts=max_attempts,
            request_interval_seconds=request_interval_seconds,
            sleep=sleep,
            monotonic=monotonic,
        )

    def test_import_is_bounded_audited_and_resumable(self) -> None:
        worker = FakeWorker(self.records)
        service = self.make_service(worker)
        run_id = service.start_or_resume(query_term="肺癌", pages=2, target=2)
        self.assertEqual(service.discover(run_id, query_term="肺癌", pages=2, target=2), 2)
        self.assertEqual(
            service.process(run_id, batch_size=1),
            {"success": 2, "failed": 0, "pending": 0, "processed_this_call": 2},
        )
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM trials").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM raw_documents").fetchone()[0], 3)
        outcomes = {
            row[0]
            for row in self.connection.execute(
                "SELECT processing_outcome FROM sync_items WHERE sync_run_id=?", (run_id,)
            )
        }
        self.assertEqual(outcomes, {"created"})
        resumed = service.start_or_resume(query_term="肺癌", pages=2, target=2)
        self.assertNotEqual(resumed, run_id, "successful runs must not be resumed")

    def test_scanned_samples_can_be_enqueued_idempotently(self) -> None:
        worker = FakeWorker(self.records)
        service = self.make_service(worker)
        run_id = service.start_or_resume(query_term="status-layer:招募中", pages=0, target=2)
        samples = [{
            "registration_no": record.registration_no,
            "detail_url": record.detail_url,
            "recruitment_status_raw": record.description.split(" | ", 1)[0],
        } for record in self.records[:2]]
        self.assertEqual(service.enqueue_samples(run_id, samples), 2)
        self.assertEqual(service.enqueue_samples(run_id, samples), 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT records_discovered FROM sync_runs WHERE id = ?", (run_id,)
            ).fetchone()[0],
            2,
        )

    def test_worker_requests_are_throttled_across_single_resource_batches(self) -> None:
        worker = FakeWorker(self.records)
        clock = [100.0]
        sleeps: list[float] = []

        def monotonic() -> float:
            return clock[0]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        service = self.make_service(
            worker,
            request_interval_seconds=2.5,
            sleep=sleep,
            monotonic=monotonic,
        )
        run_id = service.start_or_resume(query_term="肺癌", pages=1, target=2)
        service.discover(run_id, query_term="肺癌", pages=1, target=2)
        service.process(run_id, batch_size=1)
        self.assertEqual(sleeps, [2.5, 2.5])

    def test_process_limit_leaves_a_resumable_checkpoint(self) -> None:
        worker = FakeWorker(self.records)
        service = self.make_service(worker)
        run_id = service.start_or_resume(query_term="试验", pages=1, target=3)
        service.discover(run_id, query_term="试验", pages=1, target=3)
        first = service.process(run_id, batch_size=2, max_items=2)
        self.assertEqual(first["processed_this_call"], 2)
        self.assertEqual(first["success"], 2)
        self.assertEqual(first["pending"], 1)
        resumed = service.start_or_resume(query_term="试验", pages=1, target=3)
        self.assertEqual(resumed, run_id)
        second = service.process(run_id, batch_size=2, max_items=2)
        self.assertEqual(second["processed_this_call"], 1)
        self.assertEqual(second["success"], 3)
        self.assertEqual(second["pending"], 0)

    def test_discovery_skips_completed_pages_and_full_target(self) -> None:
        worker = FakeWorker(self.records)
        service = self.make_service(worker)
        run_id = service.start_or_resume(query_term="试验", pages=3, target=3)
        self.assertEqual(service.discover(run_id, query_term="试验", pages=3, target=3), 3)
        call_count = len(worker.calls)
        self.assertEqual(service.discover(run_id, query_term="试验", pages=3, target=3), 0)
        self.assertEqual(len(worker.calls), call_count)

    def test_failed_item_records_retry_checkpoint_without_losing_success(self) -> None:
        failed_id = self.records[1].detail_id
        worker = FakeWorker(self.records, {failed_id})
        service = self.make_service(worker)
        run_id = service.start_or_resume(query_term="肺癌", pages=1, target=3)
        service.discover(run_id, query_term="肺癌", pages=1, target=3)
        summary = service.process(run_id, batch_size=3)
        self.assertEqual(
            summary,
            {"success": 2, "failed": 0, "pending": 1, "processed_this_call": 3},
        )
        retry = self.connection.execute(
            "SELECT status, attempt_count, last_error_code, next_retry_at FROM sync_items "
            "WHERE registration_no = ?",
            (self.records[1].registration_no,),
        ).fetchone()
        self.assertEqual((retry["status"], retry["attempt_count"], retry["last_error_code"]), ("retry", 1, "captcha_detected"))
        self.assertIsNotNone(retry["next_retry_at"])

    def test_final_single_item_failure_does_not_block_later_items(self) -> None:
        failed_id = self.records[0].detail_id
        worker = FakeWorker(self.records, {failed_id})
        service = self.make_service(worker, max_attempts=1)
        run_id = service.start_or_resume(query_term="全量", pages=1, target=3)
        service.discover(run_id, query_term="全量", pages=1, target=3)
        summary = service.process(run_id, batch_size=3)
        self.assertEqual(
            summary,
            {"success": 2, "failed": 1, "pending": 0, "processed_this_call": 3},
        )
        states = {
            row["registration_no"]: row["status"]
            for row in self.connection.execute(
                "SELECT registration_no, status FROM sync_items WHERE sync_run_id=?",
                (run_id,),
            )
        }
        self.assertEqual(states[self.records[0].registration_no], "failed")
        self.assertEqual(states[self.records[1].registration_no], "success")
        self.assertEqual(states[self.records[2].registration_no], "success")

    def test_invalid_detail_refreshes_changed_id_by_exact_ctr_search(self) -> None:
        old = self.records[0]
        replacement = FakeRecord(
            registration_no=old.registration_no,
            detail_url="https://www.chinadrugtrials.org.cn/detail?id=0000000000000000000000000000000a",
            detail_id="0000000000000000000000000000000a",
        )

        class RefreshWorker(FakeWorker):
            def fetch(inner_self, job_id, request_id, resources):
                resource = resources[0]
                inner_self.calls.append(resources)
                if resource.kind == "result_page":
                    return [{
                        "status": "succeeded", "content": "REFRESH_RESULTS",
                        "source_url": "https://www.chinadrugtrials.org.cn/results",
                        "retrieved_at": "2026-08-13T00:00:00+00:00",
                    }]
                if resource.detail_id == old.detail_id:
                    return [{
                        "status": "failed", "content": None,
                        "source_url": old.detail_url,
                        "retrieved_at": "2026-08-13T00:00:00+00:00",
                        "error_code": "cde_detail_invalid",
                        "error_message": "stale id",
                    }]
                return [{
                    "status": "succeeded", "content": f"DETAIL:{resource.detail_id}",
                    "source_url": replacement.detail_url,
                    "retrieved_at": "2026-08-13T00:00:00+00:00",
                }]

        worker = RefreshWorker([replacement])
        service = self.make_service(worker)
        # Enqueue the obsolete URL directly; exact search will return replacement.
        run_id = service.start_or_resume(query_term="refresh", pages=0, target=1)
        service.enqueue_samples(run_id, [{
            "registration_no": old.registration_no,
            "detail_url": old.detail_url,
            "recruitment_status_raw": "招募中",
        }])
        service.parse_result_page = lambda content: [replacement]

        result = service.process(run_id, batch_size=1)

        self.assertEqual(result["success"], 1)
        stored = self.connection.execute(
            "SELECT source_url, status, recruitment_status_raw FROM sync_items WHERE sync_run_id=?",
            (run_id,),
        ).fetchone()
        self.assertEqual(stored["source_url"], replacement.detail_url)
        self.assertEqual(stored["status"], "success")
        self.assertEqual(stored["recruitment_status_raw"], "招募中")


if __name__ == "__main__":
    unittest.main()
