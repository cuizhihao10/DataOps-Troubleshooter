"""通过生产诊断运行时执行可选的真实模型 Golden 冒烟评测。

该入口在 FastAPI lifespan 内复用真实 PostgreSQL GraphRAG、Planner/Auditor Structured Outputs、
LangGraph 和 stdio MCP，而不是直接读取 Fixture 拼装答案。v1 默认选择三类代表案例，并只把合成
场景路由元数据追加到用户问题；Golden 允许根因、必要工具、证据答案和评分规则绝不进入 Prompt。
输出同时包含现有 Golden 评分与脱敏模型调用遥测，未配置数据库或模型密钥时在付费调用前失败。
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.capabilities import DiagnosisIntent, HistoryTrigger
from app.core.fixture_registry import FixtureRegistry, load_golden_cases
from app.core.settings import Settings, get_settings
from app.domain.scenarios import GoldenCaseCategory, GoldenCaseSpec, ScenarioFixture
from app.evaluation.golden_diagnosis import (
    GoldenDiagnosisEvalReport,
    evaluate_golden_diagnosis,
)
from app.evaluation.live_history_seed import (
    LiveHistorySeedReport,
    seed_live_golden_history,
)
from app.observability import (
    InMemoryModelCallRecorder,
    ModelCallMetric,
    ModelCallStatus,
    bind_model_call_recorder,
    reset_model_call_recorder,
)
from app.orchestration.diagnosis_models import DiagnosisRunResult
from app.orchestration.diagnosis_worker import DiagnosisRunWorker
from app.orchestration.run_models import (
    AgentRunSnapshot,
    AgentRunStatus,
    DiagnosisMessage,
    DiagnosisSession,
)

LIVE_GOLDEN_EVAL_CONTRACT_ID = "live-golden-eval:v3"
LIVE_GOLDEN_SMOKE_CASE_IDS = (
    "golden_lts_invalid_partition_parameter_single",
    "golden_cross_lts_bds_flashsync_watermark_timezone_mismatch",
    "golden_bds_conflicting_partition_evidence",
)


class LiveGoldenSetupError(ValueError):
    """表示真实模型评测在执行依赖或付费调用前发现的可修复设置错误。

    CLI 只把该异常转换为简短 argparse 消息；运行中的数据库、MCP、模型、Pydantic 或评分错误保留
    原异常与 traceback，避免把代码回归误报成用户少传一个参数。
    """


class LiveDiagnosisRuntime(Protocol):
    """声明真实 Golden runner 所需的最小资源化诊断运行时接口。

    协议与 FastAPI 使用的生产 runtime 方法一致，但不依赖具体 PostgreSQL 类，便于单测注入只记录
    输入的替身。实现必须持久化 session/run，并返回强类型终态快照，不能直接构造评分答案。
    """

    async def create_session(self, *, title: str) -> DiagnosisSession:
        """创建独立评测会话并返回已提交的资源身份。

        每条案例使用新 session，避免上一案例 checkpoint 或证据进入下一案例；数据库错误必须向上
        传播，不能退回仅内存会话后继续声称是生产路径实测。
        """

        ...

    async def submit_message(
        self,
        session_id: str,
        message: DiagnosisMessage,
    ) -> AgentRunSnapshot | None:
        """提交结构化消息并返回完成、失败或不存在语义的 run 快照。

        v1 生产 runtime 同步执行，因此成功必须返回 completed；``None`` 表示会话身份异常。未来改为
        Worker 后 runner 应轮询 GET/仓储契约，而不是把 running 当成零分结果。
        """

        ...

    async def get_run(self, run_id: str) -> AgentRunSnapshot | None:
        """读取已持久化的 run 快照，供真实后台 Worker 完成后轮询终态。

        实现必须按 run_id 返回 queued/running/terminal 快照或 ``None``；调用方不会重新执行
        workflow。测试替身可以直接返回固定快照，但生产实现必须经过 PostgreSQL/Pydantic 边界。
        """

        ...


class LiveGoldenEvalReport(BaseModel):
    """封装一次真实模型评测的可复现版本、成本遥测和 Golden 得分。

    报告只允许 ``measured``，因为没有密钥时 CLI 会失败而不会生成占位成绩。模型调用明细不含文本；
    聚合字段通过 validator 从明细重算，防止手工修改 token、调用数或修复失败数。v3 增加
    ``history_seed``：记忆类指标是否有非空分母属于运行前置条件而不是模型表现，报告必须自己说清楚。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: Literal["live-golden-eval:v3"]
    metric_kind: Literal["measured"] = "measured"
    scope: Literal["smoke", "full", "custom"]
    code_revision: str = Field(min_length=1, max_length=100, pattern=r"\S")
    selected_case_ids: tuple[str, ...] = Field(min_length=1)
    chat_provider: Literal["openai-compatible"]
    chat_model: str = Field(min_length=1, max_length=200)
    planner_prompt_id: str = Field(min_length=1, max_length=100)
    auditor_prompt_id: str = Field(min_length=1, max_length=100)
    embedding_provider: str = Field(min_length=1, max_length=100)
    golden_case_contract_id: str = Field(min_length=1, max_length=100)
    started_at: datetime
    completed_at: datetime
    duration_ms: float = Field(ge=0)
    model_call_count: int = Field(ge=1)
    output_invalid_call_count: int = Field(ge=0)
    unreported_usage_call_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    model_calls: tuple[ModelCallMetric, ...] = Field(min_length=1)
    # None 表示本轮没有预置历史案例：数据库里没有 confirmed 案例时四个记忆指标必然是 0，那是前置
    # 条件缺失而不是模型表现。字段可选而不是必填，是为了让 v1/v2 时代已发布的运行仍能被原样解释。
    history_seed: LiveHistorySeedReport | None = None
    golden_report: GoldenDiagnosisEvalReport

    @model_validator(mode="after")
    def validate_derived_measurements(self) -> LiveGoldenEvalReport:
        """从明细重算身份、时间、调用状态和 token 聚合字段。

        该校验把报告定位为不可手工美化的测量工件：案例顺序必须等于 Golden 明细，时间必须带时区
        且单调，token 仅汇总供应商实际报告的 usage；未报告的调用单独计数而不是补零后隐藏。
        """

        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("live Golden timestamps must include a timezone")
        if self.completed_at < self.started_at:
            raise ValueError("live Golden completion cannot precede start")
        case_ids = tuple(case.case_id for case in self.golden_report.cases)
        if self.selected_case_ids != case_ids:
            raise ValueError("live Golden selected cases must match scored case order")
        if self.model_call_count != len(self.model_calls):
            raise ValueError("live Golden model_call_count must match call details")
        invalid_calls = sum(
            call.status is ModelCallStatus.OUTPUT_INVALID for call in self.model_calls
        )
        if self.output_invalid_call_count != invalid_calls:
            raise ValueError("live Golden invalid-call count must match call details")
        reported = [call.token_usage for call in self.model_calls if call.token_usage is not None]
        if self.unreported_usage_call_count != len(self.model_calls) - len(reported):
            raise ValueError("live Golden missing-usage count must match call details")
        expected_input = sum(usage.input_tokens for usage in reported)
        expected_output = sum(usage.output_tokens for usage in reported)
        if (self.input_tokens, self.output_tokens, self.total_tokens) != (
            expected_input,
            expected_output,
            expected_input + expected_output,
        ):
            raise ValueError("live Golden token totals must match reported call usage")
        return self


# 显式补建 schema，因为本模块会以 `__main__` 身份被执行。`from __future__ import annotations` 让所有
# 注解都是字符串，而 Pydantic 按 `cls.__module__` 回查命名空间；用 `runpy.run_module(run_name=
# "__main__")` 这类嵌套方式装载时，`sys.modules["__main__"]` 并不是正在执行的这份命名空间，于是
# `Literal` 解析不到，schema 被推迟成 mock，直到第一次实例化才补建。报告恰好是全部案例跑完后才构造
# 的对象：实测有一次 28 条案例全部执行完毕、在 asyncio 深栈里补建失败抛 PydanticUserError，付费结果
# 一条未落盘。导入期补建把这类失败从"整轮结果作废"降级为"进程起不来"。
LiveGoldenEvalReport.model_rebuild()


class LiveGoldenRunner:
    """把 Golden 输入投影为生产 ``DiagnosisMessage`` 并执行完整诊断运行时。

    runner 只读取 case 的问题、场景身份和期望意图，不读取允许根因、必要工具、证据来源或停止答案。
    Fixture 仅提供组件、资源和观察窗口等 MCP 路由元数据，使真实模型能在合成环境中形成合法 Action。
    """

    def __init__(
        self,
        runtime: LiveDiagnosisRuntime,
        fixture_registry: FixtureRegistry,
        *,
        worker: DiagnosisRunWorker | None = None,
        poll_interval_seconds: float = 0.05,
        timeout_seconds: float = 300.0,
    ) -> None:
        """注入已由 lifespan 验证的生产 runtime 和同版本 Fixture 注册表。

        构造不执行数据库、模型或 MCP I/O；未知 scenario 会在逐案构建消息时显式失败。依赖按协议
        注入使测试能验证没有 Golden 答案泄漏，同时生产 CLI 仍使用 app.state 的真实对象。
        """

        self._runtime = runtime
        self._fixture_registry = fixture_registry
        self._worker = worker
        self._poll_interval_seconds = poll_interval_seconds
        self._timeout_seconds = timeout_seconds

    async def run(self, case: GoldenCaseSpec) -> DiagnosisRunResult:
        """为单条案例创建隔离 session，提交消息并返回 completed 诊断结果。

        ``None``、running、failed 或缺失 result 都是评测基础设施失败并立即抛错，不会被评分为模型
        零分。每案独立 session 防止 checkpoint 污染；所有外部 I/O 仍由生产 runtime 负责。
        """

        scenario = self._fixture_registry.get(case.scenario_id)
        message = build_live_golden_message(case, scenario)
        session = await self._runtime.create_session(title=f"Live Golden: {case.case_id}")
        snapshot = await self._runtime.submit_message(session.session_id, message)
        if snapshot is None:
            raise RuntimeError(f"live Golden session disappeared: {case.case_id}")
        # 生产 runtime 只返回 queued；评测显式驱动一个 Worker 轮次，保证 recorder ContextVar
        # 继承到执行 task，同时仍然复用真实的 claim/lease/完成事务路径。
        if self._worker is not None:
            await self._worker.run_once()
            deadline = perf_counter() + self._timeout_seconds
            while snapshot.status not in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
            }:
                if perf_counter() >= deadline:
                    raise TimeoutError(f"live Golden run timed out: {case.case_id}")
                await asyncio.sleep(self._poll_interval_seconds)
                snapshot = await self._runtime.get_run(snapshot.run_id)
                if snapshot is None:
                    raise RuntimeError(f"live Golden run disappeared: {case.case_id}")
        if snapshot.status is not AgentRunStatus.COMPLETED or snapshot.result is None:
            raise RuntimeError(
                f"live Golden run did not complete: {case.case_id} status={snapshot.status.value}"
            )
        return snapshot.result


def build_live_golden_message(
    case: GoldenCaseSpec,
    scenario: ScenarioFixture,
) -> DiagnosisMessage:
    """构造只含用户问题与合成路由元数据的生产消息。

    资源 ID、scenario ID 和观察窗口是 Mock MCP 定位输入，不是诊断答案。函数刻意不读取
    ``required_tools``、``allowed_root_causes``、证据来源、路径或停止原因；历史类只把触发来源标为
    user_requested，仍由 confirmed-only 数据库检索决定实际召回。组件范围取案例显式声明的
    ``requested_components``（真实产品里由用户勾选），而不是 Fixture 的组件列表：多条单组件案例
    共用同一个三组件 Fixture，按 Fixture 推导会与单组件意图的元数要求直接冲突。
    """

    if scenario.scenario_id != case.scenario_id:
        raise ValueError("live Golden scenario must match case scenario_id")
    resources = sorted({result.request.resource_id for result in scenario.tool_results})
    starts = [result.request.time_range.start for result in scenario.tool_results]
    ends = [result.request.time_range.end for result in scenario.tool_results]
    routing = (
        "\n\n[合成评测路由元数据]\n"
        f"scenario_id={scenario.scenario_id}\n"
        f"resource_ids={','.join(resources)}\n"
        f"observation_window={min(starts).isoformat()}/{max(ends).isoformat()}\n"
        "以上字段只用于构造只读 MCP 请求，不代表必要工具、证据或诊断答案。"
    )
    # 组件范围与意图的自洽性由 GoldenCaseSpec 在加载阶段保证，因此这里不需要再兜底裁剪；
    # 一旦案例数据违约，失败发生在读取 JSON 时而不是烧掉真实模型费用之后。
    components = tuple(case.requested_components)
    history_trigger = (
        HistoryTrigger.USER_REQUESTED
        if case.case_category is GoldenCaseCategory.MEMORY_RECALL
        else HistoryTrigger.NOT_REQUESTED
    )
    return DiagnosisMessage(
        content=case.user_query + routing,
        intent=DiagnosisIntent(case.expected_intent),
        components=components,
        history_trigger=history_trigger,
    )


def select_live_golden_cases(
    cases: Sequence[GoldenCaseSpec],
    requested_case_ids: Sequence[str],
) -> tuple[GoldenCaseSpec, ...]:
    """按请求顺序选择案例，空请求使用版本化三案例冒烟集合。

    未知或重复 ID 在启动 app、连接数据库和产生付费模型调用前失败。默认集合分别覆盖单组件、
    三组件链路和成功响应事实冲突，控制首次真实评测成本同时保留安全性信号。
    """

    selected_ids = tuple(requested_case_ids) or LIVE_GOLDEN_SMOKE_CASE_IDS
    if len(selected_ids) != len(set(selected_ids)):
        raise LiveGoldenSetupError("live Golden case IDs must not contain duplicates")
    case_by_id = {case.case_id: case for case in cases}
    missing = [case_id for case_id in selected_ids if case_id not in case_by_id]
    if missing:
        raise LiveGoldenSetupError(f"unknown live Golden case IDs: {missing}")
    return tuple(case_by_id[case_id] for case_id in selected_ids)


def resolve_live_golden_scope(
    selected_case_ids: Sequence[str],
    all_case_ids: Sequence[str],
) -> Literal["smoke", "full", "custom"]:
    """判定一次真实模型评测的样本口径，使读者无需数案例就知道分母是什么。

    三档的存在意义是防止小样本冒充更大的口径：只有与版本化 smoke 集合逐个相同才是 ``smoke``，
    只有覆盖 Golden 数据集全部案例才是 ``full``，其余显式子集统一是 ``custom``。``full`` 用集合
    比较而不是序列比较，因为"是否覆盖全集"与执行顺序无关；smoke 仍用序列比较以保证多轮可比。
    """

    selected = tuple(selected_case_ids)
    if selected == LIVE_GOLDEN_SMOKE_CASE_IDS:
        return "smoke"
    if set(selected) == set(all_case_ids):
        return "full"
    return "custom"


def build_live_golden_report(
    *,
    settings: Settings,
    code_revision: str,
    cases: Sequence[GoldenCaseSpec],
    all_case_ids: Sequence[str],
    golden_report: GoldenDiagnosisEvalReport,
    model_calls: tuple[ModelCallMetric, ...],
    started_at: datetime,
    completed_at: datetime,
    duration_ms: float,
    history_seed: LiveHistorySeedReport | None = None,
) -> LiveGoldenEvalReport:
    """从设置、Golden 得分和调用明细构建自校验的实测报告。

    聚合只计算存在的 usage；缺失计入 ``unreported_usage_call_count``。scope 由
    :func:`resolve_live_golden_scope` 按 smoke / full / custom 三档判定，因此 28 条全集运行不会与
    随手挑选的子集共用同一个标签，小样本也无法冒充标准冒烟或全量成绩。``history_seed`` 原样透传，
    使"记忆指标为 0"能被区分成"过滤生效"与"数据库里根本没有 confirmed 案例"两种完全不同的结论。
    """

    reported_usage = [call.token_usage for call in model_calls if call.token_usage is not None]
    input_tokens = sum(usage.input_tokens for usage in reported_usage)
    output_tokens = sum(usage.output_tokens for usage in reported_usage)
    case_ids = tuple(case.case_id for case in cases)
    return LiveGoldenEvalReport(
        contract_id=LIVE_GOLDEN_EVAL_CONTRACT_ID,
        scope=resolve_live_golden_scope(case_ids, all_case_ids),
        code_revision=code_revision,
        selected_case_ids=case_ids,
        chat_provider=settings.chat_provider,
        chat_model=settings.chat_model,
        planner_prompt_id=settings.planner_prompt_id,
        auditor_prompt_id=settings.auditor_prompt_id,
        embedding_provider=settings.embedding_provider,
        golden_case_contract_id=settings.golden_case_contract_id,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        model_call_count=len(model_calls),
        output_invalid_call_count=sum(
            call.status is ModelCallStatus.OUTPUT_INVALID for call in model_calls
        ),
        unreported_usage_call_count=len(model_calls) - len(reported_usage),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        model_calls=model_calls,
        history_seed=history_seed,
        golden_report=golden_report,
    )


async def run_live_golden_evaluation(
    *,
    settings: Settings,
    code_revision: str,
    requested_case_ids: Sequence[str] = (),
    run_all_cases: bool = False,
    seed_history: bool = False,
) -> LiveGoldenEvalReport:
    """启动生产 lifespan，执行所选案例并返回真实模型测量报告。

    配置预检先于 app 启动，disabled Provider 或缺失 PostgreSQL 时不会产生模型费用。lifespan 负责
    Fixture/Prompt/MCP/数据库审计与资源释放；记录器仅包围逐案 workflow，并在 ``finally`` 恢复，
    避免污染同进程后续任务。``run_all_cases`` 按 Golden 文件声明顺序展开全集，与显式 ``--case-id``
    互斥，避免"以为跑了全集其实只跑了子集"这类无法从报告分辨的口径错误。``seed_history`` 在计时和
    模型调用之前写入 confirmed/pending/rejected 历史案例，使记忆类指标拥有真实分母；它不改变任何
    评分规则，只改变前置条件，因此开与不开的两轮不能放在同一列比较。
    """

    if settings.chat_provider == "disabled":
        raise LiveGoldenSetupError(
            "live Golden evaluation requires DATAOPS_CHAT_PROVIDER"
        )
    if settings.chat_api_key is None:
        raise LiveGoldenSetupError("live Golden evaluation requires DATAOPS_CHAT_API_KEY")
    if settings.database_url is None:
        raise LiveGoldenSetupError("live Golden evaluation requires DATAOPS_DATABASE_URL")

    cases = load_golden_cases(settings.golden_case_file)
    all_case_ids = tuple(case.case_id for case in cases)
    if run_all_cases:
        if requested_case_ids:
            raise LiveGoldenSetupError("live Golden --all-cases cannot be combined with --case-id")
        requested_case_ids = all_case_ids
    selected = select_live_golden_cases(cases, requested_case_ids)

    # 延迟导入确保配置错误在 FastAPI 模块初始化前报告，也避免普通评测导入触发应用生命周期。
    from app.api.main import app

    recorder = InMemoryModelCallRecorder()
    async with app.router.lifespan_context(app):
        runtime = app.state.diagnosis_runtime
        fixture_registry = app.state.fixture_registry
        worker = getattr(app.state, "diagnosis_worker", None)
        if runtime is None:
            raise RuntimeError("live Golden diagnosis runtime was not configured")
        if worker is None:
            raise RuntimeError("live Golden diagnosis worker was not configured")
        # lifespan 默认已启动后台循环；评测在当前 task 中绑定 recorder，因此先停掉循环，
        # 再由 runner.run_once() 驱动同一 claim 路径，避免 ContextVar 只绑定到错误的 task。
        await worker.stop()
        # 预置必须在计时和第一次付费聊天调用之前完成：写库失败要以异常终止整轮，而不是先烧掉
        # 模型费用再得到一份记忆指标全为 0、且无法判断原因的报告。
        history_seed: LiveHistorySeedReport | None = None
        if seed_history:
            memory_runtime = app.state.memory_runtime
            session_factory = app.state.session_factory
            embedding_provider = app.state.embedding_provider
            if memory_runtime is None or session_factory is None:
                raise RuntimeError("live Golden history seeding requires the memory runtime")
            history_seed = await seed_live_golden_history(
                selected,
                memory_runtime=memory_runtime,
                session_factory=session_factory,
                embedding_provider=embedding_provider,
            )
        runner = LiveGoldenRunner(runtime, fixture_registry, worker=worker)
        started_at = datetime.now(UTC)
        started_clock = perf_counter()
        token = bind_model_call_recorder(recorder)
        try:
            golden_report = await evaluate_golden_diagnosis(selected, runner)
        finally:
            # 即使模型、MCP 或评分失败也恢复 ContextVar，防止后续请求写入失败评测的 recorder。
            reset_model_call_recorder(token)
        completed_at = datetime.now(UTC)
        duration_ms = (perf_counter() - started_clock) * 1000

    return build_live_golden_report(
        settings=settings,
        code_revision=code_revision,
        cases=selected,
        all_case_ids=all_case_ids,
        golden_report=golden_report,
        model_calls=recorder.snapshot(),
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        history_seed=history_seed,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    """创建真实模型评测 CLI 参数解析器，不接受任意 shell 命令。

    code revision 必填以保证结果可追溯；``--case-id`` 可重复选择低成本子集，``--all-cases`` 展开
    Golden 全集以取得 ``scope=full`` 报告，缺省运行标准三案例。``--seed-history`` 显式打开历史案例
    预置：它会写数据库，因此默认关闭，不能由代码悄悄替使用者决定往长期记忆里放东西。output 缺省写
    stdout，指定文件时仅写已校验 JSON，不创建目录或覆盖其他隐式路径。
    """

    parser = argparse.ArgumentParser(
        description="Run opt-in live-model Golden evaluation through production runtime.",
    )
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--all-cases", action="store_true")
    parser.add_argument("--seed-history", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析参数、运行异步评测并把 ``live-golden-eval:v3`` JSON 输出到目标位置。

    异常保持非零进程退出并由 Python 输出错误，不生成半成品报告。成功时使用 Pydantic JSON 序列化
    枚举和时间；输出文件采用 UTF-8 且保留中文，便于作品集审阅和后续机器比较。
    """

    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(
            run_live_golden_evaluation(
                settings=get_settings(),
                code_revision=args.code_revision,
                requested_case_ids=args.case_id,
                run_all_cases=args.all_cases,
                seed_history=args.seed_history,
            )
        )
    except LiveGoldenSetupError as exc:
        # 配置/选择错误使用 argparse 的短消息和退出码 2，不用长 traceback 淹没真正修复项。
        parser.error(str(exc))
    rendered = report.model_dump_json(indent=2)
    if args.output is None:
        print(rendered)
    else:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
