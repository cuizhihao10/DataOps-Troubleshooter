"""用官方 AsyncOpenAI SDK 与 MockTransport 验证 Structured Outputs Planner 边界。

测试不访问付费或外部模型，但真实执行 SDK 请求序列化、Pydantic parse、refusal 和超时映射；
最后一例再连接 LangGraph 与真实 stdio MCP，证明模型适配器已进入 Action/Observation 回环。
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from app.agents.chat import (
    ChatMessage,
    ChatRole,
    OpenAICompatiblePlannerProvider,
)
from app.agents.planner import (
    PlannerProviderError,
    PlannerRefusalError,
    PlannerTurnContext,
)
from app.agents.planner_adapter import OpenAICompatiblePlannerAgent
from app.capabilities import (
    CapabilitySelectionRequest,
    DiagnosisIntent,
    get_capability_registry,
)
from app.domain.models import AgentState, Component
from app.domain.planner import PlannerStatus
from app.mcp.client import StdioMcpClient
from app.mcp.executor import McpToolExecutor
from app.observability import (
    InMemoryModelCallRecorder,
    ModelCallRole,
    ModelCallStatus,
    bind_model_call_recorder,
    reset_model_call_recorder,
)
from app.orchestration import BoundedReactLoop, ReactLoopConfig, ReactRunRequest


def _chat_response(
    *,
    content: str | None,
    refusal: str | None = None,
) -> dict[str, Any]:
    """构造官方 SDK 可解析的最小 Chat Completion HTTP JSON 响应。

    content 与 refusal 分开传入以覆盖 Structured Outputs 成功和安全拒绝；固定 ID、模型和 token
    数只用于协议解析，不作为项目性能实测。响应不包含工具调用，因为 Planner 只描述 Action。
    """

    return {
        "id": "chatcmpl_synthetic_planner",
        "object": "chat.completion",
        "created": 1,
        "model": "synthetic-compatible-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "refusal": refusal,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


def _finish_json(*, evidence_refs: list[str] | None = None) -> str:
    """生成字段完整的 finish PlannerDecision JSON 字符串。

    所有字段都显式出现以匹配 SDK strict Schema；可注入真实 evidence_refs 验证第二轮回写引用。
    函数只生成合成控制数据，不包含根因或生产信息。
    """

    return json.dumps(
        {
            "status": "finish",
            "decision_summary": "当前结构化证据足以结束本轮。",
            "hypothesis_updates": [],
            "action": None,
            "evidence_refs": evidence_refs or [],
            "stop_reason": "evidence_sufficient",
        },
        ensure_ascii=False,
    )


def _context() -> PlannerTurnContext:
    """构造供 SDK/修复测试使用的合法 LTS PlannerTurnContext。

    真实 capability registry 决定允许工具，状态显式注入同一意图和名称；上下文不含历史案例或
    GraphRAG，避免协议测试依赖数据库，同时仍经过全部 Pydantic 一致性校验。
    """

    selection = get_capability_registry().select(
        CapabilitySelectionRequest(
            intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
            components=(Component.LTS,),
        )
    )
    return PlannerTurnContext(
        state=AgentState(
            run_id="run_sdk_planner_001",
            session_id="session_sdk_planner_001",
            user_query="检查 LTS 合成任务",
            intent=selection.intent.value,
            active_capabilities=[name.value for name in selection.active_capabilities],
        ),
        capabilities=selection,
        max_react_steps=6,
        remaining_time_ms=30_000,
    )


def _sdk_client(transport: httpx.AsyncBaseTransport) -> AsyncOpenAI:
    """用合成密钥、兼容 base_url 和指定 Transport 构造真实 AsyncOpenAI 客户端。

    max_retries=0 与生产 Provider 一致，使测试调用次数可精确断言；HTTP 连接池由返回客户端拥有，
    每个测试必须显式 close。密钥不会进入请求体、日志或仓库配置。
    """

    return AsyncOpenAI(
        api_key="local_test_key",
        base_url="https://planner.example.test/v1",
        http_client=httpx.AsyncClient(transport=transport),
        max_retries=0,
    )


@pytest.mark.asyncio
async def test_official_sdk_sends_strict_pydantic_schema_without_api_tools() -> None:
    """验证 Provider 通过官方 SDK 发送 strict json_schema 并解析 PlannerDecision。

    MockTransport 捕获真实 HTTP body；必须包含 PlannerDecision 所有字段、strict=true，且不能发送
    tools/tool_choice。成功响应由 SDK 解析为 Pydantic finish，而不是适配器手写 json.loads。
    """

    captured: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """记录 SDK 请求 JSON 并返回一个合法 Structured Output。

        handler 不读取认证头，避免测试暴露合成 key；只验证 body 后返回固定协议响应。
        """

        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response(content=_finish_json()))

    client = _sdk_client(httpx.MockTransport(handler))
    provider = OpenAICompatiblePlannerProvider(
        api_key=SecretStr("unused_when_client_is_injected"),
        base_url="https://planner.example.test/v1",
        model="synthetic-compatible-model",
        timeout_seconds=5,
        client=client,
    )
    messages = (
        ChatMessage(role=ChatRole.SYSTEM, content="Return one PlannerDecision JSON object."),
        ChatMessage(role=ChatRole.USER, content="Use the supplied synthetic context only."),
    )

    recorder = InMemoryModelCallRecorder()
    token = bind_model_call_recorder(recorder)
    try:
        result = await provider.complete(messages)
    finally:
        # 恢复 ContextVar，保证同一 pytest 进程中的后续 Provider 测试不会写入本例记录器。
        reset_model_call_recorder(token)

    assert result.status is PlannerStatus.FINISH
    assert len(captured) == 1
    body = captured[0]
    assert body["model"] == "synthetic-compatible-model"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    schema = body["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == {
        "status",
        "decision_summary",
        "hypothesis_updates",
        "action",
        "evidence_refs",
        "stop_reason",
    }
    assert "tools" not in body
    assert "tool_choice" not in body
    metrics = recorder.snapshot()
    assert len(metrics) == 1
    assert metrics[0].role is ModelCallRole.PLANNER
    assert metrics[0].status is ModelCallStatus.SUCCEEDED
    assert metrics[0].token_usage is not None
    assert metrics[0].token_usage.total_tokens == 15
    await client.close()


@pytest.mark.asyncio
async def test_real_sdk_parse_failure_is_repaired_once_by_planner_agent() -> None:
    """验证 SDK 抛出的 Pydantic JSON 错误会触发一次真实第二次 Chat 请求。

    首次返回非 JSON，Provider 从 ValidationError 提取 raw input；Agent 第二次请求追加 assistant/user
    修复消息并获得合法 finish。请求总数必须为二，证明没有 SDK 隐式重试。
    """

    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """首次返回无效文本，第二次检查修复消息后返回合法 JSON。

        超出两次调用立即失败，精确验证 repair_count 与 max_retries=0 的组合边界。
        """

        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(200, json=_chat_response(content="not-json"))
        if len(bodies) == 2:
            assert [message["role"] for message in body["messages"]] == [
                "system",
                "user",
                "assistant",
                "user",
            ]
            assert body["messages"][2]["content"] == "not-json"
            assert "未通过结构化校验" in body["messages"][3]["content"]
            return httpx.Response(200, json=_chat_response(content=_finish_json()))
        raise AssertionError("Planner repair must not call the SDK more than twice")

    client = _sdk_client(httpx.MockTransport(handler))
    provider = OpenAICompatiblePlannerProvider(
        api_key=SecretStr("unused"),
        base_url="https://planner.example.test/v1",
        model="synthetic-compatible-model",
        timeout_seconds=5,
        client=client,
    )
    agent = OpenAICompatiblePlannerAgent(provider=provider, repair_count=1)

    result = await agent.decide(_context())

    assert result.status is PlannerStatus.FINISH
    assert len(bodies) == 2
    await client.close()


@pytest.mark.asyncio
async def test_sdk_refusal_and_timeout_map_to_non_repairable_domain_errors() -> None:
    """验证 refusal 与 HTTP timeout 分别映射且不被当作 JSON 修复问题。

    两个独立客户端避免状态共享：refusal 保留受控属性，ReadTimeout 经 SDK 转成 APITimeoutError 后
    再映射 PlannerProviderError(timeout, retryable)。测试不调用 Agent，因此精确覆盖 Provider。
    """

    async def refusal_handler(request: httpx.Request) -> httpx.Response:
        """返回 content=null 和结构化 refusal，模拟供应商安全拒绝。

        request 未使用但保留签名；响应不伪造 PlannerDecision JSON。
        """

        return httpx.Response(
            200,
            json=_chat_response(content=None, refusal="synthetic refusal detail"),
        )

    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        """抛出 httpx.ReadTimeout，让官方 SDK 完成异常类型转换。

        请求对象用于绑定异常，Provider 只能公开 timeout 分类，不泄露 URL 或认证头。
        """

        raise httpx.ReadTimeout("synthetic timeout", request=request)

    messages = (
        ChatMessage(role=ChatRole.SYSTEM, content="Return structured JSON only."),
        ChatMessage(role=ChatRole.USER, content="Synthetic request."),
    )
    refusal_client = _sdk_client(httpx.MockTransport(refusal_handler))
    refusal_provider = OpenAICompatiblePlannerProvider(
        api_key=SecretStr("unused"),
        base_url="https://planner.example.test/v1",
        model="synthetic-compatible-model",
        timeout_seconds=5,
        client=refusal_client,
    )
    with pytest.raises(PlannerRefusalError):
        await refusal_provider.complete(messages)
    await refusal_client.close()

    timeout_client = _sdk_client(httpx.MockTransport(timeout_handler))
    timeout_provider = OpenAICompatiblePlannerProvider(
        api_key=SecretStr("unused"),
        base_url="https://planner.example.test/v1",
        model="synthetic-compatible-model",
        timeout_seconds=0.1,
        client=timeout_client,
    )
    with pytest.raises(PlannerProviderError) as exc_info:
        await timeout_provider.complete(messages)
    assert exc_info.value.error_code == "timeout"
    assert exc_info.value.retryable is True
    await timeout_client.close()


@pytest.mark.asyncio
async def test_openai_compatible_agent_drives_langgraph_and_real_mcp_round_trip() -> None:
    """验证真实 SDK适配器驱动 LangGraph→stdio MCP→Observation→模型第二轮。

    Mock 模型首轮返回 LTS Action；真实 MCP 生成 evidence_id 后，第二个 SDK 请求的 Prompt 必须
    包含该引用，handler 再返回引用它的 finish。该测试不访问外网但贯通当前全部运行边界。
    """

    bodies: list[dict[str, Any]] = []
    run_id = "run_openai_mcp_001"

    async def handler(request: httpx.Request) -> httpx.Response:
        """按调用轮次返回 call_tool 或引用 Prompt 中真实 evidence_id 的 finish。

        第二轮找不到 evidence_id 时立即失败，防止测试用硬编码引用掩盖 Observation 未回写问题。
        """

        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            content = json.dumps(
                {
                    "status": "call_tool",
                    "decision_summary": "先读取 LTS 当前状态。",
                    "hypothesis_updates": [],
                    "action": {
                        "tool_name": "lts.get_task_status",
                        "arguments": {
                            "resource_id": "dws_order_report_daily",
                            "time_range": {
                                "start": "2026-07-10T00:00:00+08:00",
                                "end": "2026-07-10T03:00:00+08:00",
                            },
                            "scenario_id": "cross_chain_pk_conflict",
                            "trace_id": run_id,
                        },
                    },
                    "evidence_refs": [],
                    "stop_reason": None,
                },
                ensure_ascii=False,
            )
            return httpx.Response(200, json=_chat_response(content=content))
        combined_prompt = "\n".join(message["content"] for message in body["messages"])
        match = re.search(r'"evidence_id":\s*"(ev_[a-f0-9]{16})"', combined_prompt)
        if match is None:
            raise AssertionError("second Planner prompt must contain real MCP evidence_id")
        return httpx.Response(
            200,
            json=_chat_response(content=_finish_json(evidence_refs=[match.group(1)])),
        )

    client = _sdk_client(httpx.MockTransport(handler))
    provider = OpenAICompatiblePlannerProvider(
        api_key=SecretStr("unused"),
        base_url="https://planner.example.test/v1",
        model="synthetic-compatible-model",
        timeout_seconds=5,
        client=client,
    )
    agent = OpenAICompatiblePlannerAgent(provider=provider, repair_count=1)
    loop = BoundedReactLoop(
        planner=agent,
        executor=McpToolExecutor(StdioMcpClient(), retry_count=1),
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=15),
    )
    result = await loop.run(
        ReactRunRequest(
            state=AgentState(
                run_id=run_id,
                session_id="session_openai_mcp_001",
                user_query="检查 LTS 合成任务失败原因",
            ),
            capability_request=CapabilitySelectionRequest(
                intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
                components=(Component.LTS,),
            ),
        )
    )

    assert len(bodies) == 2
    assert result.state.react_step == 1
    assert result.state.stop_reason == "evidence_sufficient"
    assert result.state.evidence
    assert result.state.next_action is not None
    assert result.state.next_action.evidence_refs == result.state.observation_refs
    await client.close()
