from __future__ import annotations

import os
import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from angela_orchestration.gateway import (
    _enforce_authoritative_evidence,
    _is_information_retrieval,
    _safe_agent_response,
    handle_api_server_turn,
    handle_gateway_turn,
    reset_runtime_for_tests,
)
from angela_orchestration.models import FastPathResponse, RequestMode, SearchParameters


class _Platform:
    value = "dingtalk"


@dataclass
class _Source:
    platform: object = _Platform()
    chat_id: str = "chat-one"
    user_id: str = "user-one"


@dataclass
class _Event:
    text: str
    source: _Source
    internal: bool = False
    message_id: str | None = None
    channel_prompt: str | None = None

    def get_command(self):
        return self.text[1:].split(maxsplit=1)[0] if self.text.startswith("/") else None


@dataclass
class _Entry:
    session_id: str = "session-id"


class _AsyncStore:
    async def get_or_create_session(self, source):
        return _Entry()


class _Store:
    def __init__(self):
        self.messages = []
        self.updated = []

    def append_to_transcript(self, session_id, message):
        self.messages.append((session_id, message))

    def update_session(self, session_key):
        self.updated.append(session_key)


class _Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata))


class _ApiDatabase:
    def __init__(self):
        self.messages = []

    def ensure_session(self, session_id, source="unknown"):
        return session_id

    def append_message(self, session_id, role, content=None, timestamp=None):
        self.messages.append((session_id, role, content))


class _ApiAdapter:
    def __init__(self):
        self.database = _ApiDatabase()

    async def _ensure_session_db_async(self):
        return self.database


class _Gateway:
    def __init__(self):
        self.async_session_store = _AsyncStore()
        self.session_store = _Store()
        self.adapter = _Adapter()
        self._running_agents = {}
        self.evicted = []

    def _is_user_authorized(self, source):
        return True

    def _session_key_for_source(self, source):
        return f"{source.platform.value}:{source.chat_id}:{source.user_id}"

    def _adapter_for_source(self, source):
        return self.adapter

    def _thread_metadata_for_source(self, source):
        return None

    def _evict_cached_agent(self, session_key):
        self.evicted.append(session_key)


class GatewayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_upstream_error_text_is_not_exposed_when_local_evidence_exists(self) -> None:
        evidence = FastPathResponse(
            text="本地结果：CTR20260001",
            total_count=1,
            returned_count=1,
            result_ids=("CTR20260001",),
        )
        self.assertEqual(
            _safe_agent_response("HTTP 503 Service Unavailable", evidence),
            evidence.text,
        )

    def test_upstream_error_without_evidence_uses_friendly_message(self) -> None:
        answer = _safe_agent_response("401 Unauthorized: invalid api key", None)
        self.assertNotIn("401", answer)
        self.assertNotIn("api key", answer.casefold())

    def test_company_variant_discovery_is_not_forced_into_patient_clarification(self) -> None:
        self.assertTrue(_is_information_retrieval(SearchParameters(
            disease="肺癌", sponsors=("星河",), biomarkers=("G12C",),
            request_mode=RequestMode.INFORMATION_RETRIEVAL,
        )))
        self.assertFalse(_is_information_retrieval(SearchParameters(
            disease="肺癌", biomarkers=("G12C",),
            request_mode=RequestMode.PATIENT_MATCHING,
        )))

    def test_project_code_criteria_request_does_not_require_patient_disease(self) -> None:
        search = SearchParameters(
            project_codes=("EGFR-PACC",),
            request_mode=RequestMode.CRITERIA_ANALYSIS,
        )
        from angela_orchestration.gateway import _has_patient_context
        self.assertFalse(_has_patient_context(search))

    async def asyncSetUp(self) -> None:
        self.saved_env = dict(os.environ)
        self.tempdir = tempfile.TemporaryDirectory()
        project = Path(os.environ.get("ANGELA_TEST_PROJECT_ROOT", ""))
        database = Path(os.environ.get("ANGELA_TEST_DATABASE", ""))
        if not project.is_dir() or not database.is_file():
            self.skipTest("isolated CDE database was not supplied")
        os.environ.update({
            "ANGELA_ORCHESTRATION_ENABLED": "1",
            "ANGELA_FAST_PATH_ENABLED": "1",
            "ANGELA_LLM_ROUTER_ENABLED": "0",
            "ANGELA_MAX_ACTIVE_REQUESTS": "1",
            "ANGELA_FAST_MAX_ACTIVE_REQUESTS": "1",
            "ANGELA_AGENT_MAX_ACTIVE_REQUESTS": "1",
            "ANGELA_PROJECT_ROOT": str(project),
            "ANGELA_CDE_DATABASE": str(database),
            "ANGELA_DISEASE_CONCEPTS": str(project / "cde-data/config/disease_concepts.json"),
            "ANGELA_ORCHESTRATION_TRACE_DB": str(Path(self.tempdir.name) / "trace.sqlite3"),
            "ANGELA_REQUEST_STATE_ENABLED": "1",
            "ANGELA_REQUEST_DATABASE": str(Path(self.tempdir.name) / "runtime.sqlite3"),
        })
        reset_runtime_for_tests()

    async def asyncTearDown(self) -> None:
        reset_runtime_for_tests()
        os.environ.clear()
        os.environ.update(self.saved_env)
        self.tempdir.cleanup()

    async def test_fast_search_skips_agent_and_persists_history(self) -> None:
        gateway = _Gateway()
        calls = []

        async def unmanaged(event):
            calls.append(event.text)
            return "agent"

        answer = await handle_gateway_turn(
            gateway,
            _Event("请查CDE：男女不限，40岁，SLE，近半年，返回5条", _Source()),
            unmanaged,
        )
        self.assertIn("本地CDE数据库初步匹配", answer)
        self.assertEqual(calls, [])
        self.assertEqual([item[1]["role"] for item in gateway.session_store.messages], ["user", "assistant"])
        self.assertEqual(len(gateway.evicted), 1)

    async def test_general_ctr_and_commands_remain_on_hermes(self) -> None:
        gateway = _Gateway()
        calls = []

        async def unmanaged(event):
            calls.append(event.text)
            return "agent-response"

        prompts = ("你好", "为CTR20261056生成问卷", "/new")
        for prompt in prompts:
            answer = await handle_gateway_turn(gateway, _Event(prompt, _Source()), unmanaged)
            self.assertEqual(answer, "agent-response")
        self.assertEqual(calls, list(prompts))

    async def test_database_failure_falls_back_to_agent(self) -> None:
        os.environ["ANGELA_CDE_DATABASE"] = str(Path(self.tempdir.name) / "missing.sqlite3")
        reset_runtime_for_tests()
        gateway = _Gateway()

        async def unmanaged(event):
            return "safe-agent-fallback"

        answer = await handle_gateway_turn(
            gateway,
            _Event("男性40岁肺癌临床试验", _Source()),
            unmanaged,
        )
        self.assertEqual(answer, "safe-agent-fallback")

    async def test_direct_identifier_is_blocked_before_query(self) -> None:
        gateway = _Gateway()

        async def unmanaged(event):
            self.fail("privacy block must not call the agent")

        answer = await handle_gateway_turn(
            gateway,
            _Event("肺癌，手机号13800138000，请查临床试验", _Source()),
            unmanaged,
        )
        self.assertIn("删除这些内容", answer)

    async def test_biomarker_only_search_queries_first_without_forcing_disease(self) -> None:
        gateway = _Gateway()

        async def unmanaged(event):
            return "agent-result"

        answer = await handle_gateway_turn(
            gateway,
            _Event("目前初治 KRAS G12C研究有哪些？", _Source()),
            unmanaged,
        )
        self.assertNotIn("缺少具体疾病", answer)

    async def test_api_server_fast_search_returns_agent_result_shape(self) -> None:
        calls = []
        adapter = _ApiAdapter()

        async def unmanaged():
            calls.append(True)
            return {"final_response": "agent"}, {"total_tokens": 1}

        result, usage = await handle_api_server_turn(
            adapter,
            "请查CDE：40岁，SLE，返回5条",
            [],
            unmanaged,
            session_id="api-session-one",
        )
        self.assertIn("本地CDE数据库初步匹配", result["final_response"])
        self.assertEqual(usage["total_tokens"], 0)
        self.assertEqual(calls, [])
        self.assertEqual([item[1] for item in adapter.database.messages], ["user", "assistant"])

    async def test_api_server_general_message_uses_agent(self) -> None:
        async def unmanaged():
            return {"final_response": "你好"}, {"total_tokens": 8}

        result, usage = await handle_api_server_turn(
            object(), "你好", [], unmanaged, session_id="api-session-two"
        )
        self.assertEqual(result["final_response"], "你好")
        self.assertEqual(usage["total_tokens"], 8)

    async def test_api_agent_failure_returns_http_compatible_safe_shape(self) -> None:
        async def unmanaged():
            raise RuntimeError("HTTP 503 Service Unavailable")

        result, usage = await handle_api_server_turn(
            object(), "你好", [], unmanaged, session_id="api-safe-fallback"
        )
        self.assertIsInstance(result, dict)
        self.assertIn("稍后重试", result["final_response"])
        self.assertNotIn("503", result["final_response"])
        self.assertEqual(usage["total_tokens"], 0)

    async def test_complex_api_request_injects_authoritative_patient_facts(self) -> None:
        seen = []

        async def unmanaged(override_user_message):
            seen.append(override_user_message)
            return {"final_response": "复杂请求已处理"}, {"total_tokens": 8}

        result, _ = await handle_api_server_turn(
            object(),
            "52岁男性SLE，SLEDAI得分大于12分，请判断是否适合临床试验",
            [],
            unmanaged,
            session_id="api-complex-facts",
        )
        self.assertIn("本地CDE数据库初步匹配", result["final_response"])
        self.assertIn("CTR", result["final_response"])
        self.assertEqual(len(seen), 1)
        self.assertIn("Angela internal PatientFacts", seen[0])
        self.assertIn("Angela authoritative local CDE evidence", seen[0])
        self.assertIn('"scores": ["SLEDAI得分大于12分"]', seen[0])
        self.assertIn("[Original user request]", seen[0])

    async def test_authoritative_evidence_rejects_changed_return_count(self) -> None:
        evidence = FastPathResponse(
            text="确定性回退：CTR20260001、CTR20260002",
            total_count=108,
            returned_count=2,
            result_ids=("CTR20260001", "CTR20260002"),
        )
        changed = "共检索108项，保留10条，包括CTR20260001。"
        self.assertEqual(
            _enforce_authoritative_evidence(changed, evidence),
            evidence.text,
        )

    async def test_authoritative_evidence_appends_missing_patient_condition_trace(self) -> None:
        trace = "已纳入本次分析的患者条件：既往治疗：曲妥珠单抗；器官功能：肝功能轻度异常。"
        evidence = FastPathResponse(
            text=f"确定性结果：CTR20260001\n\n{trace}",
            total_count=1,
            returned_count=1,
            result_ids=("CTR20260001",),
            metadata={
                "condition_trace_text": trace,
                "required_condition_terms": ("曲妥珠单抗", "肝功能轻度异常"),
            },
        )
        response = "找到1条可核对记录：CTR20260001。"
        guarded = _enforce_authoritative_evidence(response, evidence)
        self.assertIn(trace, guarded)
        self.assertTrue(guarded.startswith(response))

    async def test_authoritative_evidence_does_not_duplicate_complete_condition_trace(self) -> None:
        trace = "已纳入本次分析的患者条件：生物标志物：EGFR L858R。"
        evidence = FastPathResponse(
            text=f"确定性结果：CTR20260001\n\n{trace}",
            total_count=1,
            returned_count=1,
            result_ids=("CTR20260001",),
            metadata={
                "condition_trace_text": trace,
                "required_condition_terms": ("EGFR L858R",),
            },
        )
        response = "EGFR L858R已纳入分析，候选为CTR20260001。"
        guarded = _enforce_authoritative_evidence(response, evidence)
        self.assertEqual(guarded.count("EGFR L858R已纳入分析"), 1)
        self.assertIn("UNKNOWN", guarded)
        self.assertIn("最终入组", guarded)

    async def test_authoritative_evidence_appends_unknown_and_safety_boundaries(self) -> None:
        evidence = FastPathResponse(
            text="确定性结果：CTR20260001",
            total_count=1,
            returned_count=1,
            result_ids=("CTR20260001",),
        )
        guarded = _enforce_authoritative_evidence("候选项目为CTR20260001。", evidence)
        self.assertIn("本地CDE数据库", guarded)
        self.assertIn("UNKNOWN", guarded)
        self.assertIn("待核对", guarded)
        self.assertIn("最终入组", guarded)

    async def test_zero_result_response_is_not_decorated(self) -> None:
        evidence = FastPathResponse(
            text="暂未找到",
            total_count=0,
            returned_count=0,
        )
        self.assertEqual(_enforce_authoritative_evidence("暂未找到", evidence), "暂未找到")

    async def test_gateway_agent_context_is_ephemeral(self) -> None:
        gateway = _Gateway()
        event = _Event(
            "52岁男性SLE，请判断是否适合临床试验",
            _Source(),
            message_id="ding-complex-context",
            channel_prompt="existing channel policy",
        )
        seen = []

        async def unmanaged(received):
            seen.append(received.channel_prompt)
            return "复杂请求已处理"

        answer = await handle_gateway_turn(gateway, event, unmanaged)
        self.assertIn("本地CDE数据库初步匹配", answer)
        self.assertIn("CTR", answer)
        self.assertIn("Angela internal PatientFacts", seen[0])
        self.assertIn("Angela authoritative local CDE evidence", seen[0])
        self.assertIn("existing channel policy", seen[0])
        self.assertEqual(event.channel_prompt, "existing channel policy")

    async def test_busy_agent_pool_does_not_block_fast_pool(self) -> None:
        gateway = _Gateway()
        agent_started = asyncio.Event()
        release_agent = asyncio.Event()

        async def slow_agent(event):
            agent_started.set()
            await release_agent.wait()
            return "agent-finished"

        agent_task = asyncio.create_task(
            handle_gateway_turn(gateway, _Event("你好", _Source(chat_id="agent-chat")), slow_agent)
        )
        await agent_started.wait()

        async def must_not_run(event):
            self.fail("eligible local query must not enter the occupied agent pool")

        fast_answer = await asyncio.wait_for(
            handle_gateway_turn(
                gateway,
                _Event("请查CDE：40岁，SLE，返回5条", _Source(chat_id="fast-chat")),
                must_not_run,
            ),
            timeout=5,
        )
        self.assertIn("本地CDE数据库初步匹配", fast_answer)
        release_agent.set()
        self.assertEqual(await agent_task, "agent-finished")

    async def test_duplicate_platform_message_is_executed_once(self) -> None:
        gateway = _Gateway()
        calls = []

        async def unmanaged(event):
            calls.append(event.text)
            return "仅执行一次"

        event = _Event("你好", _Source(), message_id="ding-message-001")
        first = await handle_gateway_turn(gateway, event, unmanaged)
        duplicate = await handle_gateway_turn(gateway, event, unmanaged)
        self.assertEqual(first, "仅执行一次")
        self.assertEqual(duplicate, "仅执行一次")
        self.assertEqual(calls, ["你好"])

        with sqlite3.connect(Path(self.tempdir.name) / "runtime.sqlite3") as connection:
            count, state = connection.execute(
                "SELECT COUNT(*),MAX(state) FROM requests"
            ).fetchone()
        self.assertEqual(count, 1)
        self.assertEqual(state, "COMPLETED")

    async def test_conflicting_patient_facts_wait_for_user_without_agent(self) -> None:
        gateway = _Gateway()

        async def unmanaged(event):
            self.fail("conflicting patient facts must pause before agent execution")

        answer = await handle_gateway_turn(
            gateway,
            _Event(
                "2019年出生，20岁，女，SLE，找临床试验",
                _Source(),
                message_id="ding-message-conflict",
            ),
            unmanaged,
        )
        self.assertIn("请先确认", answer)
        with sqlite3.connect(Path(self.tempdir.name) / "runtime.sqlite3") as connection:
            state, reason = connection.execute(
                "SELECT state,reason_code FROM requests"
            ).fetchone()
        self.assertEqual(state, "WAITING_USER")
        self.assertEqual(reason, "PATIENT_FACTS_CONFLICT")


if __name__ == "__main__":
    unittest.main()
