"""验证 session/message/run/event 资源路由的 503、成功、404 和安全失败映射。

测试通过 FastAPI lifespan 保留真实 Fixture/MCP 启动审计，再注入记录型 diagnosis runtime；不访问模型
或 PostgreSQL。持久化与真实 workflow 由独立 postgres 集成测试覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.api.main import app
from app.capabilities import DiagnosisIntent, HistoryTrigger
from app.domain.models import Component
from app.orchestration.diagnosis_runtime import DiagnosisExecutionFailed
from app.orchestration.run_models import (
    DIAGNOSIS_API_CONTRACT_ID,
    AgentRunSnapshot,
    AgentRunStatus,
    DiagnosisMessage,
    DiagnosisSession,
    RunEventList,
    RunEventPhase,
    RunPublicEvent,
)

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


class FakeDiagnosisRuntime:
    """提供四个资源路由需要的方法，并记录标题、消息和查询 ID。

    替身返回生产 Pydantic 模型，不使用松散字典绕过响应校验；可配置 submit 失败以验证 API 只公开
    run_id/稳定错误码，不泄露底层异常文本。
    """

    def __init__(self, *, fail_submit: bool = False) -> None:
        """初始化固定 session/running run/event 资源和空调用记录。

        ``fail_submit`` 只在 message 路由触发 DiagnosisExecutionFailed；构造不执行 I/O。固定 ID 满足
        生产 pattern，时间带 UTC，便于精确断言 JSON。
        """

        self.fail_submit = fail_submit
        self.session = DiagnosisSession(
            session_id="session_1111111111111111",
            title="合成排障会话",
            created_at=NOW,
            updated_at=NOW,
        )
        self.run = AgentRunSnapshot(
            run_id="run_2222222222222222",
            session_id=self.session.session_id,
            status=AgentRunStatus.RUNNING,
            user_query="检查 LTS 合成任务",
            intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
            components=(Component.LTS,),
            history_trigger=HistoryTrigger.NOT_REQUESTED,
            created_at=NOW,
            started_at=NOW,
            updated_at=NOW,
        )
        self.events = RunEventList(
            contract_id=DIAGNOSIS_API_CONTRACT_ID,
            run_id=self.run.run_id,
            events=(
                RunPublicEvent(
                    event_id="run_evt_3333333333333333",
                    run_id=self.run.run_id,
                    sequence=1,
                    phase=RunEventPhase.SYSTEM,
                    event_type="run_created",
                    summary="合成 run 已创建。",
                    created_at=NOW,
                ),
            ),
        )
        self.titles: list[str] = []
        self.messages: list[tuple[str, DiagnosisMessage]] = []
        self.run_queries: list[str] = []
        self.event_queries: list[str] = []

    async def create_session(self, *, title: str) -> DiagnosisSession:
        """记录标题并返回标题更新后的固定会话。

        方法不模拟数据库主键生成；model_copy 只修改已验证字符串，API 响应仍由 Pydantic 重新序列化。
        输入为空白时由路由 Schema 提前拒绝，本替身不会静默补默认标题或抛数据库异常。
        """

        self.titles.append(title)
        self.session = self.session.model_copy(update={"title": title})
        return self.session

    async def submit_message(
        self,
        session_id: str,
        message: DiagnosisMessage,
    ) -> AgentRunSnapshot | None:
        """记录消息，按 session ID 返回 running 快照、None 或安全执行异常。

        未知 session 返回 None；fail_submit 时抛带固定 run ID 的安全异常。替身不执行 workflow，路由
        只验证 HTTP 映射和请求 Schema。
        """

        self.messages.append((session_id, message))
        if session_id != self.session.session_id:
            return None
        if self.fail_submit:
            raise DiagnosisExecutionFailed(
                run_id=self.run.run_id,
                error_code="diagnosis_execution_failed",
                public_message="合成安全失败摘要。",
            )
        return self.run

    async def get_run(self, run_id: str) -> AgentRunSnapshot | None:
        """记录查询并仅对固定 run ID 返回快照。

        未命中返回 None，模拟仓储 404；方法不重新执行诊断或加载事件。输入精确匹配公开资源 ID，
        不做大小写归一化或模糊查找，避免测试隐藏路由参数错误。
        """

        self.run_queries.append(run_id)
        return self.run if run_id == self.run.run_id else None

    async def get_events(self, run_id: str) -> RunEventList | None:
        """记录查询并仅对固定 run ID 返回连续事件列表。

        事件已经通过 RunEventList 校验，未知 ID 返回 None；方法不返回 Thought 或原始异常。
        """

        self.event_queries.append(run_id)
        return self.events if run_id == self.run.run_id else None


@pytest.mark.asyncio
async def test_diagnosis_resource_routes_return_503_when_runtime_is_disabled() -> None:
    """验证默认 Planner/Auditor disabled 环境下四类资源入口都明确返回 503。

    该行为区别于未知 session/run 的 404，防止客户端把“模型未配置”误解为资源不存在；响应不包含
    API key、数据库 URL 或供应商异常。
    """

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/api/v1/sessions", json={})
            submitted = await client.post(
                "/api/v1/sessions/session_1111111111111111/messages",
                json={
                    "content": "检查 LTS",
                    "intent": "single_component_diagnosis",
                    "components": ["lts"],
                },
            )
            run = await client.get("/api/v1/runs/run_2222222222222222")
            events = await client.get("/api/v1/runs/run_2222222222222222/events")

    assert [item.status_code for item in (created, submitted, run, events)] == [503] * 4
    assert "configured Planner/Auditor" in created.json()["detail"]


@pytest.mark.asyncio
async def test_diagnosis_resource_routes_create_submit_read_and_return_404() -> None:
    """验证会话创建、消息校验、run/event 读取和未知资源 404 的完整 HTTP Schema。

    成功响应均携带 `diagnosis-resources:v1`；消息 intent/components/history trigger 被解析为
    生产枚举，未知 session/run 不调用伪默认对象。响应不包含 reasoning_process 或 Thought 字段。
    """

    runtime = FakeDiagnosisRuntime()
    async with app.router.lifespan_context(app):
        app.state.diagnosis_runtime = runtime
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/sessions",
                json={"title": "合成排障会话"},
            )
            submitted = await client.post(
                f"/api/v1/sessions/{runtime.session.session_id}/messages",
                json={
                    "content": "检查 LTS 合成任务",
                    "intent": "single_component_diagnosis",
                    "components": ["lts"],
                    "history_trigger": "not_requested",
                },
            )
            run = await client.get(f"/api/v1/runs/{runtime.run.run_id}")
            events = await client.get(f"/api/v1/runs/{runtime.run.run_id}/events")
            missing_session = await client.post(
                "/api/v1/sessions/session_aaaaaaaaaaaaaaaa/messages",
                json={
                    "content": "检查 LTS 合成任务",
                    "intent": "single_component_diagnosis",
                    "components": ["lts"],
                },
            )
            missing_run = await client.get("/api/v1/runs/run_aaaaaaaaaaaaaaaa")

    assert created.status_code == 201
    assert created.json()["contract_id"] == "diagnosis-resources:v1"
    assert submitted.status_code == 201
    assert submitted.json()["run"]["status"] == "running"
    assert run.status_code == 200
    assert events.status_code == 200
    assert events.json()["events"][0]["phase"] == "system"
    assert missing_session.status_code == 404
    assert missing_run.status_code == 404
    assert runtime.messages[0][1].components == (Component.LTS,)
    serialized = str([created.json(), submitted.json(), run.json(), events.json()])
    assert "reasoning_process" not in serialized
    assert "Thought" not in serialized


@pytest.mark.asyncio
async def test_message_failure_returns_safe_run_id_without_internal_exception_text() -> None:
    """验证 workflow 失败映射为含 run_id/error_code 的 500，且不泄露异常链。

    真实 runtime 会先持久化 failed run/event 再抛 DiagnosisExecutionFailed；路由只使用安全属性，测试
    确认响应不出现数据库、模型或 traceback 文本。
    """

    runtime = FakeDiagnosisRuntime(fail_submit=True)
    async with app.router.lifespan_context(app):
        app.state.diagnosis_runtime = runtime
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/sessions/{runtime.session.session_id}/messages",
                json={
                    "content": "检查 LTS 合成任务",
                    "intent": "single_component_diagnosis",
                    "components": ["lts"],
                },
            )

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["run_id"] == runtime.run.run_id
    assert detail["error_code"] == "diagnosis_execution_failed"
    assert detail["message"] == "合成安全失败摘要。"
    assert "traceback" not in str(detail).lower()
