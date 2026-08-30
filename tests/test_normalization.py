from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from angela_orchestration.normalization import (
    SearchNormalizer,
    extract_biomarker_terms,
    is_biomarker_term,
)
from angela_orchestration.models import RequestMode, SearchParameters


class SearchNormalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        path = Path(self.tempdir.name) / "concepts.json"
        path.write_text(json.dumps({"concepts": [
            {"canonical": "系统性红斑狼疮", "synonyms": ["SLE", "lupus", "狼疮"]},
            {"canonical": "2型糖尿病", "synonyms": ["type 2 diabetes mellitus"]},
            {"canonical": "HER2阳性乳腺癌", "synonyms": [], "parent": "乳腺癌"},
            {"canonical": "肺癌", "synonyms": [], "related": ["非小细胞肺癌", "肺腺癌"]},
            {"canonical": "非小细胞肺癌", "synonyms": ["NSCLC"], "parent": "肺癌", "related": ["肺腺癌"]},
            {"canonical": "肺腺癌", "synonyms": ["lung adenocarcinoma"], "parent": "非小细胞肺癌"},
            {"canonical": "抑郁症", "synonyms": [], "related": ["重性抑郁障碍"]},
            {"canonical": "重性抑郁障碍", "synonyms": ["MDD"], "parent": "抑郁症"},
            {"canonical": "类风湿关节炎", "synonyms": ["RA"]},
        ]}, ensure_ascii=False), encoding="utf-8")
        self.normalizer = SearchNormalizer(path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_database_resolved_hard_filters_survive_model_normalization(self) -> None:
        fallback = SearchParameters(
            disease="asthma",
            city="Shanghai",
            province="Shanghai",
            sponsors=("Example Sponsor",),
            site_names=("Ruijin Hospital",),
            project_codes=("EGFR-PACC",),
            request_mode=RequestMode.INFORMATION_RETRIEVAL,
        )
        result = self.normalizer.from_text(
            "asthma clinical trials in Shanghai",
            payload={"raw_disease": "asthma", "canonical_disease": "asthma"},
            fallback=fallback,
        )
        self.assertEqual(result.province, "Shanghai")
        self.assertEqual(result.sponsors, ("Example Sponsor",))
        self.assertEqual(result.site_names, ("Ruijin Hospital",))
        self.assertEqual(result.project_codes, ("EGFR-PACC",))

    def test_fuzzy_chinese_conditions_are_auditable(self) -> None:
        result = self.normalizer.from_text("魔都五十来岁女性，狼疮方面最近仨月有没有项目？")
        self.assertEqual(result.disease, "狼疮")
        self.assertEqual((result.age_min, result.age_max), (50, 59))
        self.assertEqual(result.sex, "female")
        self.assertEqual(result.city, "上海")
        self.assertEqual(result.lookback_months, 3)

    def test_primary_disease_precedes_explicit_comorbidity(self) -> None:
        result = self.normalizer.from_text(
            "49岁女性2型糖尿病合并慢性肾病、肌酐偏高，请做公开条件初筛。"
        )
        self.assertEqual(result.disease, "2型糖尿病")
        self.assertTrue(any("慢性肾病" in item for item in result.comorbidities))

    def test_question_grammar_is_not_part_of_disease(self) -> None:
        result = self.normalizer.from_text("上海有帕金森病临床研究吗？")
        self.assertEqual(result.disease, "帕金森病")

    def test_stage_and_treatment_prefix_are_not_disease_identity(self) -> None:
        result = self.normalizer.from_text("晚期初治肺腺癌有啥项目")
        self.assertEqual(result.disease, "肺腺癌")
        self.assertEqual(result.disease_stage, "晚期")

    def test_locked_fields_cannot_be_overwritten_by_model_payload(self) -> None:
        fallback = SearchParameters(
            disease="哮喘",
            raw_disease="哮喘",
            canonical_disease="哮喘",
            city="北京",
            trial_phases=("III",),
            locked_fields=(
                "disease", "raw_disease", "canonical_disease", "city", "trial_phases"
            ),
            field_sources=(("disease", "audited_disease_concept"),),
        )
        result = self.normalizer.from_text(
            "北京哮喘III期临床研究",
            payload={
                "raw_disease": "北京地区的哮喘",
                "canonical_disease": "北京地区的哮喘",
                "city": "上海",
                "trial_phases": ["II"],
            },
            fallback=fallback,
        )
        self.assertEqual(result.disease, "哮喘")
        self.assertEqual(result.city, "北京")
        self.assertEqual(result.trial_phases, ("III",))
        self.assertIn("disease", result.locked_fields)

    def test_english_conditions_are_extracted(self) -> None:
        result = self.normalizer.from_text(
            "Male, 44, lupus, Guangzhou, trials in the last 6 months?"
        )
        self.assertEqual(result.age, 44)
        self.assertEqual(result.sex, "male")
        self.assertEqual(result.city, "广州")
        self.assertEqual(result.lookback_months, 6)

    def test_region_is_not_silently_rewritten_to_city(self) -> None:
        result = self.normalizer.from_text("红斑狼疮，华东地区，四十多岁，最近有没有研究？")
        self.assertEqual(result.region, "华东")
        self.assertIsNone(result.city)
        self.assertEqual((result.age_min, result.age_max), (40, 49))

    def test_user_subtype_wins_over_broader_model_output(self) -> None:
        result = self.normalizer.from_text(
            "45岁女性HER2阳性乳腺癌，上海，近三个月有什么项目？",
            payload={
                "raw_disease": "乳腺癌",
                "canonical_disease": "乳腺癌",
                "query_type": "disease",
                "preserve_specificity": False,
            },
        )
        self.assertEqual(result.disease, "HER2阳性乳腺癌")
        self.assertEqual(result.canonical_disease, "HER2阳性乳腺癌")
        self.assertTrue(result.preserve_specificity)

    def test_trial_phase_is_not_misread_as_patient_disease_stage(self) -> None:
        result = self.normalizer.from_text(
            "想找HER2阳性乳腺癌的II期临床研究，女性，41岁，给我5条。"
        )
        self.assertEqual(result.trial_phases, ("II",))
        self.assertIsNone(result.disease_stage)
        self.assertEqual(result.biomarkers, ())

    def test_standalone_phase_before_project_question_is_not_disease_stage(self) -> None:
        result = self.normalizer.from_text(
            "申办方星河，肺癌，G12C，上海，近一年，II期，有哪些项目？"
        )
        self.assertEqual(result.trial_phases, ("II",))
        self.assertIsNone(result.disease_stage)

    def test_explicit_patient_disease_stage_is_preserved(self) -> None:
        result = self.normalizer.from_text("女性41岁，肺癌II期，查询临床试验")
        self.assertEqual(result.disease_stage, "II期")

    def test_separate_biomarker_evidence_is_preserved(self) -> None:
        result = self.normalizer.from_text(
            "HER2阳性乳腺癌，PD-L1阳性，查询II期临床研究"
        )
        self.assertEqual(result.trial_phases, ("II",))
        self.assertIn("PD-L1阳性", result.biomarkers)

    def test_egfr_exon19_deletion_keeps_variant_granularity(self) -> None:
        result = self.normalizer.from_text(
            "男性，57岁，肺腺癌，EGFR 19外显子缺失，奥希替尼后进展"
        )
        self.assertEqual(result.disease, "肺腺癌")
        self.assertIn("EGFR EXON19 DEL", result.biomarkers)

    def test_related_terms_never_downgrade_specific_disease_granularity(self) -> None:
        cases = (
            ("肺腺癌", "肺腺癌"),
            ("lung adenocarcinoma", "肺腺癌"),
            ("非小细胞肺癌", "非小细胞肺癌"),
            ("NSCLC", "非小细胞肺癌"),
            ("重性抑郁障碍", "重性抑郁障碍"),
            ("MDD", "重性抑郁障碍"),
        )
        for raw, canonical in cases:
            with self.subTest(raw=raw):
                result = self.normalizer.from_text(f"45岁女性，{raw}，查询临床试验")
                self.assertEqual(result.raw_disease, raw)
                self.assertEqual(result.canonical_disease, canonical)
                self.assertTrue(result.preserve_specificity)

    def test_unaudited_model_canonical_cannot_replace_raw_disease(self) -> None:
        result = self.normalizer.from_text(
            "45岁女性，乳房恶性肿瘤，上海，查询临床试验。",
            payload={
                "raw_disease": "乳房恶性肿瘤",
                "canonical_disease": "乳腺癌",
                "query_type": "disease",
                "preserve_specificity": False,
            },
        )

        self.assertEqual(result.raw_disease, "乳房恶性肿瘤")
        self.assertEqual(result.canonical_disease, "乳房恶性肿瘤")
        self.assertEqual(result.age, 45)
        self.assertEqual(result.sex, "female")
        self.assertEqual(result.city, "上海")

    def test_audited_synonym_still_uses_configured_canonical(self) -> None:
        result = self.normalizer.from_text(
            "55岁男性SLE，查询临床试验。",
            payload={
                "raw_disease": "SLE",
                "canonical_disease": "任意模型输出",
            },
        )

        self.assertEqual(result.raw_disease, "SLE")
        self.assertEqual(result.canonical_disease, "系统性红斑狼疮")

    def test_ascii_disease_alias_requires_a_complete_token(self) -> None:
        positive = self.normalizer.from_text("RA患者，查询临床试验")
        self.assertEqual(positive.canonical_disease, "类风湿关节炎")

        for marker in ("BRAF突变", "KRAS G12C突变", "HRAS突变"):
            with self.subTest(marker=marker):
                match = self.normalizer.extract_disease(marker)
                self.assertNotEqual(
                    match.canonical if match else None,
                    "类风湿关节炎",
                )

    def test_biomarker_or_score_without_disease_is_not_a_drug_or_disease(self) -> None:
        for text in (
            "KRAS G12C一线化疗后的靶向药项目",
            "目前初治 KRAS G12C研究有哪些",
            "SLEDAI评分16，帮我查临床试验",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.normalizer.extract_disease(text))

    def test_compact_biomarker_is_separated_from_following_disease(self) -> None:
        result = self.normalizer.from_text(
            "KRAS12D的胰腺癌患者，一线治疗后进展，找临床研究项目"
        )
        self.assertEqual(result.canonical_disease, "胰腺癌")
        self.assertEqual(result.biomarkers, ("KRAS12D",))
        self.assertNotIn("biomarker", result.complexity_reasons())
        self.assertIn("treatment_line", result.complexity_reasons())

    def test_compact_biomarker_without_disease_is_not_guessed(self) -> None:
        for text in ("KRAS12D有什么项目", "BRAF V600E临床研究", "EGFRL858R项目"):
            with self.subTest(text=text):
                self.assertIsNone(self.normalizer.extract_disease(text))

    def test_city_payload_uses_suffix_free_canonical_form(self) -> None:
        result = self.normalizer.from_text(
            "女性，29岁，抑郁症，最近半年",
            payload={"city": "上海市"},
        )
        self.assertEqual(result.city, "上海")

    def test_query_plan_exposes_hard_and_criteria_constraints(self) -> None:
        result = self.normalizer.from_text(
            "55岁男性肺腺癌，EGFR L858R突变，上海，近半年找临床试验"
        )
        plan = result.as_query_plan()
        self.assertEqual(plan["hard_filters"]["city"], "上海")
        self.assertEqual(plan["criteria_constraints"]["biomarkers"], ["EGFR L858R突变"])
        self.assertFalse(plan["requires_agent_review"])

    def test_birth_year_is_not_misread_as_age(self) -> None:
        result = self.normalizer.from_text("2019年出生的女孩，SLE两年，东北有什么项目？")
        self.assertEqual(result.birth_year, 2019)
        self.assertIsNone(result.age)
        self.assertEqual(result.disease_duration_months, 24)
        self.assertEqual(result.sex, "female")
        self.assertEqual(result.region, "东北")

    def test_diagnosis_year_is_not_a_birth_year(self) -> None:
        result = self.normalizer.from_text("2019年确诊SLE，现在有什么项目？")
        self.assertIsNone(result.birth_year)

    def test_explicit_score_threshold_is_preserved_for_complex_review(self) -> None:
        result = self.normalizer.from_text("52岁男性SLE，SLEDAI得分大于12分，查临床试验")
        self.assertEqual(result.scores, ("SLEDAI得分大于12分",))

    def test_model_cannot_invent_birth_year_or_result_limit(self) -> None:
        result = self.normalizer.from_text(
            "52岁男性SLE，请判断是否可能适合临床试验",
            payload={"birth_year": 1964, "max_results": 20},
        )
        self.assertIsNone(result.birth_year)
        self.assertEqual(result.max_results, 10)

    def test_explicit_user_result_limit_is_preserved(self) -> None:
        result = self.normalizer.from_text("52岁男性SLE，给我15条")
        self.assertEqual(result.max_results, 15)

        embedded = self.normalizer.from_text("45岁女性，SLE，查询最近一年的5条试验")
        self.assertEqual(embedded.max_results, 5)

    def test_result_limit_accepts_common_item_classifier(self) -> None:
        result = self.normalizer.from_text("广州克罗恩病，给我最近公开的5个。")
        self.assertEqual(result.max_results, 5)

        months = self.normalizer.from_text(
            "重性抑郁障碍，女患者，38岁，广州，最近12个月，显示2条研究。"
        )
        self.assertEqual(months.lookback_months, 12)
        self.assertEqual(months.max_results, 2)

    def test_source_patient_facts_are_merged_when_model_omits_them(self) -> None:
        result = self.normalizer.from_text(
            "55岁男性肺腺癌，KRAS G12C突变，含铂化疗后进展，肝功能轻度异常",
            payload={
                "raw_disease": "肺腺癌",
                "canonical_disease": "肺腺癌",
                "prior_treatments": [],
                "biomarkers": [],
                "disease_statuses": [],
                "organ_function_findings": [],
            },
        )
        self.assertIn("KRAS G12C突变", result.biomarkers)
        self.assertIn("含铂化疗后进展", result.prior_treatments)
        self.assertIn("含铂化疗后进展", result.disease_statuses)
        self.assertIn("肝功能轻度异常", result.organ_function_findings)

    def test_model_source_and_fallback_patient_facts_are_all_preserved(self) -> None:
        from angela_orchestration.models import SearchParameters

        fallback = SearchParameters(
            disease="SLE",
            prior_treatments=("羟氯喹",),
            comorbidities=("高血压",),
        )
        result = self.normalizer.from_text(
            "SLE伴狼疮性肾炎，既往使用吗替麦考酚酯，控制中的高血压",
            payload={
                "raw_disease": "SLE",
                "canonical_disease": "系统性红斑狼疮",
                "prior_treatments": ["激素治疗"],
                "comorbidities": [],
            },
            fallback=fallback,
        )
        self.assertEqual(
            result.prior_treatments,
            ("激素治疗", "既往使用吗替麦考酚酯", "羟氯喹"),
        )
        self.assertIn("SLE伴狼疮性肾炎", result.comorbidities)
        self.assertIn("控制中的高血压", result.comorbidities)

    def test_standalone_protein_variants_use_one_generic_syntax(self) -> None:
        expected = ("G12C", "G12D", "V600E", "L858R", "T790M")
        self.assertEqual(
            extract_biomarker_terms("、".join(expected)),
            expected,
        )
        for value in expected:
            self.assertTrue(is_biomarker_term(value), value)

    def test_standalone_variant_is_not_assigned_a_gene(self) -> None:
        self.assertEqual(extract_biomarker_terms("星河的G12C项目"), ("G12C",))
        self.assertNotIn("KRAS", extract_biomarker_terms("星河的G12C项目")[0])

    def test_exon_events_share_one_generic_syntax(self) -> None:
        self.assertEqual(
            extract_biomarker_terms("exon19缺失，EGFR exon20 insertion"),
            ("EXON19 DEL", "EGFR EXON20 INS"),
        )


if __name__ == "__main__":
    unittest.main()
