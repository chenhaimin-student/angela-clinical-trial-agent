from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cde-demo"))

from cde_demo.models import PatientProfile
from cde_demo.service import CdeLookupService


ELIGIBLE_ID = "a" * 32
INELIGIBLE_ID = "b" * 32


class FakeClient:
    def __init__(self) -> None:
        self.detail_ids: list[str] = []

    def fetch(self, _job_id: str, _request_id: str, resources):
        resource = resources[0]
        if resource.kind == "result_page":
            return [{"content": self._result_page()}]
        self.detail_ids.extend(item.detail_id for item in resources)
        return [
            {
                "content": self._detail_page("CTR20260001"),
                "source_url": "https://www.chinadrugtrials.org.cn/clinicaltrials.searchlistdetail.dhtml?id=" + item.detail_id,
            }
            for item in resources
        ]

    @staticmethod
    def _result_page() -> str:
        return (
            "<table class='searchTable'>"
            "<tr><td>1</td><td><a id='" + ELIGIBLE_ID + "'>CTR20260001</a></td>"
            "<td>\u8fdb\u884c\u4e2d \u62db\u52df\u4e2d</td><td>\u793a\u4f8b\u836f</td><td>\u80ba\u764c</td><td>\u793a\u4f8b\u7814\u7a76</td></tr>"
            "<tr><td>2</td><td><a id='" + INELIGIBLE_ID + "'>CTR20260002</a></td>"
            "<td>\u5df2\u5b8c\u6210</td><td>\u793a\u4f8b\u836f</td><td>\u80ba\u764c</td><td>\u5df2\u5b8c\u6210\u7814\u7a76</td></tr>"
            "</table>"
        )

    @staticmethod
    def _detail_page(registration_no: str) -> str:
        return (
            "<table>"
            f"<tr><th>\u767b\u8bb0\u53f7</th><td>{registration_no}</td></tr>"
            "<tr><th>\u7533\u8bf7\u4eba\u540d\u79f0</th><td>\u793a\u4f8b\u5236\u836f</td></tr>"
            "<tr><th>\u9002\u5e94\u75c7</th><td>\u80ba\u764c</td></tr>"
            "<tr><th>\u5165\u9009\u6807\u51c6</th><td>\u5e74\u9f84 18-75 \u5c81</td></tr>"
            "</table>"
        )


class CdeLookupServiceTests(unittest.TestCase):
    def test_filters_non_recruiting_records_before_detail_fetch(self) -> None:
        client = FakeClient()
        result = CdeLookupService(client).lookup(
            profile=PatientProfile(condition_terms=("\u80ba\u764c",)),
            query_terms=("\u80ba\u764c",),
            max_results=1,
            search_pages=1,
            candidate_limit=2,
        )

        self.assertEqual(client.detail_ids, [ELIGIBLE_ID])
        self.assertEqual(len(result.matches), 1)


if __name__ == "__main__":
    unittest.main()
