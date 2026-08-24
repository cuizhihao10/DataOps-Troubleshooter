"""验证 session/message/run/event 资源路由的 503、成功、404 和安全失败映射。

测试通过 FastAPI lifespan 保留真实 Fixture/MCP 启动审计，再注入记录型 diagnosis runtime；不访问模型
或 PostgreSQL。持久化与真实 workflow 由独立 postgres 集成测试覆盖。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.api.main import app
from app.capabilities import DiagnosisIntent, HistoryTrigger
from app.domain.models import Component
from app.observability import (
    RuntimeMetricsSnapshot,
    RunTrace,
    SpanMetric,
    TraceSpan,
    TraceSpanKind,
    TraceSpanStatus,
    make_span_id,
)
from app.orchestration.diagnosis_runtime import DiagnosisExecutionFailed
from app.orchestration.run_models import (
    DIAGNOSIS_API_CONTRACT_ID,
    ActiveRunConflictError,
    AgentRunSnapshot,
    AgentRunStatus,
    DiagnosisMessage,
    DiagnosisSession,
    RunEventList,
    RunEventPhase,
    RunPublicEvent,
    RunResumeConflictError,
)

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


class FakeDiagnosisRuntime:
    """提供四个资源路由需要的方法，并记录标题、消息和查询 ID。

    替身返回生产 Pydantic 模型，不使用松散字典绕过响应校验；可配置 submit 失败以验证 API 只公开
    run_id/稳定错误码，不泄露底层异常文本。
    """

    def __init__(self, *, fail_submit: bool = False, conflict_submit: bool = False) -> None:
        """初始化固定 session/running run/event 资源和空调用记录。

        ``fail_submit`` 只在 message 路由触发 DiagnosisExecutionFailed；构造不执行 I/O。固定 ID 满足
        生产 pattern，时间带 UTC，便于精确断言 JSON。
        """

        self.fail_submit = fail_submit
        self.conflict_submit = conflict_submit
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
            attempt_count=1,
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
        self.stream_cursors: list[int] = []
        self.trace_queries: list[str] = []
        self.metrics_queries = 0
        self.trace = RunTrace(
            run_id=self.run.run_id,
            spans=(
                TraceSpan(
                    run_id=self.run.run_id,
                    sequence=1,
                    span_id=make_span_id(self.run.run_id, 1),
                    kind=TraceSpanKind.WORKFLOW,
                    name="diagnosis.workflow",
                    status=TraceSpanStatus.OK,
                    started_at=NOW,
                    ended_at=NOW,
                    duration_ms=1200.5,
                ),
                TraceSpan(
                    run_id=self.run.run_id,
                    sequence=2,
                    span_id=make_span_id(self.run.run_id, 2),
                    parent_span_id=make_span_id(self.run.run_id, 1),
                    kind=TraceSpanKind.TOOL_CALL,
                    name="react.tool_call",
                    status=TraceSpanStatus.ERROR,
                    started_at=NOW,
                    ended_at=NOW,
                    duration_ms=310.25,
                    attributes={"tool_name": "lts_job_status", "ok": False},
                ),
            ),
        )

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
        if self.conflict_submit:
            raise ActiveRunConflictError(self.run.run_id)
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

    async def get_events_after(
        self,
        run_id: str,
        *,
        after_sequence: int,
    ) -> tuple[RunPublicEvent, ...] | None:
        """记录推流游标并返回序号大于它的事件，未知 run 返回 None。

        过滤逻辑与仓储的 `sequence > after_sequence` 一致，因此测试可以断言路由把 `Last-Event-ID`
        或查询参数正确转成了游标，而不是每轮重推整条时间线。
        """

        self.stream_cursors.append(after_sequence)
        if run_id != self.run.run_id:
            return None
        return tuple(
            event for event in self.events.events if event.sequence > after_sequence
        )

    async def get_run_trace(self, run_id: str) -> RunTrace | None:
        """记录查询并仅对固定 run ID 返回一棵合法的双层调用链。

        替身返回真实 ``RunTrace`` 而不是字典，因此路由的响应模型仍会重新校验唯一根与父指针；
        span 落库与压实语义由 postgres 集成测试覆盖，这里只验证 HTTP 层的 200/404/503 映射。
        """

        self.trace_queries.append(run_id)
        return self.trace if run_id == self.run.run_id else None

    async def get_runtime_metrics(self) -> RuntimeMetricsSnapshot:
        """返回一份固定的运行时指标快照，供 /metrics 渲染断言使用。

        快照数值刻意与 span 断言解耦：路由只负责渲染与 Content-Type，聚合正确性属于仓储职责，
        混在一起会让 HTTP 测试在 SQL 变更时误报失败。
        """

        self.metrics_queries += 1
        return RuntimeMetricsSnapshot(
            run_counts={"completed": 1},
            spans=(
                SpanMetric(
                    kind="workflow",
                    name="diagnosis.workflow",
                    count=1,
                    duration_ms_sum=1200.5,
                    duration_ms_max=1200.5,
                    error_count=0,
                ),
            ),
        )

    async def cancel_run(self, run_id: str) -> AgentRunSnapshot | None:
        """模拟服务端原子取消，验证 HTTP 层只暴露 cancelled 快照。

        测试替身只改变已验证的 Pydantic 快照，不执行数据库或 Worker I/O；真实的
        行锁和事件写入由 PostgreSQL 集成测试覆盖，避免 API 测试伪造底层语义。
        """

        if run_id != self.run.run_id:
            return None
        if self.run.status in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING}:
            self.run = self.run.model_copy(
                update={
                    "status": AgentRunStatus.CANCELLED,
                    "error_code": "user_cancelled",
                    "error_message": "诊断任务已由用户取消，可从会话检查点恢复。",
                    "completed_at": NOW,
                    "updated_at": NOW,
                }
            )
        return self.run

    async def resume_run(self, run_id: str) -> AgentRunSnapshot | None:
        """模拟只允许 cancelled 来源恢复，并返回新的 queued run。

        该替身复用原始输入但替换 run ID 与生命周期字段，专门验证 HTTP 202/409
        映射；它不声称实现真正的 checkpoint 恢复，真实恢复由 runtime 测试负责。
        """

        if run_id != self.run.run_id:
            return None
        if self.run.status is not AgentRunStatus.CANCELLED:
            raise RunResumeConflictError(run_id, self.run.status)
        self.run = self.run.model_copy(
            update={
                "run_id": "run_4444444444444444",
                "status": AgentRunStatus.QUEUED,
                "error_code": None,
                "error_message": None,
                "started_at": None,
                "completed_at": None,
                "attempt_count": 0,
                "updated_at": NOW,
            }
        )
        return self.run


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

        成功响应均携带 `diagnosis-resources:v4`；消息 intent/components/history trigger 被解析为
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
    assert created.json()["contract_id"] == "diagnosis-resources:v4"
    assert submitted.status_code == 202
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


@pytest.mark.asyncio
async def test_message_conflict_returns_active_run_id_for_client_polling() -> None:
    """验证 queued/running session 冲突映射为 409，而不是重复创建并发 run。

    替身直接抛出仓储层 ActiveRunConflictError，路由应只公开 active_run_id 和稳定提示；客户端
    可以据此继续轮询旧 run。测试不访问数据库，专注检查 HTTP 错误边界和不泄漏异常文本。
    """

    runtime = FakeDiagnosisRuntime(conflict_submit=True)
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

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "active_run_id": runtime.run.run_id,
        "message": "session has an active run",
    }


@pytest.mark.asyncio
async def test_cancel_and_resume_routes_keep_cancelled_run_auditable() -> None:
    """验证 cancel 进入独立终态、resume 创建新 queued run 且未知 ID 返回 404。

    测试同时检查公开 error_code 和 HTTP 状态，确保浏览器能区分用户取消、恢复提交
    以及未知资源，而不会看到内部异常链或部分模型输出。
    """

    runtime = FakeDiagnosisRuntime()
    async with app.router.lifespan_context(app):
        app.state.diagnosis_runtime = runtime
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            cancelled = await client.post(f"/api/v1/runs/{runtime.run.run_id}/cancel")
            resumed = await client.post(f"/api/v1/runs/{runtime.run.run_id}/resume")
            missing = await client.post("/api/v1/runs/run_aaaaaaaaaaaaaaaa/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["run"]["status"] == "cancelled"
    assert cancelled.json()["run"]["error_code"] == "user_cancelled"
    assert resumed.status_code == 202
    assert resumed.json()["run"]["status"] == "queued"
    assert resumed.json()["run"]["run_id"] == "run_4444444444444444"
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_trace_and_metrics_routes_expose_timeline_without_reasoning_text() -> None:
    """验证 trace 路由返回契约化 span 树、/metrics 渲染曝光文本，且两者都不含推理正文。

    trace 是本项目对外可见的可观测性证据，因此这里同时断言 contract_id、父子关系与错误状态；
    /metrics 断言 Content-Type 与指标族，防止未来换渲染实现时静默丢掉版本参数导致抓取端拒绝。
    """

    runtime = FakeDiagnosisRuntime()
    async with app.router.lifespan_context(app):
        app.state.diagnosis_runtime = runtime
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            trace = await client.get(f"/api/v1/runs/{runtime.run.run_id}/trace")
            missing = await client.get("/api/v1/runs/run_aaaaaaaaaaaaaaaa/trace")
            metrics = await client.get("/metrics")

    assert trace.status_code == 200
    payload = trace.json()
    assert payload["contract_id"] == "run-trace:v1"
    assert payload["trace"]["contract_id"] == "run-trace:v1"
    assert payload["trace"]["dropped_span_count"] == 0
    spans = payload["trace"]["spans"]
    assert [span["name"] for span in spans] == ["diagnosis.workflow", "react.tool_call"]
    assert spans[0]["parent_span_id"] is None
    assert spans[1]["parent_span_id"] == spans[0]["span_id"]
    assert spans[1]["status"] == "error"
    assert spans[1]["attributes"] == {"tool_name": "lts_job_status", "ok": False}
    assert missing.status_code == 404
    assert runtime.trace_queries == [runtime.run.run_id, "run_aaaaaaaaaaaaaaaa"]

    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'dataops_runs_total{status="completed"} 1' in metrics.text
    assert 'dataops_span_count{kind="workflow",name="diagnosis.workflow"} 1' in metrics.text
    assert runtime.metrics_queries == 1
    serialized = f"{payload}{metrics.text}"
    assert "Thought" not in serialized
    assert "reasoning_process" not in serialized


@pytest.mark.asyncio
async def test_trace_and_metrics_routes_return_503_when_runtime_is_disabled() -> None:
    """验证 runtime 未装配时 trace 与 /metrics 都返回 503 而不是空 trace 或全零指标。

    全零曝光会被看板渲染成"零错误"，把"没部署"伪装成"很健康"，这是可观测性里最危险的一类
    假象；明确 503 让抓取端把该实例标记为 down，而不是纳入正常样本。
    """

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            trace = await client.get("/api/v1/runs/run_2222222222222222/trace")
            metrics = await client.get("/metrics")

    assert trace.status_code == 503
    assert metrics.status_code == 503


def _cancelled(run: AgentRunSnapshot) -> AgentRunSnapshot:
    """把 running 快照复制成合法的 cancelled 终态，供推流测试构造可结束的 run。

    推流只在 run 终态或连接预算耗尽时结束，而默认替身停在 running；若直接用 running 做推流断言，
    测试要么等满连接寿命要么依赖计时，因此这里显式给出终态而不是缩短寿命预算。
    """

    return run.model_copy(
        update={
            "status": AgentRunStatus.CANCELLED,
            "completed_at": NOW,
            "error_code": "user_cancelled",
            "error_message": "用户已取消该诊断。",
        }
    )


async def _read_sse_frames(client: httpx.AsyncClient, url: str) -> list[dict[str, str]]:
    """读取一条 SSE 响应并把 `event:`/`id:`/`data:` 行解析为帧字典列表。

    这里刻意手工解析而不引入 SSE 客户端库：断言的重点是服务端是否真的按 `text/event-stream`
    的行协议输出了命名事件和标准 `id` 字段——用库解析会把这些细节隐藏起来，一旦帧编码退化成
    匿名 `data:` 消息，浏览器 `EventSource` 的重连续传就会静默失效而测试仍然通过。
    """

    frames: list[dict[str, str]] = []
    current: dict[str, str] = {}
    async with client.stream("GET", url) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            if not line.strip():
                if current:
                    frames.append(current)
                    current = {}
                continue
            key, _, value = line.partition(":")
            current[key.strip()] = value.strip()
    if current:
        frames.append(current)
    return frames


@pytest.mark.asyncio
async def test_run_stream_pushes_named_frames_with_cursor_ids_until_terminal() -> None:
    """验证推流以命名帧和标准 id 推送状态、事件与结束原因，并在终态自行收束。

    帧名让前端按类型分发，`id` 让浏览器重连自动带上 `Last-Event-ID`；两者都退化成匿名 data 时
    前端只能自己累加游标，重连必然重复或漏事件。结束原因等于 run 终态，说明推流没有把"连接结束"
    和"诊断结束"混为一谈。
    """

    runtime = FakeDiagnosisRuntime()
    runtime.run = _cancelled(runtime.run)
    async with app.router.lifespan_context(app):
        app.state.diagnosis_runtime = runtime
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            frames = await _read_sse_frames(client, f"/api/v1/runs/{runtime.run.run_id}/stream")

    assert [frame["event"] for frame in frames] == ["run_snapshot", "run_event", "stream_end"]
    assert [frame["id"] for frame in frames] == ["0", "1", "1"]
    assert runtime.stream_cursors == [0]
    payloads = [json.loads(frame["data"]) for frame in frames]
    assert all(item["contract_id"] == "run-stream:v1" for item in payloads)
    assert payloads[1]["event"]["event_type"] == "run_created"
    assert payloads[2]["end_reason"] == "cancelled"
    serialized = str(payloads)
    assert "Thought" not in serialized
    assert "reasoning_process" not in serialized


@pytest.mark.asyncio
async def test_run_stream_resumes_from_last_event_id_and_rejects_unknown_run() -> None:
    """验证重连头把游标推进到已收事件之后，未知 run 在开流前就返回 404。

    404 必须是真正的 HTTP 状态码：一旦响应体开始，状态码已经发出，未知 run 只能表达为流内一帧，
    而浏览器会把它当作网络抖动继续重连。带 `Last-Event-ID` 的重连则不应重复推送序号 1。
    """

    runtime = FakeDiagnosisRuntime()
    runtime.run = _cancelled(runtime.run)
    async with app.router.lifespan_context(app):
        app.state.diagnosis_runtime = runtime
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with client.stream(
                "GET",
                f"/api/v1/runs/{runtime.run.run_id}/stream",
                headers={"Last-Event-ID": "1"},
            ) as response:
                assert response.status_code == 200
                body = "".join([chunk async for chunk in response.aiter_text()])
            missing = await client.get("/api/v1/runs/run_aaaaaaaaaaaaaaaa/stream")

    assert "event: run_event" not in body
    assert "event: stream_end" in body
    assert runtime.stream_cursors == [1]
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_run_stream_returns_503_when_runtime_is_disabled() -> None:
    """验证 runtime 未装配时推流返回 503，而不是一条永远不产生事件的空流。

    空流在浏览器里表现为"一直转圈"，演示者无法区分"没部署"和"诊断很慢"；503 让前端立刻退回轮询
    并显示明确原因，与 trace/metrics 在同种情况下的行为保持一致。
    """

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/runs/run_2222222222222222/stream")

    assert response.status_code == 503
