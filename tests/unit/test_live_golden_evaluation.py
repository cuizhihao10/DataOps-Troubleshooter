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
    build_argument_parser,
    build_live_golden_message,
    resolve_live_golden_scope,
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


def test_scope_separates_smoke_full_and_arbitrary_subsets() -> None:
    """验证三档 scope 真正区分冒烟集、Golden 全集与随手挑选的子集。

    scope 是读者判断分母的唯一字段：若全集运行和任意子集共用 ``custom``，28/28 的成绩就无法与
    "我挑了 28 条里的 5 条"区分。smoke 用序列比较以保证多轮可比，full 用集合比较因为覆盖全集与
    执行顺序无关；少一条即退回 custom，不允许"接近全集"被读成全量。
    """

    all_case_ids = tuple(case.case_id for case in load_golden_cases(GOLDEN_CASE_FILE))

    assert resolve_live_golden_scope(LIVE_GOLDEN_SMOKE_CASE_IDS, all_case_ids) == "smoke"
    assert resolve_live_golden_scope(all_case_ids, all_case_ids) == "full"
    assert resolve_live_golden_scope(tuple(reversed(all_case_ids)), all_case_ids) == "full"
    assert resolve_live_golden_scope(all_case_ids[:-1], all_case_ids) == "custom"
    assert resolve_live_golden_scope(all_case_ids[:1], all_case_ids) == "custom"


@pytest.mark.asyncio
async def test_all_cases_and_explicit_case_ids_are_mutually_exclusive() -> None:
    """验证同时给出 ``--all-cases`` 与 ``--case-id`` 会在付费调用前失败而不是静默取其一。

    两个选项都表达"跑哪些案例"，静默偏向任何一侧都会产生 scope 与实际分母不符的报告；因此拒绝
    发生在 Provider 预检之后、加载 app 与产生模型费用之前。
    """

    settings = Settings(
        chat_provider="openai-compatible",
        chat_api_key="test-key-not-used-because-selection-fails-first",
        database_url="postgresql+asyncpg://user:pass@127.0.0.1:5432/db",
    )

    with pytest.raises(ValueError, match="--all-cases cannot be combined with --case-id"):
        await run_live_golden_evaluation(
            settings=settings,
            code_revision="test-revision",
            requested_case_ids=(LIVE_GOLDEN_SMOKE_CASE_IDS[0],),
            run_all_cases=True,
        )


def test_all_cases_flag_is_off_by_default_in_the_cli() -> None:
    """验证 CLI 默认不展开全集，避免一次误敲命令产生 28 条真实模型调用的费用。

    真实模型评测是显式 opt-in 的付费路径，全量运行必须由 ``--all-cases`` 明确请求；解析器同时保留
    ``--case-id`` 的可重复语义，两者的组合由运行期边界拒绝。
    """

    parser = build_argument_parser()

    default_args = parser.parse_args(["--code-revision", "abc1234"])
    assert default_args.all_cases is False
    assert default_args.case_id == []

    full_args = parser.parse_args(["--code-revision", "abc1234", "--all-cases"])
    assert full_args.all_cases is True


def test_every_golden_case_builds_a_valid_live_message_without_model_calls() -> None:
    """验证全部 28 条案例都能离线构造出合法生产消息，使 ``--all-cases`` 不会跑到一半才失败。

    这条门禁来自一次真实失败：共用三组件 Fixture 的单组件案例此前从 Fixture 推导组件，第六条案例
    在构造 ``DiagnosisMessage`` 时被 capability 元数校验拒绝，而前五条案例的真实模型费用已经花掉且
    不写任何报告。断言本身不访问模型、数据库或 MCP，因此可以在每次单测里以零成本复现该边界。
    泄漏检查只针对 runner 追加的路由段：``user_query`` 是用户自己的措辞，一条记忆案例的问题里本来
    就写着"FlashSync 主键冲突"，把它算成泄漏会逼着改写案例文本去迎合测试。
    """

    cases = load_golden_cases(GOLDEN_CASE_FILE)
    registry = FixtureRegistry.from_directory(FIXTURE_DIRECTORY)

    assert len(cases) == 28
    for case in cases:
        message = build_live_golden_message(case, registry.get(case.scenario_id))
        assert message.intent is DiagnosisIntent(case.expected_intent)
        assert message.components == tuple(case.requested_components)
        assert message.content.startswith(case.user_query)
        routing = message.content[len(case.user_query) :]
        # 组件范围是输入，但工具名、根因、证据来源和停止原因始终不得出现在追加的路由段里。
        for forbidden in (
            *case.allowed_root_causes,
            *(tool.value for tool in case.required_tools),
            *case.required_evidence_sources,
            *case.expected_stop_reasons,
        ):
            assert forbidden not in routing
