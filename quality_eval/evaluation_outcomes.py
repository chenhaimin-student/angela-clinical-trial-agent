from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Iterable


class EvaluationOutcome(str, Enum):
    PASS_FAST = "PASS_FAST"
    PASS_COMPLEX = "PASS_COMPLEX"
    UNKNOWN_EVIDENCE = "UNKNOWN_EVIDENCE"
    FAIL = "FAIL"


HARD_FAILURE_PREFIXES = (
    "http_",
    "no_response",
    "missing_answer",
    "returned_count_exceeds_limit",
    "missing_condition_trace:",
    "returned_ctr_missing_from_database",
    "returned_ctr_not_recruitable",
    "returned_ctr_outside_query_contract:",
    "explicit_conflict_leakage:",
    "false_empty_answer",
    "疾病解析与原问题不一致:",
    "returned_fact_mismatch:",
)

SOFT_EVIDENCE_PREFIXES = (
    "复杂附加条件无法仅凭疾病级Oracle确认是否漏召回",
    "no_auditable_disease_query",
    "无法从原问题建立可审计疾病查询",
)

UNKNOWN_LANGUAGE = re.compile(
    r"(?:无法|不能|暂时不能|尚不能).{0,16}(?:确认|判断|核实)|"
    r"(?:信息|证据|资料).{0,8}(?:不足|缺失|不完整)|"
    r"(?:未公开|没有公开).{0,12}(?:标准|条件|信息|证据)|"
    r"需要.{0,12}(?:补充|核对|确认)|"
    r"暂未找到.{0,24}(?:完全|同时|精确|明确)?(?:符合|满足)",
    re.IGNORECASE,
)
CTR_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])CTR\d{6,}(?:_p)?(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OutcomeDecision:
    outcome: EvaluationOutcome
    answered: bool
    route: str
    hard_failures: tuple[str, ...]
    evidence_gaps: tuple[str, ...]
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["outcome"] = self.outcome.value
        return payload


def classify_outcome(
    *,
    route: str | None,
    answer: str | None,
    issues: Iterable[str] = (),
    http_status: int = 200,
    request_error: str | None = None,
) -> OutcomeDecision:
    """Classify a completed answer without conflating uncertainty with error.

    PASS means the route-appropriate hard checks passed.  UNKNOWN_EVIDENCE is
    reserved for an answered complex request that explicitly preserves an
    evidence boundary.  Incorrect facts, conflicts, false empty answers,
    transport failures and blank responses always remain FAIL.
    """
    normalized_route = str(route or "unknown")
    text = str(answer or "").strip()
    normalized_issues = tuple(str(value) for value in issues if str(value))
    operational: list[str] = []
    if http_status != 200:
        operational.append(f"http_{http_status}")
    if request_error:
        operational.append(str(request_error))
    if not text:
        operational.append("no_response")

    all_issues = tuple(dict.fromkeys((*operational, *normalized_issues)))
    hard = tuple(
        issue for issue in all_issues
        if issue.startswith(HARD_FAILURE_PREFIXES)
    )
    soft = tuple(
        issue for issue in all_issues
        if issue.startswith(SOFT_EVIDENCE_PREFIXES)
    )
    unclassified = tuple(
        issue for issue in all_issues if issue not in hard and issue not in soft
    )

    if hard or unclassified:
        failures = tuple(dict.fromkeys((*hard, *unclassified)))
        return OutcomeDecision(
            EvaluationOutcome.FAIL,
            bool(text),
            normalized_route,
            failures,
            soft,
            "存在传输、事实、资格冲突或未归类的验收错误",
        )

    if normalized_route == "local_cde_query":
        if soft:
            return OutcomeDecision(
                EvaluationOutcome.FAIL,
                True,
                normalized_route,
                soft,
                (),
                "快速通道不允许带着未解析的证据缺口直接通过",
            )
        return OutcomeDecision(
            EvaluationOutcome.PASS_FAST,
            True,
            normalized_route,
            (),
            (),
            "快速查询回答及数据库硬约束全部通过",
        )

    if normalized_route == "main_agent":
        language_gap = bool(UNKNOWN_LANGUAGE.search(text))
        grounded_trials = bool(CTR_PATTERN.search(text))
        if soft or (language_gap and not grounded_trials):
            gaps = soft or ("answer_explicitly_preserves_unknown_evidence",)
            return OutcomeDecision(
                EvaluationOutcome.UNKNOWN_EVIDENCE,
                True,
                normalized_route,
                (),
                gaps,
                "复杂问题已回答，但没有足够的可核验试验证据，并明确保留为未知",
            )
        return OutcomeDecision(
            EvaluationOutcome.PASS_COMPLEX,
            True,
            normalized_route,
            (),
            (),
            "复杂通道回答通过可自动执行的事实与冲突校验",
        )

    return OutcomeDecision(
        EvaluationOutcome.FAIL,
        bool(text),
        normalized_route,
        ("unknown_execution_route",),
        (),
        "无法确认请求实际由哪个业务通道执行",
    )
