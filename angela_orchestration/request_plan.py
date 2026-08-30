from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskKind(str, Enum):
    FIND_TRIALS = "find_trials"
    COMPARE_RECORDS = "compare_records"
    CHECK_CRITERIA = "check_criteria"
    ASSESS_PATIENT = "assess_patient"
    GENERATE_QUESTIONNAIRE = "generate_questionnaire"
    OPEN_ANALYSIS = "open_analysis"
    GENERAL = "general"


class ExecutionCapability(str, Enum):
    SQL_FILTER = "sql_filter"
    ELIGIBILITY_GUARD = "eligibility_guard"
    EVIDENCE_FILTER = "evidence_filter"
    AGENT_REASONING = "agent_reasoning"
    CHANNEL_BYPASS = "channel_bypass"


class ExecutionPlan(str, Enum):
    LOCAL_SQL = "local_sql"
    LOCAL_GUARDED = "local_guarded"
    LOCAL_EVIDENCE_FILTER = "local_evidence_filter"
    MAIN_AGENT = "main_agent"
    CHANNEL_BYPASS = "channel_bypass"


@dataclass(frozen=True)
class QueryConstraint:
    field: str
    operator: str
    value: Any
    capability: ExecutionCapability
    source: str = "structured_search"

    def as_dict(self) -> dict[str, Any]:
        value = list(self.value) if isinstance(self.value, tuple) else self.value
        return {
            "field": self.field,
            "operator": self.operator,
            "value": value,
            "capability": self.capability.value,
            "source": self.source,
        }


@dataclass(frozen=True)
class RequestPlan:
    task: TaskKind
    execution: ExecutionPlan
    constraints: tuple[QueryConstraint, ...] = ()
    unsupported_constraints: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    legacy_fast_eligible: bool = False
    mode: str = "shadow"
    ready: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def differs_from_legacy(self) -> bool:
        suggested_fast = self.execution in {
            ExecutionPlan.LOCAL_SQL,
            ExecutionPlan.LOCAL_GUARDED,
            ExecutionPlan.LOCAL_EVIDENCE_FILTER,
        }
        return suggested_fast != self.legacy_fast_eligible

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.value,
            "execution": self.execution.value,
            "constraints": [item.as_dict() for item in self.constraints],
            "unsupported_constraints": list(self.unsupported_constraints),
            "reasons": list(self.reasons),
            "legacy_fast_eligible": self.legacy_fast_eligible,
            "differs_from_legacy": self.differs_from_legacy,
            "mode": self.mode,
            "ready": self.ready,
            "metadata": self.metadata,
        }
