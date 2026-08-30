from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class RetrievalMetrics:
    relevant_returned: int
    irrelevant_returned: int
    unjudged_returned: int
    relevant_total: int
    precision: float | None
    recall: float | None
    label_coverage: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def retrieval_metrics(returned: Iterable[str], labels: dict[str, bool]) -> RetrievalMetrics:
    result_ids = tuple(dict.fromkeys(str(value).upper() for value in returned if value))
    relevant = {ctr for ctr, value in labels.items() if value}
    irrelevant = {ctr for ctr, value in labels.items() if not value}
    relevant_returned = sum(ctr in relevant for ctr in result_ids)
    irrelevant_returned = sum(ctr in irrelevant for ctr in result_ids)
    unjudged = len(result_ids) - relevant_returned - irrelevant_returned
    judged_returned = relevant_returned + irrelevant_returned
    return RetrievalMetrics(
        relevant_returned=relevant_returned,
        irrelevant_returned=irrelevant_returned,
        unjudged_returned=unjudged,
        relevant_total=len(relevant),
        precision=(relevant_returned / judged_returned) if judged_returned else None,
        recall=(relevant_returned / len(relevant)) if relevant else None,
        label_coverage=(judged_returned / len(result_ids)) if result_ids else 1.0,
    )


def safe_ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def threshold_status(value: float | None, threshold: float, *, labels_ready: bool = True) -> str:
    if not labels_ready or value is None:
        return "NOT_READY"
    return "PASS" if value >= threshold else "FAIL"
