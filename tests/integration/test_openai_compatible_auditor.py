"""用真实 AsyncOpenAI SDK MockTransport 验证 Auditor Structured Outputs 与 LangGraph 返工。

测试不访问付费模型，但真实执行 HTTP 请求序列化、strict Pydantic Schema、SDK parse 和两轮报告
审计；最终场景证明模型错误 accept 仍被确定性门禁否决并只返工一次。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from app.agents.auditor import AuditorTurnContext
from app.agents.auditor_adapter import OpenAICompatibleAuditorAgent
from app.agents.auditor_chat import OpenAICompatibleAuditorProvider
from app.agents.chat import ChatMessage, ChatRole
from app.capabilities import (
    CapabilitySelection,
    CapabilitySelectionRequest,
    DiagnosisIntent,
    get_capability_registry,
)
from app.domain.models import (
    AgentState,
    AuditStatus,
    Component,
    DiagnosisReport,
    Evidence,
    EvidenceSourceType,
    RemediationStep,
    RiskLevel,
    RootCauseConclusion,
)
from app.observability import (
    InMemoryModelCallRecorder,
    ModelCallRole,
    ModelCallStatus,
    bind_model_call_recorder,
    reset_model_call_recorder,
)
from app.orchestration import (
    AuditedReportWorkflow,
    ReportRunRequest,
    ReportWorkflowConfig,
    ReportWorkflowOutcome,
)

OBSERVED_AT = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)


def _chat_response(content: str) -> dict[str, Any]:
    """构造官方 SDK 可解析的最小合成 Chat Completion 响应。

    响应不包含 tool_calls，因为 Auditor 没有工具权限；固定 token 仅满足协议字段，不作为性能实测。
    """

    return {
        "id": "chatcmpl_synthetic_auditor",
        "object": "chat.completion",
        "created": 1,
        "model": "synthetic-compatible-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": None},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _accept_json() -> str:
    """返回字段完整、满足 strict Schema 的 AuditResult accept JSON。

    issues 和 revision_instructions 即使为空也显式提供，匹配 OpenAI Structured Outputs 所有字段
    required 的请求 Schema。
    """

    return json.dumps(
        {"status": "accept", "issues": [], "revision_instructions": []},
        ensure_ascii=False,
    )


def _selection() -> CapabilitySelection:
    """通过真实 registry 选择 LTS 单组件固定能力集合。

    返回对象同时供 Auditor context 和报告工作流使用，确保能力规则不是测试手写字典。
    """

    return get_capability_registry().select(
        CapabilitySelectionRequest(
            intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
            components=(Component.LTS,),
        )
    )


def _state(selection: CapabilitySelection, *, with_report: bool) -> AgentState:
    """构造含实时工具证据、ReAct 终态和可选最小报告的合成 AgentState。

    with_report=True 用于直接 Provider context；工作流输入为 False，让 draft node 自行创建
    或注入报告。
    """

    report = (
        DiagnosisReport(summary="证据不足。", uncertainties=["缺少日志证据。"])
        if with_report
        else None
    )
    return AgentState(
        run_id="run_sdk_auditor_001",
        session_id="session_sdk_auditor_001",
        user_query="审计 LTS 合成报告",
        intent=selection.intent.value,
        active_capabilities=[name.value for name in selection.active_capabilities],
        evidence=[
            Evidence(
                evidence_id="ev_sdk_auditor_001",
                source_type=EvidenceSourceType.TOOL,
                source_id="synthetic_lts_status",
                content="合成任务处于等待上游状态。",
                observed_at=OBSERVED_AT,
                reliability=0.95,
            )
        ],
        stop_reason="evidence_insufficient",
        draft_report=report,
    )


def _context() -> AuditorTurnContext:
    """构造可直接发送给 Auditor Agent 的合法首次审计上下文。

    上下文不含 GraphRAG 或案例，足以验证 SDK schema/parse；state 已带草稿并与 capability 对齐。
    """

    selection = _selection()
    return AuditorTurnContext(
        state=_state(selection, with_report=True),
        capabilities=selection,
        revision_number=0,
    )


def _sdk_client(transport: httpx.AsyncBaseTransport) -> AsyncOpenAI:
    """用合成密钥、兼容端点和指定 Transport 构造真实 AsyncOpenAI 客户端。

    `max_retries=0` 与生产一致，使 HTTP 调用次数可精确断言；每个测试负责显式关闭返回客户端。
    """

    return AsyncOpenAI(
        api_key="local_auditor_test_key",
        base_url="https://auditor.example.test/v1",
        http_client=httpx.AsyncClient(transport=transport),
        max_retries=0,
    )


class UnsupportedBuilder:
    """生成引用真实但根因不对应任何状态假设的合成报告。

    替身用于端到端证明 SDK 返回 accept 后，确定性规则仍强制一次修订；它不调用模型、修改状态
    或生成悬空引用，因而失败只能归因于语义门禁。
    """

    def build(
        self,
        state: AgentState,
        *,
        evidence_bundle=None,
        confirmed_case_memories=(),
        history_case_matches=(),
    ) -> DiagnosisReport:
        """使用现有 evidence_id 生成语义无依据根因和完整低风险步骤。

        可选参数满足生产协议但不读取；报告通过 Pydantic，因此否决来自语义门禁而非 JSON 失败。
        """

        ref = state.evidence[0].evidence_id
        return DiagnosisReport(
            summary="故意注入无依据根因。",
            root_causes=[
                RootCauseConclusion(
                    root_cause="不存在于状态假设的损坏",
                    confidence=0.99,
                    evidence_refs=[ref],
                )
            ],
            evidence_refs=[ref],
            remediation_steps=[
                RemediationStep(
                    order=1,
                    action="继续只读核验。",
                    risk_level=RiskLevel.LOW,
                    prerequisites=["确认 run_id。"],
                    rollback="不修改系统。",
                    verification="记录 Evidence。",
                )
            ],
            risks=["仅只读核验。"],
        )


@pytest.mark.asyncio
async def test_official_sdk_sends_audit_result_schema_without_tools() -> None:
    """验证 Auditor Provider 发送 strict AuditResult Schema 且不注册 API tools。

    捕获真实 HTTP body 后检查模型、required 字段、strict=true 和 tools/tool_choice 缺失；成功响应
    必须由 SDK 解析为 AuditStatus.accept，而不是手写 json.loads。
    """

    captured: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """记录请求体并返回合法 accept Structured Output。

        handler 不读取 Authorization 头，避免测试输出合成 key；只检查 JSON body。
        """

        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response(_accept_json()))

    client = _sdk_client(httpx.MockTransport(handler))
    provider = OpenAICompatibleAuditorProvider(
        api_key=SecretStr("unused_when_injected"),
        base_url="https://auditor.example.test/v1",
        model="synthetic-compatible-model",
        timeout_seconds=5,
        client=client,
    )
    messages = (
        ChatMessage(role=ChatRole.SYSTEM, content="Return one AuditResult JSON object."),
        ChatMessage(role=ChatRole.USER, content="Audit only this synthetic report."),
    )

    recorder = InMemoryModelCallRecorder()
    token = bind_model_call_recorder(recorder)
    try:
        result = await provider.complete(messages)
    finally:
        # Recorder 只覆盖这一请求，避免测试事件循环复用时把后续调用误计入本次结果。
        reset_model_call_recorder(token)

    assert result.status is AuditStatus.ACCEPT
    assert len(captured) == 1
    body = captured[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    schema = body["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == {"status", "issues", "revision_instructions"}
    assert "tools" not in body
    assert "tool_choice" not in body
    metrics = recorder.snapshot()
    assert len(metrics) == 1
    assert metrics[0].role is ModelCallRole.AUDITOR
    assert metrics[0].status is ModelCallStatus.SUCCEEDED
    assert metrics[0].token_usage is not None
    assert metrics[0].token_usage.total_tokens == 15
    await client.close()


@pytest.mark.asyncio
async def test_real_sdk_auditor_accept_is_vetoed_then_report_is_reaudited_once() -> None:
    """贯通真实 SDK、Auditor Agent 和 LangGraph，验证确定性否决与唯一返工。

    Mock 模型两次都返回 accept；首轮 Prompt 必须含 unsupported_claim，规则强制 revise 并删除根因；
    第二轮 Prompt 不再含该问题后接受。HTTP 恰好两次，最终 retry_count=1 且报告无根因。
    """

    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """记录两轮真实 SDK 请求并始终返回合法 accept。

        超出两次调用立即失败，防止隐藏 SDK 重试或第二次报告返工；请求体保留供断言两轮
        deterministic issues 的变化。
        """

        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) > 2:
            raise AssertionError("Auditor workflow must not call the SDK more than twice")
        return httpx.Response(200, json=_chat_response(_accept_json()))

    client = _sdk_client(httpx.MockTransport(handler))
    provider = OpenAICompatibleAuditorProvider(
        api_key=SecretStr("unused"),
        base_url="https://auditor.example.test/v1",
        model="synthetic-compatible-model",
        timeout_seconds=5,
        client=client,
    )
    auditor = OpenAICompatibleAuditorAgent(provider=provider, repair_count=1)
    selection = _selection()
    workflow = AuditedReportWorkflow(
        auditor=auditor,
        config=ReportWorkflowConfig(max_revisions=1),
        builder=UnsupportedBuilder(),
    )

    result = await workflow.run(
        ReportRunRequest(
            state=_state(selection, with_report=False),
            capabilities=selection,
        )
    )

    assert result.outcome is ReportWorkflowOutcome.ACCEPTED
    assert result.state.retry_count == 1
    assert result.state.draft_report is not None
    assert result.state.draft_report.root_causes == []
    assert len(bodies) == 2
    assert "unsupported_claim" in bodies[0]["messages"][1]["content"]
    assert "【确定性规则预检问题】\n[]" in bodies[1]["messages"][1]["content"]
    await client.close()
