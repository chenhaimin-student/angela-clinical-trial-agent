from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .capability_router import CapabilityRouter
from .config import OrchestrationConfig
from .fast_path import LocalCdeFastPath
from .llm_router import LlmIntentRouter
from .models import FastPathResponse, RequestMode, RouteDecision, RouteKind
from .patient_facts import PatientFacts, PatientFactsBuilder
from .request_state import InvalidTransition, RequestRecord, RequestState, RequestStateStore
from .router import IntentRouter
from .scheduler import RequestScheduler
from .trace import TraceStore


logger = logging.getLogger("angela.orchestration")
Handler = Callable[[Any], Awaitable[Any]]
ApiHandler = Callable[..., Awaitable[tuple[Any, dict[str, Any]]]]


@dataclass
class _Runtime:
    config: OrchestrationConfig
    router: IntentRouter
    router_scheduler: RequestScheduler
    fast_scheduler: RequestScheduler
    agent_scheduler: RequestScheduler
    trace: TraceStore
    fast_path: LocalCdeFastPath
    llm_router: LlmIntentRouter
    request_state: RequestStateStore | None
    capability_router: CapabilityRouter


_runtime: _Runtime | None = None
_runtime_signature: tuple[Any, ...] | None = None


def _get_runtime() -> _Runtime:
    global _runtime, _runtime_signature
    config = OrchestrationConfig.from_env()
    signature = (
        config.enabled,
        config.fast_path_enabled,
        config.max_active,
        config.queue_capacity,
        config.router_max_active,
        config.router_queue_capacity,
        config.fast_max_active,
        config.fast_queue_capacity,
        config.agent_max_active,
        config.agent_queue_capacity,
        config.queue_timeout_seconds,
        config.duplicate_limit,
        config.router_duplicate_limit,
        config.fast_duplicate_limit,
        config.agent_duplicate_limit,
        config.llm_router_enabled,
        config.llm_router_model,
        config.llm_router_reasoning_effort,
        config.llm_router_timeout_seconds,
        config.llm_router_confidence_threshold,
        config.llm_router_max_retries,
        config.llm_router_bypass_simple_fast,
        config.capability_router_mode,
        str(config.database_path),
        str(config.concepts_path),
        str(config.trace_database_path),
        config.request_state_enabled,
        str(config.request_database_path),
    )
    if _runtime is None or signature != _runtime_signature:
        request_state = (
            RequestStateStore(config.request_database_path)
            if config.request_state_enabled
            else None
        )
        if request_state is not None:
            recovered = request_state.prepare_interrupted_for_replay()
            if recovered:
                logger.warning(
                    "prepared %d interrupted Angela request(s) for idempotent channel replay",
                    recovered,
                )
        _runtime = _Runtime(
            config=config,
            router=IntentRouter(config.concepts_path, database_path=config.database_path),
            router_scheduler=RequestScheduler(
                config.router_max_active,
                config.router_queue_capacity,
                config.queue_timeout_seconds,
                config.router_duplicate_limit,
            ),
            fast_scheduler=RequestScheduler(
                config.fast_max_active,
                config.fast_queue_capacity,
                config.queue_timeout_seconds,
                config.fast_duplicate_limit,
            ),
            agent_scheduler=RequestScheduler(
                config.agent_max_active,
                config.agent_queue_capacity,
                config.queue_timeout_seconds,
                config.agent_duplicate_limit,
            ),
            trace=TraceStore(config.trace_database_path),
            fast_path=LocalCdeFastPath(config),
            llm_router=LlmIntentRouter(config),
            request_state=request_state,
            capability_router=CapabilityRouter(config.capability_router_mode),
        )
        _runtime_signature = signature
    return _runtime


def reset_runtime_for_tests() -> None:
    global _runtime, _runtime_signature
    _runtime = None
    _runtime_signature = None


def _is_information_retrieval(search: Any) -> bool:
    """Structured public-record lookup must not invoke patient qualification."""
    return bool(
        search is not None
        and getattr(search, "request_mode", None) == RequestMode.INFORMATION_RETRIEVAL
    )


def _has_patient_context(search: Any) -> bool:
    return bool(
        search is not None
        and (
            getattr(search, "disease", "")
            or getattr(search, "age", None) is not None
            or getattr(search, "age_min", None) is not None
            or getattr(search, "age_max", None) is not None
            or getattr(search, "birth_year", None) is not None
            or getattr(search, "sex", "unknown") not in {"", "unknown"}
        )
    )


def _requires_disease_clarification(decision: RouteDecision) -> bool:
    """Do not ask the Agent to infer a disease for criteria-aware requests.

    A marker-only public-record lookup is valid information retrieval.  Once
    the request adds treatment, eligibility or other patient constraints,
    however, a disease is required before those constraints can be evaluated.
    """
    search = decision.search
    return bool(
        search is not None
        and not getattr(search, "disease", "")
        and not any((
            getattr(search, "biomarkers", ()),
            getattr(search, "sponsors", ()),
            getattr(search, "site_names", ()),
            getattr(search, "project_codes", ()),
        ))
    )


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "unknown")


def _event_message_id(event: Any) -> str | None:
    for owner in (event, getattr(event, "source", None)):
        value = getattr(owner, "message_id", None) if owner is not None else None
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _idempotency_key(
    *,
    channel: str,
    session_key: str,
    message_id: str | None,
    fallback_request_id: str,
) -> str:
    if message_id:
        digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:20]
        return f"channel:{channel}:{digest}:{message_id}"
    return f"ephemeral:{fallback_request_id}"


def _state_payload(text: str, route_hint: str) -> dict[str, Any]:
    encoded = text.encode("utf-8")
    return {
        "text_sha256": hashlib.sha256(encoded).hexdigest(),
        "text_bytes": len(encoded),
        "route_hint": route_hint,
        "raw_text_stored": False,
    }


def _duplicate_response(record: RequestRecord) -> str:
    if record.result and isinstance(record.result.get("text"), str):
        return str(record.result["text"])
    if record.state == RequestState.CANCELLED:
        return "该请求已取消，未执行查询。"
    if record.state == RequestState.FAILED:
        return "该消息此前处理失败，未重复执行。请重新发送一条新消息后再试。"
    labels = {
        RequestState.RECEIVED: "已接收",
        RequestState.PARSING: "正在理解请求",
        RequestState.WAITING_USER: "正在等待补充或确认信息",
        RequestState.READY: "已准备处理",
        RequestState.QUEUED: "正在排队",
        RequestState.RUNNING: "正在处理",
        RequestState.DELIVERING: "正在返回结果",
        RequestState.COMPLETED: "已完成",
    }
    label = labels.get(record.state, record.state.value)
    suffix = f"，记录的排队位置为 {record.queue_position}" if record.queue_position else ""
    return f"这条消息已经接收，当前状态：{label}{suffix}。系统不会重复执行。"


async def _begin_request_state(
    runtime: _Runtime,
    *,
    generated_request_id: str,
    idempotency_key: str,
    session_key: str,
    channel: str,
    text: str,
    route_hint: str,
) -> tuple[str, str | None, bool]:
    store = runtime.request_state
    if store is None:
        return generated_request_id, None, True
    record, created = await asyncio.to_thread(
        store.create_or_get,
        request_id=generated_request_id,
        idempotency_key=idempotency_key,
        session_key=session_key,
        channel=channel,
        payload=_state_payload(text, route_hint),
    )
    if created:
        await asyncio.to_thread(
            store.transition,
            record.request_id,
            RequestState.PARSING,
            stage="routing_and_patient_facts",
        )
        return record.request_id, None, True
    replayed, claimed = await asyncio.to_thread(store.claim_replayed, idempotency_key)
    if claimed and replayed is not None:
        return replayed.request_id, None, False
    return record.request_id, _duplicate_response(replayed or record), False


async def _request_transition(
    runtime: _Runtime,
    request_id: str,
    state: RequestState,
    **kwargs: Any,
) -> RequestRecord | None:
    if runtime.request_state is None:
        return None
    try:
        return await asyncio.to_thread(
            runtime.request_state.transition,
            request_id,
            state,
            **kwargs,
        )
    except (InvalidTransition, KeyError):
        logger.warning(
            "request-state transition ignored request=%s target=%s",
            request_id,
            state.value,
            exc_info=True,
        )
        return None


async def _complete_request_state(
    runtime: _Runtime,
    request_id: str,
    text: str,
) -> None:
    if runtime.request_state is None:
        return
    await _request_transition(
        runtime,
        request_id,
        RequestState.DELIVERING,
        stage="channel_delivery",
    )
    await _request_transition(
        runtime,
        request_id,
        RequestState.COMPLETED,
        stage="completed",
        result={"text": text},
    )


async def _fail_request_state(
    runtime: _Runtime,
    request_id: str,
    reason_code: str,
) -> None:
    await _request_transition(
        runtime,
        request_id,
        RequestState.FAILED,
        stage="failed",
        reason_code=reason_code,
        error={"code": reason_code},
    )


async def _record_queue_position(
    runtime: _Runtime,
    request_id: str,
    position: int,
) -> None:
    if runtime.request_state is None:
        return
    try:
        await asyncio.to_thread(
            runtime.request_state.update_queue_position,
            request_id,
            position,
        )
    except (InvalidTransition, KeyError, ValueError):
        logger.warning("failed to persist queue position", exc_info=True)


def _patient_facts_context(
    facts: PatientFacts | None,
    evidence: FastPathResponse | None = None,
) -> str | None:
    if facts is None:
        return None
    payload = {
        "schema_version": facts.schema_version,
        "birth_year": facts.birth_year.value,
        "age": {
            "minimum_years": facts.age.minimum_years,
            "maximum_years": facts.age.maximum_years,
            "status": facts.age.status.value,
            "as_of": facts.age.as_of.isoformat() if facts.age.as_of else None,
        },
        "sex": facts.sex.value,
        "diseases": list(facts.diseases),
        "canonical_diseases": list(facts.canonical_diseases),
        "disease_duration_months": facts.disease_duration_months.value,
        "disease_stage": facts.disease_stage.value,
        "treatment_line": facts.treatment_line.value,
        "prior_treatments": list(facts.prior_treatments),
        "biomarkers": list(facts.biomarkers),
        "scores": list(facts.scores),
        "comorbidities": list(facts.comorbidities),
        "disease_statuses": list(facts.disease_statuses),
        "organ_function_findings": list(facts.organ_function_findings),
        "reproductive_statuses": list(facts.reproductive_statuses),
        "allergies": list(facts.allergies),
        "city": facts.city,
        "region": facts.region,
        "lookback_months": facts.lookback_months,
        "precise_eligibility": facts.precise_eligibility,
        "missing": [issue.code for issue in facts.missing],
        "ambiguities": [issue.code for issue in facts.ambiguities],
    }
    context = (
        "[Angela internal PatientFacts; de-identified and deterministic]\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        + "\nUse these fields as the authoritative patient facts when calling CDE tools. "
        "Do not reinterpret a birth year as a registration year or age. Keep UNKNOWN unresolved, "
        "never return an EXPLICIT_CONFLICT trial, and do not give a final enrollment conclusion."
    )
    if evidence is not None:
        context += (
            "\n\n[Angela authoritative local CDE evidence]\n"
            + evidence.text
            + "\nUse this pre-fetched evidence for the answer. Do not call the CDE search tool again "
            "for this request. Do not change its result count, invent another CTR, or turn "
            "UNKNOWN into PASS. You may explain and organize the evidence in natural language."
        )
    return context


def _enforce_authoritative_evidence(
    response_text: str,
    evidence: FastPathResponse | None,
) -> str:
    """Reject model output that contradicts deterministic local CDE evidence."""
    if evidence is None or evidence.returned_count == 0:
        return response_text
    allowed = set(evidence.result_ids)
    mentioned = set(re.findall(r"CTR\d{6,}", response_text, flags=re.IGNORECASE))
    returned_claims = {
        int(value)
        for value in re.findall(
            r"(?:返回|保留|展示(?:并核对)?(?:了)?)\s*(\d+)\s*[条项]",
            response_text,
        )
    }
    contradicts_nonempty = bool(
        re.search(r"(?:未检索到|未找到可返回|返回\s*0\s*[条项]|匹配(?:结果)?为\s*0\s*[条项])", response_text)
    )
    count_mismatch = any(value != evidence.returned_count for value in returned_claims)
    if (
        not mentioned
        or not mentioned.issubset(allowed)
        or contradicts_nonempty
        or count_mismatch
    ):
        logger.warning(
            "agent response failed authoritative CDE evidence guard; using deterministic fallback"
        )
        return evidence.text
    required_terms = tuple(evidence.metadata.get("required_condition_terms") or ())
    missing_terms = [term for term in required_terms if str(term) not in response_text]
    condition_trace = str(evidence.metadata.get("condition_trace_text") or "").strip()
    if missing_terms and condition_trace and condition_trace not in response_text:
        logger.info(
            "agent response omitted %d supplied patient conditions; appending deterministic trace",
            len(missing_terms),
        )
        response_text = response_text.rstrip() + "\n\n" + condition_trace

    additions: list[str] = []
    if "CDE" not in response_text.upper():
        additions.append("数据来源：本地CDE数据库中的公开登记信息。")
    if not re.search(r"(?:UNKNOWN|待核对|尚待核实|信息不足|无法确认)", response_text, re.IGNORECASE):
        additions.append("公开原文未能核实的条件均记为 UNKNOWN（待核对），不能视为已经符合。")
    if not re.search(
        r"(?:最终入组(?:结论|决定)|不代表最终入组)",
        response_text,
    ):
        additions.append("本结果不代表最终入组结论，最终由研究医生和研究中心确认。")
    if additions:
        response_text = response_text.rstrip() + "\n\n" + "\n".join(additions)
    return response_text


_UPSTREAM_ERROR_PATTERN = re.compile(
    r"(?:\b(?:401|403|429|500|502|503|504)\b.*(?:error|unauthori[sz]ed|forbidden|"
    r"rate.?limit|service.?unavailable|bad.?gateway|timeout|failed|失败|不可用|鉴权|认证)"
    r"|(?:error|unauthori[sz]ed|forbidden|rate.?limit|service.?unavailable|"
    r"bad.?gateway|timeout|failed|失败|不可用|鉴权|认证).*\b(?:401|403|429|500|502|503|504)\b)",
    re.IGNORECASE | re.DOTALL,
)


def _safe_agent_response(
    response_text: str,
    evidence: FastPathResponse | None,
) -> str:
    """Keep provider/auth failures internal and preserve local CDE evidence."""
    text = str(response_text or "").strip()
    if text and not _UPSTREAM_ERROR_PATTERN.search(text):
        return _enforce_authoritative_evidence(text, evidence)
    if evidence is not None:
        return evidence.text
    return "当前服务暂时无法完成这次分析，请稍后重试。"


async def _prefetch_authoritative_evidence(
    runtime: _Runtime,
    decision: RouteDecision,
    patient_facts: PatientFacts | None,
    source_text: str,
) -> FastPathResponse | None:
    if (
        decision.kind != RouteKind.CDE_SEARCH
        or decision.search is None
        or patient_facts is None
    ):
        return None
    try:
        return await asyncio.to_thread(
            runtime.fast_path.search,
            decision.search,
            patient_facts=patient_facts,
            source_text=source_text,
        )
    except Exception as exc:
        logger.warning(
            "authoritative CDE evidence prefetch failed; continuing with main agent: %s",
            type(exc).__name__,
        )
        return None


async def _run_gateway_agent(
    unmanaged_handler: Handler,
    event: Any,
    facts: PatientFacts | None,
    evidence: FastPathResponse | None = None,
) -> Any:
    context = _patient_facts_context(facts, evidence)
    if not context or not hasattr(event, "channel_prompt"):
        return await unmanaged_handler(event)
    original_prompt = getattr(event, "channel_prompt", None)
    event.channel_prompt = "\n\n".join(
        value for value in (original_prompt, context) if value
    )
    try:
        return await unmanaged_handler(event)
    finally:
        event.channel_prompt = original_prompt


async def _run_api_agent(
    unmanaged_handler: ApiHandler,
    user_message: str,
    facts: PatientFacts | None,
    evidence: FastPathResponse | None = None,
) -> tuple[Any, dict[str, Any]]:
    context = _patient_facts_context(facts, evidence)
    parameters = inspect.signature(unmanaged_handler).parameters.values()
    accepts_override = any(
        parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        }
        for parameter in parameters
    )
    if context and accepts_override:
        contextual_message = f"{context}\n\n[Original user request]\n{user_message}"
        return await unmanaged_handler(contextual_message)
    return await unmanaged_handler()


def _should_bypass(gateway: Any, event: Any) -> bool:
    if bool(getattr(event, "internal", False)):
        return True
    command = event.get_command() if hasattr(event, "get_command") else None
    if command:
        return True
    source = getattr(event, "source", None)
    if source is None:
        return True
    try:
        if not gateway._is_user_authorized(source):
            return True
    except Exception:
        return True
    try:
        session_key = gateway._session_key_for_source(source)
        if session_key in getattr(gateway, "_running_agents", {}):
            return True
    except Exception:
        pass
    return False


async def _notify(gateway: Any, event: Any, text: str) -> None:
    try:
        adapter = gateway._adapter_for_source(event.source)
        if adapter is None:
            return
        metadata = gateway._thread_metadata_for_source(event.source)
        await adapter.send(event.source.chat_id, text, metadata=metadata)
    except Exception:
        logger.debug("queue notification failed", exc_info=True)


async def _persist_fast_turn(gateway: Any, event: Any, session_key: str, answer: str) -> None:
    try:
        entry = await gateway.async_session_store.get_or_create_session(event.source)
        timestamp = time.time()
        user_message = {"role": "user", "content": event.text or "", "timestamp": timestamp}
        assistant_message = {"role": "assistant", "content": answer, "timestamp": timestamp + 0.000001}
        await asyncio.to_thread(gateway.session_store.append_to_transcript, entry.session_id, user_message)
        await asyncio.to_thread(gateway.session_store.append_to_transcript, entry.session_id, assistant_message)
        await asyncio.to_thread(gateway.session_store.update_session, session_key)
        try:
            gateway._evict_cached_agent(session_key)
        except Exception:
            logger.debug("agent cache eviction after fast path failed", exc_info=True)
    except Exception:
        logger.warning("fast-path transcript persistence failed", exc_info=True)


async def _persist_api_fast_turn(adapter: Any, session_id: str | None, user_message: str, answer: str) -> None:
    if not session_id:
        return
    try:
        database = await adapter._ensure_session_db_async()
        if database is None:
            return

        def _write() -> None:
            database.ensure_session(session_id, source="api_server")
            database.append_message(session_id, "user", content=user_message, timestamp=time.time())
            database.append_message(session_id, "assistant", content=answer, timestamp=time.time())

        await asyncio.to_thread(_write)
    except Exception:
        logger.warning("API fast-path transcript persistence failed", exc_info=True)


async def _resolve_with_router_pool(
    runtime: _Runtime,
    text: str,
    fallback: RouteDecision,
    request_id: str,
    session_key: str,
    notify: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[RouteDecision, str | None]:
    if not runtime.llm_router.should_resolve(text, fallback):
        return fallback, None
    admission = await runtime.router_scheduler.acquire(request_id, session_key, notify=notify)
    if admission.rejection_text:
        return fallback, admission.rejection_text
    try:
        return await runtime.llm_router.resolve(text, fallback), None
    finally:
        if admission.lease is not None:
            await admission.lease.release()


async def _finish_rejected(
    runtime: _Runtime,
    request_id: str,
    received_at: float,
) -> None:
    now = time.time()
    await asyncio.to_thread(
        runtime.trace.finish,
        request_id,
        started_at=now,
        received_at=received_at,
        status="rejected",
        error_code="queue_rejected",
    )


async def handle_gateway_turn(gateway: Any, event: Any, unmanaged_handler: Handler) -> Any:
    runtime = _get_runtime()
    if not runtime.config.enabled or _should_bypass(gateway, event):
        return await unmanaged_handler(event)

    source = event.source
    session_key = gateway._session_key_for_source(source)
    decision = runtime.router.classify(event.text or "")
    generated_request_id = f"angela-{uuid4().hex}"
    received_at = time.time()
    platform = _platform_name(source)
    idempotency_key = _idempotency_key(
        channel=platform,
        session_key=session_key,
        message_id=_event_message_id(event),
        fallback_request_id=generated_request_id,
    )
    request_id, duplicate_response, trace_is_new = await _begin_request_state(
        runtime,
        generated_request_id=generated_request_id,
        idempotency_key=idempotency_key,
        session_key=session_key,
        channel=platform,
        text=event.text or "",
        route_hint=decision.kind.value,
    )
    if duplicate_response is not None:
        return duplicate_response
    fast_candidate = bool(
        runtime.config.fast_path_enabled and decision.fast_eligible and decision.search
    )
    if trace_is_new:
        await asyncio.to_thread(
            runtime.trace.start,
            request_id,
            session_key,
            platform,
            decision.kind.value,
            fast_candidate,
        )

    execution_started_at = time.time()
    try:
        if decision.kind == RouteKind.PRIVACY_BLOCK:
            response = (
                "检测到手机号、身份证号或邮箱等直接身份信息。请删除这些内容后，仅保留年龄、性别、"
                "疾病、病理分型、既往治疗、所在城市等脱敏条件再查询。"
            )
            await asyncio.to_thread(
                runtime.trace.finish,
                request_id,
                started_at=execution_started_at,
                received_at=received_at,
                status="privacy_blocked",
                error_code="direct_identifier_detected",
            )
            await _request_transition(
                runtime,
                request_id,
                RequestState.WAITING_USER,
                stage="privacy_redaction_required",
                reason_code="DIRECT_IDENTIFIER_DETECTED",
                result={"text": response},
            )
            return response

        if decision.kind == RouteKind.CDE_SEARCH and (
            decision.search is None or _requires_disease_clarification(decision)
        ):
            response = (
                "我识别到了临床试验检索意图，但还缺少具体疾病或肿瘤类型。"
                "请补充病种后再查；如果只有生物标志物，也请说明对应癌种或疾病。"
            )
            await asyncio.to_thread(
                runtime.trace.finish,
                request_id,
                started_at=execution_started_at,
                received_at=received_at,
                status="needs_clarification",
                error_code="missing_disease",
            )
            await _request_transition(
                runtime,
                request_id,
                RequestState.WAITING_USER,
                stage="disease_required",
                reason_code="MISSING_DISEASE",
                result={"text": response},
            )
            return response

        deterministic_decision = decision
        decision, rejection = await _resolve_with_router_pool(
            runtime,
            event.text or "",
            deterministic_decision,
            request_id,
            session_key,
            notify=lambda text: _notify(gateway, event, text),
        )
        if rejection:
            await _finish_rejected(runtime, request_id, received_at)
            await _fail_request_state(runtime, request_id, "ROUTER_QUEUE_REJECTED")
            return rejection
        capability_plan = runtime.capability_router.plan(
            event.text or "",
            decision,
            deterministic_decision=deterministic_decision,
        )
        decision = runtime.capability_router.apply(decision, capability_plan)
        fast = bool(
            runtime.config.fast_path_enabled and decision.fast_eligible and decision.search
        )
        patient_facts = (
            PatientFactsBuilder().build(event.text or "", decision.search)
            if (
                decision.kind == RouteKind.CDE_SEARCH
                and decision.search is not None
                and not _is_information_retrieval(decision.search)
                and _has_patient_context(decision.search)
            )
            else None
        )
        if patient_facts is not None and patient_facts.requires_clarification:
            execution_started_at = time.time()
            response = patient_facts.clarification_message() or "请先确认患者信息中的冲突后再查询。"
            await asyncio.to_thread(
                runtime.trace.finish,
                request_id,
                started_at=execution_started_at,
                received_at=received_at,
                status="needs_clarification",
                error_code="patient_facts_conflict",
            )
            await _request_transition(
                runtime,
                request_id,
                RequestState.WAITING_USER,
                stage="patient_facts_confirmation",
                reason_code="PATIENT_FACTS_CONFLICT",
                result={"text": response},
            )
            return response

        await _request_transition(
            runtime,
            request_id,
            RequestState.READY,
            stage="route_decided",
            metadata={
                "route": decision.kind.value,
                "fast_eligible": fast,
                "capability_router_mode": runtime.config.capability_router_mode,
                "capability_shadow": capability_plan.as_dict(),
                "capability_activated": bool(
                    fast and not capability_plan.legacy_fast_eligible
                ),
            },
        )

        if fast and decision.search is not None:
            await _request_transition(
                runtime,
                request_id,
                RequestState.QUEUED,
                stage="fast_path_admission",
            )
            admission = await runtime.fast_scheduler.acquire(
                request_id,
                session_key,
                notify=lambda text: _notify(gateway, event, text),
                on_queued=lambda position: _record_queue_position(runtime, request_id, position),
            )
            if admission.rejection_text:
                await _finish_rejected(runtime, request_id, received_at)
                await _fail_request_state(runtime, request_id, "FAST_QUEUE_REJECTED")
                return admission.rejection_text
            execution_started_at = time.time()
            await _request_transition(
                runtime,
                request_id,
                RequestState.RUNNING,
                stage="local_cde_query",
                metadata={"queue_wait_seconds": admission.queue_wait_seconds},
            )
            try:
                try:
                    result = await asyncio.to_thread(
                        runtime.fast_path.search,
                        decision.search,
                        patient_facts=patient_facts,
                        source_text=event.text or "",
                    )
                except Exception as exc:
                    logger.warning("fast path failed; falling back to Hermes: %s", type(exc).__name__)
                    await _request_transition(
                        runtime,
                        request_id,
                        RequestState.QUEUED,
                        stage="agent_fallback_admission",
                        reason_code="FAST_PATH_FALLBACK",
                        metadata={"error_type": type(exc).__name__},
                    )
                else:
                    await _persist_fast_turn(gateway, event, session_key, result.text)
                    await asyncio.to_thread(
                        runtime.trace.finish,
                        request_id,
                        started_at=execution_started_at,
                        received_at=received_at,
                        status="fast_path_success",
                        total_count=result.total_count,
                        returned_count=result.returned_count,
                    )
                    await _complete_request_state(runtime, request_id, result.text)
                    return result.text
            finally:
                if admission.lease is not None:
                    await admission.lease.release()

        if not fast:
            await _request_transition(
                runtime,
                request_id,
                RequestState.QUEUED,
                stage="agent_admission",
            )
        admission = await runtime.agent_scheduler.acquire(
            request_id,
            session_key,
            notify=lambda text: _notify(gateway, event, text),
            on_queued=lambda position: _record_queue_position(runtime, request_id, position),
        )
        if admission.rejection_text:
            await _finish_rejected(runtime, request_id, received_at)
            await _fail_request_state(runtime, request_id, "AGENT_QUEUE_REJECTED")
            return admission.rejection_text
        execution_started_at = time.time()
        await _request_transition(
            runtime,
            request_id,
            RequestState.RUNNING,
            stage="main_agent",
            metadata={"queue_wait_seconds": admission.queue_wait_seconds},
        )
        try:
            evidence = await _prefetch_authoritative_evidence(
                runtime,
                decision,
                patient_facts,
                event.text or "",
            )
            try:
                response = await _run_gateway_agent(
                    unmanaged_handler,
                    event,
                    patient_facts,
                    evidence,
                )
            except Exception as exc:
                logger.warning("main agent failed; using safe response: %s", type(exc).__name__)
                response = ""
            response_text = _safe_agent_response(str(response), evidence)
            await asyncio.to_thread(
                runtime.trace.finish,
                request_id,
                started_at=execution_started_at,
                received_at=received_at,
                status="agent_success",
                total_count=evidence.total_count if evidence is not None else None,
                returned_count=evidence.returned_count if evidence is not None else None,
            )
            await _complete_request_state(runtime, request_id, response_text)
            return response_text
        finally:
            if admission.lease is not None:
                await admission.lease.release()
    except Exception as exc:
        await _fail_request_state(runtime, request_id, type(exc).__name__)
        await asyncio.to_thread(
            runtime.trace.finish,
            request_id,
            started_at=execution_started_at,
            received_at=received_at,
            status="failed",
            error_code=type(exc).__name__,
        )
        raise


async def handle_api_server_turn(
    adapter: Any,
    user_message: str,
    conversation_history: list[dict[str, str]],
    unmanaged_handler: ApiHandler,
    *,
    session_id: str | None = None,
    gateway_session_key: str | None = None,
    message_id: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Apply the same queue and deterministic CDE path to Hermes' HTTP API."""
    del conversation_history  # History is loaded and persisted by the API adapter/session DB.
    runtime = _get_runtime()
    if not runtime.config.enabled:
        return await unmanaged_handler()

    decision = runtime.router.classify(user_message or "")
    generated_request_id = f"angela-{uuid4().hex}"
    session_key = gateway_session_key or session_id or generated_request_id
    received_at = time.time()
    idempotency_key = _idempotency_key(
        channel="api_server",
        session_key=session_key,
        message_id=message_id,
        fallback_request_id=generated_request_id,
    )
    request_id, duplicate_response, trace_is_new = await _begin_request_state(
        runtime,
        generated_request_id=generated_request_id,
        idempotency_key=idempotency_key,
        session_key=session_key,
        channel="api_server",
        text=user_message or "",
        route_hint=decision.kind.value,
    )
    if duplicate_response is not None:
        return {
            "final_response": duplicate_response,
            "session_id": session_id,
            "completed": True,
            "idempotent_replay": True,
        }, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    fast_candidate = bool(
        runtime.config.fast_path_enabled and decision.fast_eligible and decision.search
    )
    if trace_is_new:
        await asyncio.to_thread(
            runtime.trace.start,
            request_id,
            session_key,
            "api_server",
            decision.kind.value,
            fast_candidate,
        )
    execution_started_at = time.time()
    try:
        if decision.kind == RouteKind.PRIVACY_BLOCK:
            response = (
                "检测到手机号、身份证号或邮箱等直接身份信息。请删除这些内容后，仅保留年龄、性别、"
                "疾病、病理分型、既往治疗、所在城市等脱敏条件再查询。"
            )
            await asyncio.to_thread(
                runtime.trace.finish,
                request_id,
                started_at=execution_started_at,
                received_at=received_at,
                status="privacy_blocked",
                error_code="direct_identifier_detected",
            )
            await _request_transition(
                runtime,
                request_id,
                RequestState.WAITING_USER,
                stage="privacy_redaction_required",
                reason_code="DIRECT_IDENTIFIER_DETECTED",
                result={"text": response},
            )
            await _persist_api_fast_turn(adapter, session_id, user_message, response)
            return {"final_response": response, "session_id": session_id, "completed": True}, {
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            }

        if decision.kind == RouteKind.CDE_SEARCH and (
            decision.search is None or _requires_disease_clarification(decision)
        ):
            response_text = (
                "我识别到了临床试验检索意图，但还缺少具体疾病或肿瘤类型。"
                "请补充病种后再查；如果只有生物标志物，也请说明对应癌种或疾病。"
            )
            await asyncio.to_thread(
                runtime.trace.finish,
                request_id,
                started_at=execution_started_at,
                received_at=received_at,
                status="needs_clarification",
                error_code="missing_disease",
            )
            await _request_transition(
                runtime,
                request_id,
                RequestState.WAITING_USER,
                stage="disease_required",
                reason_code="MISSING_DISEASE",
                result={"text": response_text},
            )
            await _persist_api_fast_turn(adapter, session_id, user_message, response_text)
            return {"final_response": response_text, "session_id": session_id, "completed": True}, {
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            }

        deterministic_decision = decision
        decision, rejection = await _resolve_with_router_pool(
            runtime,
            user_message or "",
            deterministic_decision,
            request_id,
            session_key,
        )
        if rejection:
            await _finish_rejected(runtime, request_id, received_at)
            await _fail_request_state(runtime, request_id, "ROUTER_QUEUE_REJECTED")
            return {"final_response": rejection}, {
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            }
        capability_plan = runtime.capability_router.plan(
            user_message or "",
            decision,
            deterministic_decision=deterministic_decision,
        )
        decision = runtime.capability_router.apply(decision, capability_plan)
        fast = bool(
            runtime.config.fast_path_enabled and decision.fast_eligible and decision.search
        )
        patient_facts = (
            PatientFactsBuilder().build(user_message or "", decision.search)
            if (
                decision.kind == RouteKind.CDE_SEARCH
                and decision.search is not None
                and not _is_information_retrieval(decision.search)
                and _has_patient_context(decision.search)
            )
            else None
        )
        if patient_facts is not None and patient_facts.requires_clarification:
            execution_started_at = time.time()
            response_text = patient_facts.clarification_message() or "请先确认患者信息中的冲突后再查询。"
            await asyncio.to_thread(
                runtime.trace.finish,
                request_id,
                started_at=execution_started_at,
                received_at=received_at,
                status="needs_clarification",
                error_code="patient_facts_conflict",
            )
            await _request_transition(
                runtime,
                request_id,
                RequestState.WAITING_USER,
                stage="patient_facts_confirmation",
                reason_code="PATIENT_FACTS_CONFLICT",
                result={"text": response_text},
            )
            await _persist_api_fast_turn(adapter, session_id, user_message, response_text)
            return {"final_response": response_text, "session_id": session_id, "completed": True}, {
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            }

        await _request_transition(
            runtime,
            request_id,
            RequestState.READY,
            stage="route_decided",
            metadata={
                "route": decision.kind.value,
                "fast_eligible": fast,
                "capability_router_mode": runtime.config.capability_router_mode,
                "capability_shadow": capability_plan.as_dict(),
                "capability_activated": bool(
                    fast and not capability_plan.legacy_fast_eligible
                ),
            },
        )

        if fast and decision.search is not None:
            await _request_transition(
                runtime,
                request_id,
                RequestState.QUEUED,
                stage="fast_path_admission",
            )
            admission = await runtime.fast_scheduler.acquire(
                request_id,
                session_key,
                on_queued=lambda position: _record_queue_position(runtime, request_id, position),
            )
            if admission.rejection_text:
                await _finish_rejected(runtime, request_id, received_at)
                await _fail_request_state(runtime, request_id, "FAST_QUEUE_REJECTED")
                return {"final_response": admission.rejection_text}, {
                    "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                }
            execution_started_at = time.time()
            await _request_transition(
                runtime,
                request_id,
                RequestState.RUNNING,
                stage="local_cde_query",
                metadata={"queue_wait_seconds": admission.queue_wait_seconds},
            )
            try:
                try:
                    result = await asyncio.to_thread(
                        runtime.fast_path.search,
                        decision.search,
                        patient_facts=patient_facts,
                        source_text=user_message or "",
                    )
                except Exception as exc:
                    logger.warning("API fast path failed; falling back to Hermes: %s", type(exc).__name__)
                    await _request_transition(
                        runtime,
                        request_id,
                        RequestState.QUEUED,
                        stage="agent_fallback_admission",
                        reason_code="FAST_PATH_FALLBACK",
                        metadata={"error_type": type(exc).__name__},
                    )
                else:
                    await _persist_api_fast_turn(adapter, session_id, user_message, result.text)
                    await asyncio.to_thread(
                        runtime.trace.finish,
                        request_id,
                        started_at=execution_started_at,
                        received_at=received_at,
                        status="fast_path_success",
                        total_count=result.total_count,
                        returned_count=result.returned_count,
                    )
                    await _complete_request_state(runtime, request_id, result.text)
                    return {"final_response": result.text, "session_id": session_id, "completed": True}, {
                        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                    }
            finally:
                if admission.lease is not None:
                    await admission.lease.release()

        if not fast:
            await _request_transition(
                runtime,
                request_id,
                RequestState.QUEUED,
                stage="agent_admission",
            )
        admission = await runtime.agent_scheduler.acquire(
            request_id,
            session_key,
            on_queued=lambda position: _record_queue_position(runtime, request_id, position),
        )
        if admission.rejection_text:
            await _finish_rejected(runtime, request_id, received_at)
            await _fail_request_state(runtime, request_id, "AGENT_QUEUE_REJECTED")
            return {"final_response": admission.rejection_text}, {
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            }
        execution_started_at = time.time()
        await _request_transition(
            runtime,
            request_id,
            RequestState.RUNNING,
            stage="main_agent",
            metadata={"queue_wait_seconds": admission.queue_wait_seconds},
        )
        try:
            evidence = await _prefetch_authoritative_evidence(
                runtime,
                decision,
                patient_facts,
                user_message or "",
            )
            try:
                response, usage = await _run_api_agent(
                    unmanaged_handler,
                    user_message,
                    patient_facts,
                    evidence,
                )
            except Exception as exc:
                logger.warning("API main agent failed; using safe response: %s", type(exc).__name__)
                response, usage = "", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            response_text = (
                str(response.get("final_response") or "")
                if isinstance(response, dict)
                else str(response)
            )
            guarded_text = _safe_agent_response(response_text, evidence)
            if isinstance(response, dict):
                response = dict(response)
                response["final_response"] = guarded_text
            else:
                response = {
                    "final_response": guarded_text,
                    "session_id": session_id,
                    "completed": True,
                }
            await asyncio.to_thread(
                runtime.trace.finish,
                request_id,
                started_at=execution_started_at,
                received_at=received_at,
                status="agent_success",
                total_count=evidence.total_count if evidence is not None else None,
                returned_count=evidence.returned_count if evidence is not None else None,
            )
            await _complete_request_state(runtime, request_id, guarded_text)
            return response, usage
        finally:
            if admission.lease is not None:
                await admission.lease.release()
    except Exception as exc:
        await _fail_request_state(runtime, request_id, type(exc).__name__)
        await asyncio.to_thread(
            runtime.trace.finish,
            request_id,
            started_at=execution_started_at,
            received_at=received_at,
            status="failed",
            error_code=type(exc).__name__,
        )
        raise
