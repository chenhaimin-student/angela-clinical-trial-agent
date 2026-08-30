from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from cde_store.database import initialize_database
from cde_store.local_query import (
    DiseaseConcepts,
    LocalTrialQuery,
    LocalTrialQueryService,
    age_compatibility,
    extract_trial_phases,
    sex_compatibility,
    contains_term,
    contains_biomarker,
)
from cde_store.repository import TrialRepository


def snapshot(ctr: str, *, disease: str, publication: str, status: str = "进行中 招募中", phase: str = "III期") -> dict:
    return {
        "registration_no": ctr,
        "detail_id": ctr.lower(),
        "detail_url": f"https://example.test/{ctr}",
        "public_title": f"{disease}公开研究",
        "professional_title": f"{disease}专业研究",
        "indication_raw": disease,
        "drug_name_raw": "示例药物",
        "sponsor_name_raw": "示例申办方",
        "trial_phase_raw": phase,
        "trial_scope_raw": "国内试验",
        "overall_status_raw": "进行中",
        "recruitment_status_raw": status,
        "first_publication_date": publication,
        "source_update_date": None,
        "inclusion_criteria_raw": "年龄18至65岁，男性或女性均可",
        "exclusion_criteria_raw": "严重感染",
    }


class LocalQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.connection = initialize_database(root / "db.sqlite3")
        concepts_path = root / "concepts.json"
        concepts_path.write_text(json.dumps({
            "version": 1,
            "concepts": [{
                "canonical": "系统性红斑狼疮", "synonyms": ["SLE"],
                "related": ["红斑狼疮"],
                "parent": "自身免疫病",
            }],
        }, ensure_ascii=False), encoding="utf-8")
        self.service = LocalTrialQueryService(self.connection, DiseaseConcepts(concepts_path))
        self.repository = TrialRepository(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    def add(self, ctr: str, disease: str, publication: str, *, observed: str = "2026-08-01T00:00:00+00:00", status: str = "进行中 招募中", phase: str = "III期", sites=()):
        return self.repository.upsert_snapshot(
            snapshot(ctr, disease=disease, publication=publication, status=status, phase=phase),
            raw_document_id=None, observed_at=observed,
            criteria=[
                {"criterion_type": "inclusion", "sequence_no": 1, "raw_text": "年龄18至65岁，男性或女性均可"},
                {"criterion_type": "exclusion", "sequence_no": 1, "raw_text": "严重感染"},
            ],
            sites=sites,
            parties=[{"role": "sponsor", "name_raw": "示例申办方"}],
            contacts=[{"contact_type": "applicant", "name_raw": "公开联系人"}],
        )

    def test_defaults_limit_and_total_count(self) -> None:
        for index in range(12):
            self.add(f"CTR20260{index:03d}", "系统性红斑狼疮", f"2026-08-{index + 1:02d}")
        result = self.service.search(LocalTrialQuery(disease="SLE"))
        self.assertEqual(result["total_count"], 12)
        self.assertEqual(result["returned_count"], 10)
        self.assertEqual(len(result["results"]), 10)

    def test_maximum_limit_is_twenty(self) -> None:
        with self.assertRaises(ValueError):
            LocalTrialQuery(disease="SLE", limit=21)

    def test_trial_phase_filter_supports_mixed_chinese_roman_phases(self) -> None:
        self.add("CTR20260101", "系统性红斑狼疮", "2026-08-01", phase="I期")
        self.add("CTR20260102", "系统性红斑狼疮", "2026-08-02", phase="II期")
        self.add("CTR20260103", "系统性红斑狼疮", "2026-08-03", phase="其它 其他说明:Ⅱ/Ⅲ期")
        result = self.service.search(LocalTrialQuery(
            disease="SLE", trial_phases=("II", "III"), limit=20,
        ))
        self.assertEqual(result["total_count"], 2)
        self.assertEqual(
            {item["registration_no"] for item in result["results"]},
            {"CTR20260102", "CTR20260103"},
        )
        self.assertEqual(result["query"]["trial_phases"], ["II", "III"])

    def test_phase_extractor_accepts_ascii_and_unicode_forms(self) -> None:
        self.assertEqual(extract_trial_phases("II/III期"), {"II", "III"})
        self.assertEqual(extract_trial_phases("Ⅰb/Ⅱ期"), {"I", "II"})
        self.assertEqual(extract_trial_phases("Ib/IIa期"), {"I", "II"})
        self.assertEqual(extract_trial_phases("IV期"), {"IV"})

    def test_synonym_and_related_terms_are_auditable(self) -> None:
        self.add("CTR20260001", "系统性红斑狼疮", "2026-08-01")
        result = self.service.search(LocalTrialQuery(disease="SLE"))
        evidence = result["results"][0]["match_evidence"]
        self.assertTrue(any(item["term_kind"] == "canonical" for item in evidence))
        self.assertIn("expanded_terms", result["query"])

    def test_agent_candidate_requires_database_evidence(self) -> None:
        self.add("CTR20260002", "狼疮性肾炎", "2026-08-01")
        result = self.service.search(LocalTrialQuery(
            disease="未知口语", agent_candidate_terms=("狼疮性肾炎",),
        ))
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["results"][0]["match_evidence"][0]["term_kind"], "agent_candidate")

    def test_ascii_abbreviation_uses_token_boundaries(self) -> None:
        self.assertTrue(contains_term("诊断为 SLE 患者", "SLE"))
        self.assertFalse(contains_term("single cycle treatment", "CLE"))

    def test_exclusion_only_disease_mention_is_not_a_positive_match(self) -> None:
        item = snapshot("CTR20260020", disease="健康受试者", publication="2026-08-01")
        item["exclusion_criteria_raw"] = "排除系统性红斑狼疮患者"
        self.repository.upsert_snapshot(item, raw_document_id=None)
        result = self.service.search(LocalTrialQuery(disease="SLE"))
        self.assertEqual(result["total_count"], 0)

    def test_inclusion_only_disease_mention_is_not_a_positive_match(self) -> None:
        item = snapshot("CTR20260021", disease="healthy volunteers", publication="2026-08-01")
        item["inclusion_criteria_raw"] = "Patients with SLE may be included"
        self.repository.upsert_snapshot(item, raw_document_id=None)
        result = self.service.search(LocalTrialQuery(disease="SLE"))
        self.assertEqual(result["total_count"], 0)

    def test_parent_indication_plus_specific_inclusion_is_a_positive_match(self) -> None:
        item = snapshot("CTR20260022", disease="自身免疫病", publication="2026-08-01")
        item["inclusion_criteria_raw"] = "Patients with SLE may be included"
        self.repository.upsert_snapshot(item, raw_document_id=None)
        result = self.service.search(LocalTrialQuery(disease="SLE"))
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["results"][0]["registration_no"], "CTR20260022")

    def test_compound_date_uses_any_single_business_date_but_not_last_verified(self) -> None:
        self.add("CTR20260003", "系统性红斑狼疮", "2020-01-01", observed="2026-08-01T00:00:00+00:00")
        result = self.service.search(LocalTrialQuery(
            disease="SLE", date_from=date(2026, 5, 13), date_to=date(2026, 8, 13),
        ))
        self.assertEqual(result["total_count"], 0, "last_verified_at must not satisfy recent-date filtering")

    def test_status_change_can_satisfy_recent_date(self) -> None:
        self.add("CTR20260004", "系统性红斑狼疮", "2020-01-01", observed="2026-01-01T00:00:00+00:00", status="进行中 尚未招募")
        changed = snapshot("CTR20260004", disease="系统性红斑狼疮", publication="2020-01-01", status="进行中 招募中")
        self.repository.upsert_snapshot(changed, raw_document_id=None, observed_at="2026-07-01T00:00:00+00:00")
        result = self.service.search(LocalTrialQuery(
            disease="SLE", date_from=date(2026, 5, 13), date_to=date(2026, 8, 13),
        ))
        self.assertEqual(result["total_count"], 1)

    def test_city_uses_current_research_sites(self) -> None:
        self.add("CTR20260005", "系统性红斑狼疮", "2026-08-01", sites=[{
            "site_name_raw": "示例中心", "province": "上海", "city": "上海",
        }])
        self.assertEqual(self.service.search(LocalTrialQuery(disease="SLE", city="上海"))["total_count"], 1)
        self.assertEqual(self.service.search(LocalTrialQuery(disease="SLE", city="北京"))["total_count"], 0)

    def test_city_matches_official_suffix_variants(self) -> None:
        self.add("CTR20260015", "系统性红斑狼疮", "2026-08-01", sites=[{
            "site_name_raw": "上海示例中心", "province": "上海市", "city": "上海市",
        }])
        result = self.service.search(LocalTrialQuery(disease="SLE", city="上海"))
        self.assertEqual(result["total_count"], 1)

    def test_named_site_is_a_hard_filter(self) -> None:
        self.add("CTR20260030", "哮喘", "2026-08-01", sites=[{
            "site_name_raw": "上海交通大学医学院附属瑞金医院",
            "province": "上海市", "city": "上海市",
        }])
        self.add("CTR20260031", "哮喘", "2026-08-02", sites=[{
            "site_name_raw": "上海市其他医院", "province": "上海市", "city": "上海市",
        }])
        result = self.service.search(LocalTrialQuery(
            disease="哮喘", site_names=("上海交通大学附属瑞金医院",), limit=20,
        ))
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["results"][0]["registration_no"], "CTR20260030")

    def test_province_is_a_hard_filter(self) -> None:
        self.add("CTR20260032", "肺癌", "2026-08-01", sites=[{
            "site_name_raw": "广东示例医院", "province": "广东省", "city": "广州市",
        }])
        self.add("CTR20260033", "肺癌", "2026-08-02", sites=[{
            "site_name_raw": "上海示例医院", "province": "上海市", "city": "上海市",
        }])
        result = self.service.search(LocalTrialQuery(disease="肺癌", province="广东省"))
        self.assertEqual(result["total_count"], 1)

    def test_project_code_is_not_degraded_to_gene_marker(self) -> None:
        matching = snapshot("CTR20260034", disease="实体瘤", publication="2026-08-01")
        matching["public_title"] = "EGFR-PACC 项目研究"
        unrelated = snapshot("CTR20260035", disease="实体瘤", publication="2026-08-02")
        unrelated["public_title"] = "EGFR阳性实体瘤研究"
        self.repository.upsert_snapshot(matching, raw_document_id=None)
        self.repository.upsert_snapshot(unrelated, raw_document_id=None)
        result = self.service.search(LocalTrialQuery(project_codes=("EGFR-PACC",)))
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["results"][0]["registration_no"], "CTR20260034")

    def test_specific_small_cell_term_does_not_match_non_small_cell(self) -> None:
        self.assertTrue(contains_term("局限期小细胞肺癌", "小细胞肺癌"))
        self.assertFalse(contains_term("非小细胞肺癌", "小细胞肺癌"))

    def test_required_biomarker_is_a_conjunctive_public_evidence_filter(self) -> None:
        matching = snapshot("CTR20260016", disease="肺腺癌", publication="2026-08-01")
        matching["inclusion_criteria_raw"] = "经检测存在KRAS G12D突变的患者"
        non_matching = snapshot("CTR20260017", disease="肺腺癌", publication="2026-08-02")
        self.repository.upsert_snapshot(matching, raw_document_id=None)
        self.repository.upsert_snapshot(non_matching, raw_document_id=None)
        result = self.service.search(LocalTrialQuery(
            disease="肺腺癌", required_biomarkers=("KRAS12D",), limit=20,
        ))
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["results"][0]["registration_no"], "CTR20260016")
        self.assertEqual(
            result["results"][0]["constraint_evidence"]["biomarkers"][0]["term"],
            "KRAS12D",
        )
        self.assertEqual(result["excluded_counts"]["biomarker"], 1)

    def test_egfr_exon19_deletion_matches_chinese_public_evidence(self) -> None:
        self.assertTrue(contains_biomarker(
            "经检测存在EGFR 19外显子缺失的患者",
            "EGFR EXON19 DEL",
        ))
        self.assertFalse(contains_biomarker(
            "经检测存在EGFR L858R突变的患者",
            "EGFR EXON19 DEL",
        ))

    def test_standalone_variant_matches_positive_evidence_without_gene_inference(self) -> None:
        self.assertTrue(contains_biomarker("经检测存在KRAS G12C突变", "G12C"))
        self.assertTrue(contains_biomarker("经检测存在BRAF V600E突变", "V600E"))
        self.assertFalse(contains_biomarker("经检测存在KRAS G12D突变", "G12C"))

    def test_standalone_exon_event_matches_any_explicit_gene_evidence(self) -> None:
        self.assertTrue(contains_biomarker("存在EGFR 19号外显子缺失", "EXON19 DEL"))
        self.assertFalse(contains_biomarker("存在EGFR 20号外显子插入", "EXON19 DEL"))

    def test_sponsor_and_standalone_variant_are_same_trial_and_filters(self) -> None:
        matching = snapshot("CTR20260018", disease="肺癌", publication="2026-08-01")
        matching["sponsor_name_raw"] = "星河医药（示例）股份有限公司"
        matching["inclusion_criteria_raw"] = "存在KRAS G12C突变"
        other_sponsor = snapshot("CTR20260019", disease="肺癌", publication="2026-08-02")
        other_sponsor["sponsor_name_raw"] = "其他申办方"
        other_sponsor["inclusion_criteria_raw"] = "存在KRAS G12C突变"
        exclusion_only = snapshot("CTR20260020", disease="肺癌", publication="2026-08-03")
        exclusion_only["sponsor_name_raw"] = "星河医药（示例）有限公司"
        exclusion_only["exclusion_criteria_raw"] = "排除KRAS G12C突变"
        for item in (matching, other_sponsor, exclusion_only):
            self.repository.upsert_snapshot(item, raw_document_id=None)
        result = self.service.search(LocalTrialQuery(
            sponsors=("星河",), required_biomarkers=("G12C",), limit=20,
        ))
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["results"][0]["registration_no"], "CTR20260018")
        self.assertEqual(
            result["results"][0]["constraint_evidence"]["biomarkers"][0]["term"],
            "G12C",
        )

    def test_query_requires_at_least_one_auditable_retrieval_constraint(self) -> None:
        with self.assertRaises(ValueError):
            LocalTrialQuery()

    def test_blood_type_is_received_but_not_filtered(self) -> None:
        self.add("CTR20260006", "系统性红斑狼疮", "2026-08-01")
        result = self.service.search(LocalTrialQuery(disease="SLE", blood_type="A型"))
        self.assertEqual(result["total_count"], 1)
        self.assertTrue(result["warnings"])

    def test_restricted_fields_are_opt_in(self) -> None:
        self.add("CTR20260007", "系统性红斑狼疮", "2026-08-01", sites=[{
            "site_name_raw": "示例中心", "city": "上海", "principal_investigator": "研究者",
        }])
        public = self.service.get_trial("CTR20260007")
        restricted = self.service.get_trial("CTR20260007", include_restricted_fields=True)
        self.assertNotIn("sites", public)
        self.assertNotIn("contacts", public)
        self.assertIn("sites", restricted)
        self.assertIn("contacts", restricted)

    def test_age_and_sex_only_exclude_when_explicit(self) -> None:
        self.assertEqual(age_compatibility(40, "年龄18至65岁"), "matched")
        self.assertEqual(age_compatibility(70, "年龄18至65岁"), "excluded")
        self.assertEqual(age_compatibility(7, "18岁≤年龄≤75岁"), "excluded")
        self.assertEqual(age_compatibility(7, "年龄18周岁且≤69周岁"), "excluded")
        self.assertEqual(age_compatibility(70, "成人患者"), "unknown")
        self.assertEqual(sex_compatibility("male", "仅限女性受试者"), "excluded")
        self.assertEqual(sex_compatibility("male", "男性或女性均可"), "unknown")
        self.assertEqual(sex_compatibility("male", "男性和女性受试者均可参加"), "unknown")
        self.assertEqual(
            sex_compatibility("male", "女性受试者必须避孕，男性受试者必须采取避孕措施"),
            "unknown",
        )


if __name__ == "__main__":
    unittest.main()
