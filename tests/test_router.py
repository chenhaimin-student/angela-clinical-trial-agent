from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from angela_orchestration.entity_resolution import LocationResolution, SiteResolution, SponsorResolution
from angela_orchestration.models import RequestMode, RouteKind
from angela_orchestration.router import IntentRouter


class _TestSponsorResolver:
    def resolve(self, text: str):
        if "星河" in text:
            return (SponsorResolution("星河", ("星河医药（示例）股份有限公司",)),)
        if "海岚生物" in text:
            return (SponsorResolution("海岚生物", ("海岚生物科技（示例）有限公司",)),)
        return ()


class _TestLocationResolver:
    def resolve(self, text: str):
        values = []
        if "Alicante" in text:
            values.append(LocationResolution("alicante", "Alicante", "city"))
        if "shanghai" in text.casefold():
            values.append(LocationResolution("shanghai", "SHANGHAI", "city"))
        if "DEL" in text:
            values.append(LocationResolution("del", "DEL", "city"))
        if "old" in text.casefold():
            values.append(LocationResolution("old", "OLD", "city"))
        if "西南" in text:
            values.append(LocationResolution("西南", "西南", "city"))
        if "上海" in text:
            values.append(LocationResolution("上海", "上海", "city"))
        if "天津" in text:
            values.append(LocationResolution("天津", "天津", "city"))
        if "广东省" in text:
            values.append(LocationResolution("广东省", "广东省", "province"))
        return tuple(values)


class _TestSiteResolver:
    def resolve(self, text: str):
        if "上海交通大学附属瑞金医院" in text:
            return (SiteResolution(
                "上海交通大学附属瑞金医院",
                "上海交通大学医学院附属瑞金医院",
            ),)
        return ()


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        concepts = Path(self.tempdir.name) / "concepts.json"
        concepts.write_text(json.dumps({
            "concepts": [
                {"canonical": "系统性红斑狼疮", "synonyms": ["SLE"], "related": ["红斑狼疮"]},
                {"canonical": "重度抑郁障碍", "synonyms": ["抑郁症", "MDD"], "related": []},
                {
                    "canonical": "非小细胞肺癌",
                    "synonyms": ["NSCLC", "肺腺癌", "lung adenocarcinoma"],
                    "related": ["肺癌"],
                },
                {"canonical": "类风湿关节炎", "synonyms": ["RA"], "related": []},
                {
                    "canonical": "2型糖尿病",
                    "synonyms": ["T2DM", "type 2 diabetes mellitus"],
                    "related": [],
                },
            ]
        }, ensure_ascii=False), encoding="utf-8")
        self.router = IntentRouter(concepts)
        self.router._sponsor_resolver = _TestSponsorResolver()
        self.router._location_resolver = _TestLocationResolver()
        self.router._site_resolver = _TestSiteResolver()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_structured_patient_search_extracts_fields(self) -> None:
        decision = self.router.classify("男性，40岁，AB型血，肺腺癌，近半年，给我15条")
        self.assertEqual(decision.kind, RouteKind.CDE_SEARCH)
        self.assertTrue(decision.fast_eligible)
        self.assertEqual(decision.search.age, 40)
        self.assertEqual(decision.search.sex, "male")
        self.assertEqual(decision.search.blood_type, "AB型血")
        self.assertEqual(decision.search.lookback_months, 6)
        self.assertEqual(decision.search.max_results, 15)
        self.assertTrue({
            "disease", "age", "sex", "blood_type", "lookback_months", "max_results"
        }.issubset(decision.search.locked_fields))

    def test_free_disease_search_is_supported(self) -> None:
        for prompt in ("查询哮喘的临床试验", "查找胃癌临床研究", "女性 32岁 精神分裂症"):
            with self.subTest(prompt=prompt):
                decision = self.router.classify(prompt)
                self.assertEqual(decision.kind, RouteKind.CDE_SEARCH)
                self.assertTrue(decision.fast_eligible)
                self.assertEqual(decision.search.max_results, 10)

    def test_unaudited_disease_uses_raw_lexical_fast_route(self) -> None:
        prompts = (
            ("请查询癫痫的临床试验，男性，42岁，杭州，给我5条。", "癫痫"),
            ("2010年出生的男孩，诊断为特应性皮炎，请找当前招募项目。", "特应性皮炎"),
            ("女性，47岁，患有强直性脊柱炎，华南地区，最近半年有哪些临床研究？", "强直性脊柱炎"),
            ("female, 49-year-old, diagnosed with 重症肌无力, show clinical trials in shanghai.", "重症肌无力"),
            ("female, 57-year-old, diagnosed with 丙型肝炎, show clinical trials in shanghai.", "丙型肝炎"),
            ("女性，49岁，重症肌无力，上海有什么临床试验？", "重症肌无力"),
        )
        for prompt, disease in prompts:
            with self.subTest(disease=disease):
                decision = self.router.classify(prompt)
                self.assertEqual(decision.kind, RouteKind.CDE_SEARCH)
                self.assertTrue(decision.fast_eligible)
                self.assertEqual(decision.search.raw_disease, disease)
                self.assertEqual(decision.search.canonical_disease, disease)

    def test_patient_context_prefix_is_not_part_of_raw_disease(self) -> None:
        decision = self.router.classify(
            "患者情况：多发性骨髓瘤，男性，69岁，经过三线治疗仍复发。请按入排条件分析是否适合，返回5条。"
        )
        self.assertEqual(decision.search.raw_disease, "多发性骨髓瘤")

    def test_primary_diagnosis_precedes_later_comorbidity(self) -> None:
        decision = self.router.classify(
            "患者情况：T2DM，女性，59岁，合并慢性肾病，肌酐偏高。"
            "请按入排条件分析是否适合，返回5条。"
        )
        self.assertEqual(decision.search.raw_disease, "T2DM")
        self.assertEqual(decision.search.canonical_disease, "2型糖尿病")
        self.assertIn("合并慢性肾病", decision.search.comorbidities)

    def test_full_english_diabetes_name_is_preserved(self) -> None:
        decision = self.router.classify(
            "患有type 2 diabetes mellitus，五十来岁，男性，有项目吗？"
        )
        self.assertEqual(decision.search.raw_disease, "type 2 diabetes mellitus")
        self.assertEqual(decision.search.canonical_disease, "2型糖尿病")

    def test_english_diagnosis_prefix_is_not_part_of_raw_disease(self) -> None:
        decision = self.router.classify(
            "female, 41-year-old, diagnosed with 阿尔茨海默病, show 4 clinical trials in shanghai."
        )
        self.assertIsNotNone(decision.search)
        self.assertEqual(decision.search.raw_disease, "阿尔茨海默病")

    def test_common_search_prefixes_are_not_part_of_raw_disease(self) -> None:
        cases = (
            ("请查肺癌目前有哪些临床试验，男患者，47岁。", "肺癌"),
            ("请查询鼻咽癌的临床试验，男性，30岁，杭州，给我5条。", "鼻咽癌"),
            ("想找哮喘的II期临床研究，女性，41岁，给我5条。", "哮喘"),
            ("患者确诊双相情感障碍，男，52岁，A型血，查询临床试验。", "双相情感障碍"),
        )
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                decision = self.router.classify(prompt)
                self.assertEqual(decision.search.raw_disease, expected)

    def test_natural_language_hyphens_do_not_become_project_codes(self) -> None:
        cases = (
            "female, 41-year-old, diagnosed with 阿尔茨海默病, show 4 clinical trials in shanghai.",
            "患者确诊HER2-positive breast cancer，男，52岁，查询临床试验。",
            "non-small cell lung cancer，女性，32岁，想看当前招募研究。",
            "PD-L1阳性的肺癌有哪些临床试验？",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                decision = self.router.classify(prompt)
                self.assertEqual(decision.search.project_codes, ())

        her2 = self.router.classify(
            "患者确诊HER2-positive breast cancer，男，52岁，查询临床试验。"
        )
        self.assertEqual(her2.search.raw_disease, "HER2-positive breast cancer")

        explicit = self.router.classify("星河有哪些EGFR-PACC项目")
        self.assertEqual(explicit.search.project_codes, ("EGFR-PACC",))

    def test_ordinary_request_words_do_not_become_site_filters(self) -> None:
        self.router._site_resolver = type("Resolver", (), {
            "resolve": lambda _self, _text: (
                SiteResolution("clinicaltrials", "ClinicalTrials Research Site"),
            )
        })()
        decision = self.router.classify(
            "female, 41-year-old, diagnosed with 阿尔茨海默病, show 4 clinical trials in shanghai."
        )
        self.assertEqual(decision.search.site_names, ())

    def test_short_english_grammar_tokens_do_not_become_locations(self) -> None:
        decision = self.router.classify(
            "rheumatoid arthritis patient, male, 61-year-old, show 4 clinical trials."
        )
        self.assertIsNone(decision.search.city)
        self.assertIsNone(decision.search.province)

    def test_unknown_disease_keeps_all_basic_filters_without_model_help(self) -> None:
        decision = self.router.classify(
            "45岁女性，乳腺癌，上海，查询最近一年的5条试验。"
        )

        self.assertEqual(decision.kind, RouteKind.CDE_SEARCH)
        self.assertEqual(decision.search.raw_disease, "乳腺癌")
        self.assertEqual(decision.search.canonical_disease, "乳腺癌")
        self.assertEqual(decision.search.age, 45)
        self.assertEqual(decision.search.sex, "female")
        self.assertEqual(decision.search.city, "上海")
        self.assertEqual(decision.search.lookback_months, 12)
        self.assertEqual(decision.search.max_results, 5)

    def test_ctr_specialized_routes_stay_on_agent(self) -> None:
        questionnaire = self.router.classify("请为CTR20261056生成通俗问卷")
        live = self.router.classify("实时核验CTR20261056最新状态")
        detail = self.router.classify("介绍CTR20261056")
        self.assertEqual(questionnaire.kind, RouteKind.CDE_QUESTIONNAIRE)
        self.assertEqual(live.kind, RouteKind.CDE_LIVE_VERIFY)
        self.assertEqual(detail.kind, RouteKind.CDE_DETAIL)
        self.assertFalse(questionnaire.fast_eligible)

    def test_general_and_privacy_routes(self) -> None:
        self.assertEqual(self.router.classify("你好").kind, RouteKind.GENERAL)
        self.assertEqual(
            self.router.classify("肺癌，手机号13800138000").kind,
            RouteKind.PRIVACY_BLOCK,
        )

    def test_fuzzy_known_disease_is_safe_fast_fallback(self) -> None:
        decision = self.router.classify(
            "不太懂学名，就是红斑狼疮那类病，华东地区，四十多岁，最近有没有研究？"
        )
        self.assertEqual(decision.kind, RouteKind.CDE_SEARCH)
        self.assertTrue(decision.fast_eligible)
        self.assertEqual(decision.search.region, "华东")
        self.assertEqual((decision.search.age_min, decision.search.age_max), (40, 49))

    def test_complex_matching_request_is_not_forced_to_fast_fallback(self) -> None:
        decision = self.router.classify("同时有SLE和类风湿，既往多线治疗，判断哪个试验最适合")
        self.assertFalse(decision.fast_eligible)

    def test_ascii_alias_is_fast_only_when_present_as_a_complete_token(self) -> None:
        positive = self.router.classify("RA患者，查询临床试验")
        self.assertEqual(positive.kind, RouteKind.CDE_SEARCH)
        self.assertTrue(positive.fast_eligible)
        self.assertEqual(positive.search.canonical_disease, "类风湿关节炎")

        cases = (
            "大肠癌晚期，BRAF突变，请问有什么临床研究适合？",
            "甲状腺髓样癌术后肺转移，HRAS突变，有临床项目吗？",
            "KRAS G12C一线化疗后的靶向药项目",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                decision = self.router.classify(prompt)
                self.assertNotEqual(
                    decision.search.canonical_disease if decision.search else None,
                    "类风湿关节炎",
                )

    def test_disease_is_separated_from_trailing_biomarker_attributes(self) -> None:
        cases = (
            ("大肠癌晚期，BRAF突变，请问有什么临床研究适合？", "大肠癌"),
            ("甲状腺髓样癌术后肺转移，HRAS突变，有临床项目吗？", "甲状腺髓样癌"),
            ("KRAS G12C肺癌，有什么临床试验？", "肺癌"),
        )
        for prompt, disease in cases:
            with self.subTest(prompt=prompt):
                decision = self.router.classify(prompt)
                self.assertEqual(decision.kind, RouteKind.CDE_SEARCH)
                self.assertEqual(
                    decision.fast_eligible,
                    "适合" not in prompt and "肺转移" not in prompt,
                )
                self.assertEqual(decision.search.canonical_disease, disease)

    def test_biomarker_with_treatment_constraints_is_complex(self) -> None:
        for prompt in (
            "KRAS G12C一线化疗后的靶向药项目",
            "目前初治 KRAS G12C研究有哪些",
        ):
            with self.subTest(prompt=prompt):
                decision = self.router.classify(prompt)
                self.assertFalse(decision.fast_eligible)
                self.assertIsNotNone(decision.search)
                self.assertEqual(
                    decision.search.request_mode,
                    RequestMode.INFORMATION_RETRIEVAL,
                )

    def test_score_without_disease_does_not_invent_a_search(self) -> None:
        decision = self.router.classify("SLEDAI评分16，帮我查临床试验")
        self.assertFalse(decision.fast_eligible)
        self.assertIsNone(decision.search)

    def test_compact_biomarker_and_disease_build_complex_query_plan(self) -> None:
        decision = self.router.classify(
            "KRAS12D的胰腺癌患者，一线方案进展，找临床研究项目"
        )
        self.assertEqual(decision.kind, RouteKind.CDE_SEARCH)
        self.assertFalse(decision.fast_eligible)
        self.assertEqual(decision.search.canonical_disease, "胰腺癌")
        self.assertEqual(decision.search.biomarkers, ("KRAS12D",))
        self.assertNotIn("biomarker", decision.search.complexity_reasons())
        self.assertEqual(decision.search.request_mode, RequestMode.PATIENT_MATCHING)

    def test_company_and_standalone_variant_are_complete_fast_filters(self) -> None:
        decision = self.router.classify("星河有哪些G12C项目？")
        self.assertEqual(decision.kind, RouteKind.CDE_SEARCH)
        self.assertTrue(decision.fast_eligible)
        self.assertEqual(decision.search.sponsors, ("星河",))
        self.assertEqual(decision.search.biomarkers, ("G12C",))
        self.assertEqual(decision.search.disease, "")
        self.assertEqual(decision.search.request_mode, RequestMode.INFORMATION_RETRIEVAL)

    def test_region_word_does_not_become_database_sponsor(self) -> None:
        self.router._sponsor_resolver = type(
            "RegionSponsorResolver",
            (),
            {"resolve": lambda self, text: (
                SponsorResolution("华东", ("远岚医药（示例）股份有限公司",)),
            )},
        )()
        decision = self.router.classify(
            "性别不限，65岁，lung adenocarcinoma，华东地区，查招募项目。"
        )
        self.assertEqual(decision.search.disease, "lung adenocarcinoma")
        self.assertEqual(decision.search.region, "华东")
        self.assertEqual(decision.search.sponsors, ())

    def test_hospital_question_grammar_is_not_part_of_disease(self) -> None:
        decision = self.router.classify(
            "上海交通大学附属瑞金医院有什么哮喘项目吗？"
        )
        self.assertEqual(decision.search.disease, "哮喘")
        self.assertEqual(
            decision.search.site_names,
            ("上海交通大学附属瑞金医院",),
        )

    def test_request_modes_separate_retrieval_patient_and_criteria_work(self) -> None:
        retrieval = self.router.classify(
            "申办方星河，肺癌，G12C，上海，近一年，II期，有哪些项目？"
        )
        patient = self.router.classify("男性55岁，肺癌，G12C，帮我匹配临床试验")
        criteria = self.router.classify("肺癌项目的入选标准和排除标准是什么？")
        self.assertEqual(retrieval.search.request_mode, RequestMode.INFORMATION_RETRIEVAL)
        self.assertTrue(retrieval.fast_eligible)
        self.assertEqual(retrieval.search.city, "上海")
        self.assertEqual(patient.search.request_mode, RequestMode.PATIENT_MATCHING)
        self.assertTrue(patient.fast_eligible)
        self.assertEqual(criteria.search.request_mode, RequestMode.CRITERIA_ANALYSIS)
        self.assertFalse(criteria.fast_eligible)

    def test_english_age_and_sex_select_patient_matching(self) -> None:
        for prompt in (
            "female, 33-year-old, diagnosed with 黑色素瘤, show 4 clinical trials in shanghai.",
            "male, 52-year-old, diagnosed with lung adenocarcinoma, show clinical trials.",
        ):
            with self.subTest(prompt=prompt):
                decision = self.router.classify(prompt)
                self.assertEqual(decision.search.request_mode, RequestMode.PATIENT_MATCHING)
                self.assertNotEqual(decision.search.city, "OLD")
                if "shanghai" in prompt.casefold():
                    self.assertEqual(decision.search.city, "上海")

    def test_region_and_age_words_do_not_become_dynamic_cities(self) -> None:
        region = self.router.classify(
            "系统性红斑狼疮，男性，70岁，西南地区，近一年有哪些招募中的试验？"
        )
        age_word = self.router.classify(
            "rheumatoid arthritis patient, male, 61-year-old, show 4 clinical trials."
        )
        self.assertEqual(region.search.region, "西南")
        self.assertIsNone(region.search.city)
        self.assertIsNone(age_word.search.city)

    def test_exact_research_center_location_is_fast_metadata_filter(self) -> None:
        decision = self.router.classify(
            "Alicante有研究中心的星河 肺癌 G12C项目"
        )
        self.assertTrue(decision.fast_eligible)
        self.assertEqual(decision.search.request_mode, RequestMode.INFORMATION_RETRIEVAL)
        self.assertEqual(decision.search.city, "Alicante")

    def test_location_token_inside_biomarker_is_not_a_city_filter(self) -> None:
        decision = self.router.classify("贝达药业有哪些EXON19 DEL项目？")
        self.assertTrue(decision.fast_eligible)
        self.assertEqual(decision.search.biomarkers, ("EXON19 DEL",))
        self.assertIsNone(decision.search.city)

    def test_city_inside_company_name_is_not_a_location_filter(self) -> None:
        decision = self.router.classify("查一下海岚生物的非小细胞肺癌临床试验")
        self.assertTrue(decision.fast_eligible)
        self.assertEqual(decision.search.sponsors, ("海岚生物",))
        self.assertIsNone(decision.search.city)

    def test_standalone_variant_family_is_not_g12c_specific(self) -> None:
        for variant in ("G12D", "V600E", "L858R", "T790M"):
            with self.subTest(variant=variant):
                decision = self.router.classify(f"星河有哪些{variant}项目？")
                self.assertEqual(decision.search.biomarkers, (variant,))
                self.assertTrue(decision.fast_eligible)

    def test_marker_only_query_keeps_basic_location_and_time_filters(self) -> None:
        decision = self.router.classify("HER2阳性项目，天津，近半年，给我5条")
        self.assertEqual(decision.search.biomarkers, ("HER2阳性",))
        self.assertEqual(decision.search.city, "天津")
        self.assertEqual(decision.search.lookback_months, 6)
        self.assertEqual(decision.search.max_results, 5)

    def test_site_company_and_criteria_requests_use_agent_route(self) -> None:
        for prompt in (
            "上海哪家医院的哮喘项目最多？",
            "云舟慢淋项目的对照药是什么？",
            "肺癌项目的入排标准有哪些？",
        ):
            with self.subTest(prompt=prompt):
                decision = self.router.classify(prompt)
                self.assertFalse(decision.fast_eligible)

    def test_named_hospital_is_preserved_instead_of_downgraded_to_city(self) -> None:
        decision = self.router.classify("上海交通大学附属瑞金医院参与的哮喘项目有哪些")
        self.assertEqual(decision.search.site_names, ("上海交通大学附属瑞金医院",))
        self.assertEqual(decision.search.city, "上海")
        self.assertEqual(decision.search.disease, "哮喘")

    def test_province_is_not_downgraded_to_city(self) -> None:
        decision = self.router.classify("广东省肺癌临床研究项目有哪些")
        self.assertEqual(decision.search.province, "广东省")
        self.assertEqual(decision.search.disease, "肺癌")

    def test_hyphenated_project_code_has_priority_over_gene_marker(self) -> None:
        decision = self.router.classify("EGFR-PACC的项目有哪些？")
        self.assertEqual(decision.search.project_codes, ("EGFR-PACC",))
        self.assertEqual(decision.search.biomarkers, ())
        self.assertEqual(decision.search.disease, "")

    def test_screen_failure_request_uses_criteria_analysis(self) -> None:
        decision = self.router.classify("呼吸哮喘COPD单抗项目筛败率有点高怎么规避")
        self.assertEqual(decision.search.request_mode, RequestMode.CRITERIA_ANALYSIS)
        self.assertFalse(decision.fast_eligible)

    def test_unresolved_explicit_company_is_not_silently_dropped(self) -> None:
        decision = self.router.classify("你好帮我归纳一下初治胃癌的项目，在澄明公司")
        self.assertEqual(decision.search.disease, "胃癌")
        self.assertEqual(decision.search.sponsors, ("澄明",))

    def test_site_ranking_request_uses_agent_analysis(self) -> None:
        decision = self.router.classify("广东省肺癌临床研究项目做得最好的是那家医院")
        self.assertFalse(decision.fast_eligible)

    def test_named_site_enrollment_count_uses_agent_analysis(self) -> None:
        decision = self.router.classify("上海交通大学附属瑞金医院临床哮喘项目有几个，分别入组多少")
        self.assertEqual(decision.search.disease, "哮喘")
        self.assertFalse(decision.fast_eligible)

    def test_conversational_prefix_does_not_become_disease(self) -> None:
        decision = self.router.classify("你好，关于帕金森目前在研的项目有哪些")
        self.assertEqual(decision.search.disease, "帕金森")

    def test_trailing_search_language_is_not_part_of_disease(self) -> None:
        decision = self.router.classify("多发性骨髓瘤在运行中的临床项目有哪些")
        self.assertEqual(decision.search.disease, "多发性骨髓瘤")

    def test_specific_lung_cancer_phrase_is_not_downgraded(self) -> None:
        decision = self.router.classify("局限期小细胞肺癌目前有什么临床研究正在入组")
        self.assertEqual(decision.search.disease, "局限期小细胞肺癌")
        self.assertTrue(decision.search.preserve_specificity)

    def test_query_prefix_is_not_part_of_disease(self) -> None:
        decision = self.router.classify("查询哮喘的临床试验数据")
        self.assertEqual(decision.search.disease, "哮喘")

    def test_disease_after_generic_project_language_is_recovered(self) -> None:
        decision = self.router.classify("现在获批正在招募的GCP项目有哪些入组初治肠癌")
        self.assertEqual(decision.search.disease, "肠癌")


if __name__ == "__main__":
    unittest.main()
