"""验证真实模型 Golden 入口的案例选择、路由隔离和配置失败边界。

测试不访问真实模型或 PostgreSQL；它使用已校验 Fixture 和只记录消息的 runtime 替身，证明 runner
只把合成路由元数据送入生产消息，不泄露 Golden 根因、必要工具、证据答案或停止原因。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.capabilities import DiagnosisIntent, HistoryTrigger
from app.core.fixture_registry import FixtureRegistry, load_golden_cases
from app.core.settings import Settings
from app.evaluation.live_golden import (
    LIVE_GOLDEN_SMOKE_CASE_IDS,
    LiveGoldenRunner,
    build_live_golden_message,
    run_live_golden_evaluation,
    select_live_golden_cases,
)
from app.orchestration.diagnosis_models import DiagnosisRunResult
from app.orchestration.run_models import AgentRunSnapshot, AgentRunStatus, DiagnosisSession

FIXTURE_DIRECTORY = Path("data/fixtures/scenarios")
GOLDEN_CASE_FILE = Path("data/fixtures/golden_cases.json")


class RecordingDiagnosisRuntime:
    """记录 runner 提交内容并返回预先构造的 completed 结果。

    替身不执行模型、数据库或 MCP；它只验证 LiveGoldenRunner 的资源隔离和终态提取。生产路径是否
    使用真实依赖由 CLI lifespan 与既有各层集成测试共同保证。
    """

    def __init__(self, result: DiagnosisRunResult) -> None:
        """保存返回结果并初始化会话标题、ID 和消息捕获槽位。

        ``model_construct`` 仅在各方法内生成最小壳对象，避免本测试重复构造与路由逻辑无关的完整
        报告；result 身份仍是 DiagnosisRunResult，runner 不会收到松散字典。
        """

        self.result = result
        self.created_titles: list[str] = []
        self.submitted: list[tuple[str, object]] = []

    async def create_session(self, *, title: str) -> DiagnosisSession:
        """记录独立案例标题并返回具有合法稳定 ID 的最小会话壳。

        方法没有 I/O；固定 ID 只服务单案例测试。真实 CLI 每案由 PostgreSQL runtime 生成不同身份，
        不使用本替身的固定值。
        """

        self.created_titles.append(title)
        return DiagnosisSession.model_construct(session_id="session_0123456789abcdef")

    async def submit_message(
        self,
        session_id: str,
        message: object,
    ) -> AgentRunSnapshot:
        """捕获 session/message 并返回 completed 强类型快照壳。

        快照只填 runner 实际读取的 status/result；跳过仓储时间字段是有意缩小单测关注面，完整
        AgentRunSnapshot 不变量已由资源 API 集成测试覆盖。
        """

        self.submitted.append((session_id, message))
        return AgentRunSnapshot.model_construct(
            status=AgentRunStatus.COMPLETED,
            result=self.result,
        )


def test_default_live_selection_covers_three_representative_categories() -> None:
    """验证空选择使用固定三案例冒烟集且保持声明顺序。

    顺序稳定才能比较多次真实模型结果；三个案例分别覆盖单组件、跨组件和事实冲突，未知测试环境
    状态不能改变默认集合或偷偷扩大付费调用规模。
    """

    cases = load_golden_cases(GOLDEN_CASE_FILE)

    selected = select_live_golden_cases(cases, ())

    assert tuple(case.case_id for case in selected) == LIVE_GOLDEN_SMOKE_CASE_IDS
    assert {case.case_category.value for case in selected} == {
        "single_component",
        "cross_component",
        "tool_anomaly_or_conflict",
    }


def test_live_message_contains_routing_metadata_but_not_golden_answers() -> None:
    """验证合成场景路由可生成合法 Action 输入，同时不把评分答案送给模型。

    检查内容包含 scenario/resource/window，却不含 allowed roots、required tool 名、证据 source ID 或
    expected stop reason；这保证真实模型分数不是由 Prompt 直接抄 Golden 标注得到。
    """

    cases = load_golden_cases(GOLDEN_CASE_FILE)
    registry = FixtureRegistry.from_directory(FIXTURE_DIRECTORY)
    case = next(
        item
        for item in cases
        if item.case_id == "golden_bds_conflicting_partition_evidence"
    )

    message = build_live_golden_message(case, registry.get(case.scenario_id))

    assert message.intent is DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS
    assert message.history_trigger is HistoryTrigger.NOT_REQUESTED
    assert "scenario_id=bds_conflicting_partition_evidence" in message.content
    assert "resource_ids=bds_inventory_snapshot_hourly,dwd_inventory_snapshot" in message.content
    assert "observation_window=" in message.content
    for forbidden in (
        *case.allowed_root_causes,
        *(tool.value for tool in case.required_tools),
        *case.required_evidence_sources,
        *case.expected_stop_reasons,
    ):
        assert forbidden not in message.content


@pytest.mark.asyncio
async def test_live_runner_uses_one_isolated_session_and_returns_completed_result() -> None:
    """验证 runner 通过资源 runtime 提交生产消息并提取终态 DiagnosisRunResult。

    测试不模拟答案内容；最小 result 壳仅作为身份哨兵。捕获项证明标题绑定 case ID、提交使用新会话，
    返回值来自 runtime snapshot 而非 runner 读取 Golden 标注后自行构造。
    """

    cases = load_golden_cases(GOLDEN_CASE_FILE)
    registry = FixtureRegistry.from_directory(FIXTURE_DIRECTORY)
    case = next(
        item
        for item in cases
        if item.case_id == "golden_lts_invalid_partition_parameter_single"
    )
    result = DiagnosisRunResult.model_construct()
    runtime = RecordingDiagnosisRuntime(result)
    runner = LiveGoldenRunner(runtime, registry)

    actual = await runner.run(case)

    assert actual is result
    assert runtime.created_titles == [f"Live Golden: {case.case_id}"]
    assert runtime.submitted[0][0] == "session_0123456789abcdef"
    assert "scenario_id=lts_parameter_validation_failure" in runtime.submitted[0][1].content


@pytest.mark.asyncio
async def test_live_evaluation_rejects_disabled_provider_before_app_lifespan() -> None:
    """验证默认 disabled 配置在数据库、MCP 和付费模型初始化前明确失败。

    该边界避免 CI 或求职演示误把未运行占位报告写成 measured；错误发生在导入 FastAPI app 之前，
    因而也不会启动 stdio 子进程或创建 PostgreSQL 连接。
    """

    with pytest.raises(ValueError, match="DATAOPS_CHAT_PROVIDER"):
        await run_live_golden_evaluation(
            settings=Settings(),
            code_revision="test-revision",
        )
