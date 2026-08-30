from __future__ import annotations

import unittest

from cde_demo.matching import preliminary_match
from cde_demo.models import CdeTrial, InputPrivacyError, PatientProfile, reject_direct_identifiers
from cde_demo.parser import parse_result_page, parse_rss, parse_trial_html


class CdeDemoTests(unittest.TestCase):
    def test_rejects_direct_identifier(self) -> None:
        with self.assertRaises(InputPrivacyError):
            reject_direct_identifiers({"patient": {"姓名": "不应出现"}})

    def test_parses_rss_detail_id(self) -> None:
        records = parse_rss(
            "<rss><channel><item><guid>CTR20260001</guid><title>肺癌试验</title>"
            "<description>说明</description><link>https://example.test/detail?id=abc123</link>"
            "</item></channel></rss>"
        )
        self.assertEqual(records[0].detail_id, "abc123")

    def test_parses_result_page_detail_id(self) -> None:
        records = parse_result_page(
            "<table class='searchTable'><tr class='Tab_title'><th>标题</th></tr>"
            "<tr><td>1</td><td><a id='d400e118ffc0485a8e6085578321b5f3'>CTR20240220</a></td>"
            "<td>进行中 招募中</td><td>示例药物</td><td>肺腺癌</td><td>示例研究</td></tr></table>"
        )
        self.assertEqual(records[0].registration_no, "CTR20240220")
        self.assertTrue(records[0].detail_url.endswith("d400e118ffc0485a8e6085578321b5f3"))

    def test_parses_official_result_page_detail_id_with_p_suffix(self) -> None:
        records = parse_result_page(
            "<table class='searchTable'><tr class='Tab_title'><th>标题</th></tr>"
            "<tr><td>1</td><td><a id='2457661b7c934cbbb45a6bfe91a97cfc_p'>CTR20201430</a></td>"
            "<td>进行中 尚未招募</td><td>示例药物</td><td>慢性乙型肝炎</td><td>示例研究</td></tr></table>"
        )
        self.assertEqual(records[0].detail_id, "2457661b7c934cbbb45a6bfe91a97cfc_p")
        self.assertTrue(records[0].detail_url.endswith("2457661b7c934cbbb45a6bfe91a97cfc_p"))

    def test_parses_official_legacy_numeric_detail_id(self) -> None:
        records = parse_result_page(
            "<table class='searchTable'><tr class='Tab_title'><th>标题</th></tr>"
            "<tr><td>1</td><td><a id='2128_p'>CTR20131771</a></td>"
            "<td>已完成</td><td>示例药物</td><td>示例适应症</td><td>示例研究</td></tr></table>"
        )
        self.assertEqual(records[0].detail_id, "2128_p")
        self.assertTrue(records[0].detail_url.endswith("2128_p"))

    def test_parses_detail_and_finds_preliminary_match(self) -> None:
        trial = parse_trial_html(
            "<table><tr><th>登记号</th><td>CTR20260001</td></tr>"
            "<tr><th>申请人名称</th><td>示例制药</td></tr>"
            "<tr><th>适应症</th><td>肺腺癌</td></tr>"
            "<tr><th>试验通俗题目</th><td>EGFR 突变肺腺癌研究</td></tr>"
            "<tr><th>入选标准</th><td>年龄 18-75 岁，女性受试者</td></tr>"
            "</table>",
            "https://example.test/detail?id=abc123",
        )
        profile = PatientProfile(age=56, sex="female", condition_terms=("肺腺癌",), pathology_terms=("EGFR",))
        match = preliminary_match(profile, trial)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertGreaterEqual(match.score, 7)
        self.assertIn("年龄：56 岁", match.matched_fields)

    def test_filters_known_age_conflict(self) -> None:
        trial = CdeTrial(
            registration_no="CTR20260001", status=None, recruitment_status=None, first_publication_date=None,
            sponsor_name=None, drug_name=None, indication="肺腺癌", public_title=None,
            professional_title=None, trial_phase=None, trial_scope=None,
            inclusion_criteria="年龄 18-60 岁", exclusion_criteria=None,
            detail_url="https://example.test", raw_text="肺腺癌",
        )
        self.assertIsNone(preliminary_match(PatientProfile(age=70, condition_terms=("肺腺癌",)), trial))


if __name__ == "__main__":
    unittest.main()
