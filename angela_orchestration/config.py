from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _truthy(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _choice(name: str, default: str, choices: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    if value not in choices:
        return default
    return value


@dataclass(frozen=True)
class OrchestrationConfig:
    enabled: bool
    fast_path_enabled: bool
    max_active: int
    queue_capacity: int
    router_max_active: int
    router_queue_capacity: int
    fast_max_active: int
    fast_queue_capacity: int
    agent_max_active: int
    agent_queue_capacity: int
    queue_timeout_seconds: int
    duplicate_limit: int
    router_duplicate_limit: int
    fast_duplicate_limit: int
    agent_duplicate_limit: int
    llm_router_enabled: bool
    llm_router_model: str
    llm_router_reasoning_effort: str
    llm_router_timeout_seconds: float
    llm_router_confidence_threshold: float
    llm_router_max_retries: int
    llm_router_bypass_simple_fast: bool
    capability_router_mode: str
    project_root: Path
    database_path: Path
    concepts_path: Path
    trace_database_path: Path
    request_state_enabled: bool
    request_database_path: Path

    @classmethod
    def from_env(cls) -> "OrchestrationConfig":
        default_project_root = Path(__file__).resolve().parent.parent
        project_root = Path(
            os.getenv("ANGELA_PROJECT_ROOT", str(default_project_root))
        ).expanduser().resolve()
        runtime_root = Path(
            os.getenv("ANGELA_RUNTIME_DIR", str(project_root / ".runtime"))
        ).expanduser().resolve()
        database_path = Path(
            os.getenv(
                "ANGELA_CDE_DATABASE",
                str(runtime_root / "cde_trials.sqlite3"),
            )
        ).expanduser().resolve()
        concepts_path = Path(
            os.getenv(
                "ANGELA_DISEASE_CONCEPTS",
                str(project_root / "cde-data/config/disease_concepts.json"),
            )
        ).expanduser().resolve()
        trace_path = Path(
            os.getenv(
                "ANGELA_ORCHESTRATION_TRACE_DB",
                str(runtime_root / "angela-orchestration.db"),
            )
        ).expanduser().resolve()
        request_database_path = Path(
            os.getenv(
                "ANGELA_REQUEST_DATABASE",
                str(runtime_root / "angela-runtime.db"),
            )
        ).expanduser().resolve()
        return cls(
            enabled=_truthy("ANGELA_ORCHESTRATION_ENABLED", False),
            fast_path_enabled=_truthy("ANGELA_FAST_PATH_ENABLED", True),
            max_active=_integer("ANGELA_MAX_ACTIVE_REQUESTS", 8, 1, 64),
            queue_capacity=_integer("ANGELA_QUEUE_CAPACITY", 24, 0, 500),
            router_max_active=_integer("ANGELA_ROUTER_MAX_ACTIVE_REQUESTS", 16, 1, 64),
            router_queue_capacity=_integer("ANGELA_ROUTER_QUEUE_CAPACITY", 16, 0, 500),
            fast_max_active=_integer("ANGELA_FAST_MAX_ACTIVE_REQUESTS", 16, 1, 64),
            fast_queue_capacity=_integer("ANGELA_FAST_QUEUE_CAPACITY", 16, 0, 500),
            agent_max_active=_integer("ANGELA_AGENT_MAX_ACTIVE_REQUESTS", 8, 1, 64),
            agent_queue_capacity=_integer("ANGELA_AGENT_QUEUE_CAPACITY", 24, 0, 500),
            queue_timeout_seconds=_integer("ANGELA_QUEUE_TIMEOUT_SECONDS", 90, 1, 900),
            duplicate_limit=_integer("ANGELA_DUPLICATE_PENDING_LIMIT", 1, 1, 10),
            router_duplicate_limit=_integer("ANGELA_ROUTER_DUPLICATE_PENDING_LIMIT", 2, 1, 10),
            fast_duplicate_limit=_integer("ANGELA_FAST_DUPLICATE_PENDING_LIMIT", 2, 1, 10),
            agent_duplicate_limit=_integer("ANGELA_AGENT_DUPLICATE_PENDING_LIMIT", 1, 1, 10),
            llm_router_enabled=_truthy("ANGELA_LLM_ROUTER_ENABLED", False),
            llm_router_model=os.getenv("ANGELA_LLM_ROUTER_MODEL", "gpt-5.4-mini").strip()
            or "gpt-5.4-mini",
            llm_router_reasoning_effort=os.getenv(
                "ANGELA_LLM_ROUTER_REASONING_EFFORT", "low"
            ).strip().lower()
            or "low",
            llm_router_timeout_seconds=_number(
                "ANGELA_LLM_ROUTER_TIMEOUT_SECONDS", 10.0, 0.1, 30.0
            ),
            llm_router_confidence_threshold=_number(
                "ANGELA_LLM_ROUTER_CONFIDENCE_THRESHOLD", 0.85, 0.0, 1.0
            ),
            llm_router_max_retries=_integer(
                "ANGELA_LLM_ROUTER_MAX_RETRIES", 1, 0, 1
            ),
            llm_router_bypass_simple_fast=_truthy(
                "ANGELA_LLM_ROUTER_BYPASS_SIMPLE_FAST", True
            ),
            capability_router_mode=_choice(
                "ANGELA_CAPABILITY_ROUTER_MODE",
                "shadow",
                {"off", "shadow", "partial", "on"},
            ),
            project_root=project_root,
            database_path=database_path,
            concepts_path=concepts_path,
            trace_database_path=trace_path,
            request_state_enabled=_truthy("ANGELA_REQUEST_STATE_ENABLED", False),
            request_database_path=request_database_path,
        )
