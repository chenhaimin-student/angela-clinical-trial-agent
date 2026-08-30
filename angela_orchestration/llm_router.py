from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import replace
from typing import Any

from .config import OrchestrationConfig
from .models import RouteDecision, RouteKind, SearchParameters
from .normalization import SearchNormalizer


logger = logging.getLogger("angela.orchestration.llm_router")

_SYSTEM_PROMPT = """你是 Angela 的只读结构化路由器，只负责识别临床试验搜索意图、提取用户明确给出的条件和判断请求复杂度。
不得判断患者最终能否入组，不得给出诊断或治疗建议。
complex=true 的情况包括：要求判断是否入组、解读或筛选入排标准、按量表/化验/生物标志物阈值筛选、未成年人且要求资格判断或同时存在复杂条件、否定病史或合并症条件、初次诊断且资料不足、复杂既往治疗、多个疾病或需要多步医学推理。
例如“SLEDAI大于12分”“无其他精神类疾病”“未成年患者并要求判断能否入组”必须设为 complex=true；仅按明确出生年份或年龄做宽泛试验搜索可以设 complex=false，并保留 birth_year/age 交给资格守卫。存在年龄等关键歧义时还应设 needs_clarification=true。
普通患者条件搜索可设 complex=false。raw_disease 必须保留用户使用的最具体疾病、亚型、药物或项目代号；canonical_disease 给出规范名称，但不得把亚型泛化为父级疾病。
例如 HER2阳性乳腺癌必须保持为 HER2阳性乳腺癌，不能改成乳腺癌。华东是 region，不得擅自改成上海。五十来岁表示 age_min=50、age_max=59，而不是一个精确年龄。
出生年份与年龄必须分开提取：例如“2019年出生”设置 birth_year=2019，不能把2019当年龄；“2019年确诊”不是出生年份。
尽量提取用户明确给出的疾病分期、治疗线数、既往治疗、突变/生物标志物、评分量表、合并疾病、器官功能或化验、妊娠生育状态、过敏史、复发/进展/难治等状态；不得从常识补全未表达的信息。
用户要求判断是否适合、是否入组或精准匹配时 precise_eligibility=true。
只提取用户明确表达的条件；未知字段使用 null、unknown 或空数组。不要包含“有个、患者、病人、患有”等修饰词。
只按给定 JSON Schema 输出。"""

_GENERAL_CDE_CUES = (
    "临床试验",
    "临床研究",
    "试验项目",
    "临床项目",
    "有什么项目",
    "有哪些项目",
    "项目吗",
    "招募",
    "入组",
    "入选",
    "排除",
    "入排",
    "入组要求",
    "入组条件",
    "clinical trial",
    "clinical study",
    "trials",
    "studies",
)
_PROJECT_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z]{2,}[A-Z0-9-]*\d[A-Z0-9-]*|[A-Z]{2,8})(?![A-Za-z0-9])"
)
_SMALL_TALK = {
    "你好",
    "您好",
    "哈喽",
    "hello",
    "hi",
    "测试",
    "好的",
    "谢谢",
}

_ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {"type": "string", "enum": ["cde_search", "general"]},
        "query_type": {"type": "string", "enum": ["disease", "drug", "unknown"]},
        "raw_disease": {"type": ["string", "null"]},
        "canonical_disease": {"type": ["string", "null"]},
        "preserve_specificity": {"type": "boolean"},
        "age": {"type": ["integer", "null"], "minimum": 0, "maximum": 120},
        "age_min": {"type": ["integer", "null"], "minimum": 0, "maximum": 120},
        "age_max": {"type": ["integer", "null"], "minimum": 0, "maximum": 120},
        "sex": {"type": "string", "enum": ["male", "female", "unknown"]},
        "city": {"type": ["string", "null"]},
        "region": {"type": ["string", "null"]},
        "blood_type": {"type": ["string", "null"]},
        "lookback_months": {"type": ["integer", "null"], "minimum": 1, "maximum": 60},
        "trial_phases": {"type": "array", "items": {"type": "string", "enum": ["I", "II", "III", "IV"]}, "maxItems": 4},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
        "birth_year": {"type": ["integer", "null"], "minimum": 1900, "maximum": 2100},
        "disease_duration_months": {"type": ["integer", "null"], "minimum": 0, "maximum": 1440},
        "disease_stage": {"type": ["string", "null"]},
        "treatment_line": {"type": ["string", "null"]},
        "prior_treatments": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "biomarkers": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "scores": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "comorbidities": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "disease_statuses": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "organ_function_findings": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "reproductive_statuses": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "allergies": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "precise_eligibility": {"type": "boolean"},
        "complex": {"type": "boolean"},
        "needs_clarification": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "intent",
        "query_type",
        "raw_disease",
        "canonical_disease",
        "preserve_specificity",
        "age",
        "age_min",
        "age_max",
        "sex",
        "city",
        "region",
        "blood_type",
        "lookback_months",
        "trial_phases",
        "max_results",
        "birth_year",
        "disease_duration_months",
        "disease_stage",
        "treatment_line",
        "prior_treatments",
        "biomarkers",
        "scores",
        "comorbidities",
        "disease_statuses",
        "organ_function_findings",
        "reproductive_statuses",
        "allergies",
        "precise_eligibility",
        "complex",
        "needs_clarification",
        "confidence",
    ],
}


class FinalRouteValidator:
    """Make the final fast-path decision after deterministic and LLM routing."""

    def __init__(self, confidence_threshold: float):
        self.confidence_threshold = confidence_threshold

    def decide(
        self,
        *,
        payload: dict[str, Any],
        confidence: float,
        search: SearchParameters | None,
        fallback: RouteDecision,
    ) -> RouteDecision:
        if (
            fallback.kind == RouteKind.CDE_SEARCH
            and fallback.search is not None
            and not fallback.fast_eligible
        ):
            return RouteDecision(
                RouteKind.CDE_SEARCH,
                max(confidence, fallback.confidence),
                "route_validator_rule_complex_to_agent",
                fast_eligible=False,
                search=search or fallback.search,
            )
        if bool(payload.get("complex")):
            return RouteDecision(
                RouteKind.CDE_SEARCH,
                confidence,
                "route_validator_complex_to_agent",
                fast_eligible=False,
                search=search,
            )
        if bool(payload.get("needs_clarification")):
            return RouteDecision(
                RouteKind.CDE_SEARCH,
                confidence,
                "route_validator_clarification_to_agent",
                fast_eligible=False,
                search=search,
            )
        if self._requires_agent_review(search):
            return RouteDecision(
                RouteKind.CDE_SEARCH,
                confidence,
                "route_validator_advanced_facts_to_agent",
                fast_eligible=False,
                search=search,
            )
        if confidence < self.confidence_threshold or search is None:
            return RouteDecision(
                RouteKind.CDE_SEARCH,
                confidence,
                "route_validator_low_confidence_to_agent",
                fast_eligible=False,
                search=search,
            )

        deterministic_fast = bool(fallback.fast_eligible and fallback.search is not None)
        return RouteDecision(
            RouteKind.CDE_SEARCH,
            max(confidence, fallback.confidence) if deterministic_fast else confidence,
            "route_validator_enriched_rule_fast_path" if deterministic_fast else "route_validator_fast_path",
            fast_eligible=True,
            search=search,
        )

    @staticmethod
    def _requires_agent_review(search: SearchParameters | None) -> bool:
        if search is None:
            return False
        return bool(search.complexity_reasons())


class LlmIntentRouter:
    """Optional structured extractor placed after hard privacy/CTR rules."""

    def __init__(self, config: OrchestrationConfig):
        self.config = config
        self._client: Any | None = None
        self._known_terms = self._load_known_terms()
        self._normalizer = SearchNormalizer(config.concepts_path)
        self._validator = FinalRouteValidator(config.llm_router_confidence_threshold)

    def should_resolve(self, text: str, fallback: RouteDecision) -> bool:
        if not self.config.llm_router_enabled:
            return False
        if fallback.kind in {
            RouteKind.PRIVACY_BLOCK,
            RouteKind.CDE_DETAIL,
            RouteKind.CDE_QUESTIONNAIRE,
            RouteKind.CDE_LIVE_VERIFY,
        }:
            return False
        if fallback.kind == RouteKind.CDE_SEARCH:
            return True
        if fallback.kind != RouteKind.GENERAL:
            return False
        return self._looks_like_general_cde_candidate(text)

    async def resolve(self, text: str, fallback: RouteDecision) -> RouteDecision:
        deterministic = self._deterministic_fast_decision(text, fallback)
        if deterministic is not None:
            return deterministic
        if not self.should_resolve(text, fallback):
            return fallback
        try:
            payload = await asyncio.wait_for(
                self._extract_with_retry(text),
                timeout=self.config.llm_router_timeout_seconds,
            )
            return self._decision_from_payload(text, payload, fallback)
        except asyncio.TimeoutError:
            logger.info("LLM router timed out; using deterministic fallback")
            return replace(fallback, reason=f"llm_timeout_rule_fallback:{fallback.reason}")
        except Exception as exc:
            logger.warning(
                "LLM router failed; using deterministic fallback (%s)",
                type(exc).__name__,
            )
            return replace(fallback, reason=f"llm_error_rule_fallback:{fallback.reason}")

    def _looks_like_general_cde_candidate(self, text: str) -> bool:
        normalized = (text or "").strip()
        if not normalized or normalized.casefold() in _SMALL_TALK:
            return False
        folded = normalized.casefold()
        if any(term.casefold() in folded for term in self._known_terms):
            return True
        if any(cue.casefold() in folded for cue in _GENERAL_CDE_CUES):
            return True
        return _PROJECT_CODE_PATTERN.search(normalized) is not None

    def _deterministic_fast_decision(
        self,
        text: str,
        fallback: RouteDecision,
    ) -> RouteDecision | None:
        """Keep obvious broad searches off the upstream model dependency.

        The deterministic router has already extracted the disease and basic
        filters. The mini model remains mandatory when the source text contains
        advanced patient facts, ambiguity, or a request that needs main-Agent
        review. This preserves semantic routing where it adds value while making
        the common fast path locally decidable and latency-bounded.
        """
        if (
            not self.config.llm_router_enabled
            or not self.config.llm_router_bypass_simple_fast
            or fallback.kind != RouteKind.CDE_SEARCH
            or not fallback.fast_eligible
            or fallback.search is None
        ):
            return None
        # The deterministic router has already resolved these auditable entities
        # from the user's text and the local database. Preserve them exactly:
        # a model must not turn a standalone variant into a disease, drop a
        # sponsor, or invent a gene relationship before the evidence query runs.
        if any((
            fallback.search.sponsors,
            fallback.search.biomarkers,
            fallback.search.site_names,
            fallback.search.project_codes,
        )):
            if FinalRouteValidator._requires_agent_review(fallback.search):
                return None
            return replace(fallback, reason="deterministic_entity_fast")
        normalized = self._normalizer.from_text(
            text,
            fallback=fallback.search,
        )
        if normalized is None or not normalized.disease.strip():
            return None
        # The deterministic router resolves auditable entity boundaries before
        # this bypass runs (for example disease="克罗恩病", city="广州").
        # Normalizing the original sentence again must not fold the location or
        # question grammar back into that disease ("广州地区克罗恩病").  The
        # second pass may enrich missing facts, but the already accepted disease
        # identity remains authoritative.
        normalized = replace(
            normalized,
            disease=fallback.search.disease,
            raw_disease=fallback.search.raw_disease or fallback.search.disease,
            canonical_disease=(
                fallback.search.canonical_disease or fallback.search.disease
            ),
            query_type=fallback.search.query_type,
            preserve_specificity=fallback.search.preserve_specificity,
        )
        if FinalRouteValidator._requires_agent_review(normalized):
            return None
        return RouteDecision(
            RouteKind.CDE_SEARCH,
            fallback.confidence,
            "deterministic_fast_normalized",
            fast_eligible=True,
            search=normalized,
        )

    def _load_known_terms(self) -> tuple[str, ...]:
        try:
            payload = json.loads(self.config.concepts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return ()
        terms: set[str] = set()
        for concept in payload.get("concepts", []):
            if not isinstance(concept, dict):
                continue
            values = [concept.get("canonical")]
            values.extend(concept.get("synonyms") or [])
            values.extend(concept.get("related") or [])
            for value in values:
                if isinstance(value, str) and len(value.strip()) >= 2:
                    terms.add(value.strip())
        return tuple(sorted(terms, key=len, reverse=True))

    async def _extract_with_retry(self, text: str) -> dict[str, Any]:
        attempts = self.config.llm_router_max_retries + 1
        for attempt in range(attempts):
            try:
                return await self._request_json(text)
            except Exception as exc:
                if attempt + 1 >= attempts or not self._retryable(exc):
                    raise
                logger.info("LLM router retrying once after retryable upstream status")
        raise RuntimeError("unreachable LLM router retry state")

    async def _request_json(self, text: str) -> dict[str, Any]:
        client = self._get_client()
        response = await client.responses.create(
            model=self.config.llm_router_model,
            reasoning={"effort": self.config.llm_router_reasoning_effort},
            input=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "angela_cde_route",
                    "strict": True,
                    "schema": _ROUTE_SCHEMA,
                }
            },
        )
        payload = json.loads(response.output_text)
        if not isinstance(payload, dict):
            raise ValueError("LLM router response must be a JSON object")
        return payload

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            api_key = os.getenv("OPENAI_API_KEY") or os.getenv("CUSTOM_API_KEY")
            base_url = os.getenv("OPENAI_BASE_URL")
            if not api_key or not base_url:
                raise RuntimeError("LLM router provider is not configured")
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=self.config.llm_router_timeout_seconds,
                max_retries=0,
            )
        return self._client

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        return status == 429 or (isinstance(status, int) and 500 <= status <= 599)

    def _decision_from_payload(
        self, text: str, payload: dict[str, Any], fallback: RouteDecision
    ) -> RouteDecision:
        confidence = self._confidence(payload.get("confidence"))
        if payload.get("intent") != "cde_search":
            if (
                fallback.kind == RouteKind.CDE_SEARCH
                and fallback.search is not None
                and not fallback.fast_eligible
            ):
                return replace(
                    fallback,
                    reason="llm_general_preserved_rule_complex",
                )
            return RouteDecision(
                RouteKind.GENERAL,
                confidence,
                "llm_router_general",
                fast_eligible=False,
            )

        return self._validator.decide(
            payload=payload,
            confidence=confidence,
            search=self._search_parameters(text, payload, fallback),
            fallback=fallback,
        )

    @staticmethod
    def _confidence(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be numeric")
        return min(1.0, max(0.0, float(value)))

    def _search_parameters(
        self, text: str, payload: dict[str, Any], fallback: RouteDecision
    ) -> SearchParameters | None:
        return self._normalizer.from_text(text, payload=payload, fallback=fallback.search)
