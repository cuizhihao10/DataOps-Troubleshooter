"""评测长期记忆关闭/启用对端到端诊断行为和报告安全性的实际影响。

本模块实现产品设计要求的 Memory off/on 消融：同一条合成案例依次运行不召回历史与召回历史
两种模式，再从真实 ``DiagnosisRunResult`` 中计算必要 Action 覆盖、根因命中、实时引用完整率、
历史案例投影和冲突保护。评测不读取模型 Thought，也不把历史相似度当作当前事实置信度。
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.capabilities import HistoryTrigger
from app.domain.models import Component, EvidenceSourceType
from app.domain.tooling import ToolName
from app.orchestration.diagnosis_models import DiagnosisRunResult

HISTORY_IMPACT_EVAL_CONTRACT_ID = "history-impact-eval:v1"


class HistoryImpactMode(StrEnum):
    """限定端到端历史影响消融只能比较关闭与启用长期记忆两种模式。

    ``memory_off`` 必须对应 ``HistoryTrigger.NOT_REQUESTED``；``memory_on`` 必须真实触发 confirmed
    案例召回。有限枚举防止评测器用模糊布尔值混淆实验组，也便于 JSON 报告稳定序列化。
    """

    MEMORY_OFF = "memory_off"
    MEMORY_ON = "memory_on"


class HistoryImpactEvalCase(BaseModel):
    """描述一条 Memory off/on 配对诊断案例的输入与客观验收标注。

    ``required_tool_names`` 用于必要 Action 覆盖；``allowed_root_causes`` 表示可接受 Top-1 根因；
    ``forbidden_root_causes`` 专门标注不能由旧案例覆盖到本次报告的根因。场景和输入均为合成值。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^history_impact_[a-z0-9][a-z0-9_-]{2,79}$")
    user_query: str = Field(min_length=1, max_length=2000)
    components: list[Component] = Field(min_length=1, max_length=3)
    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    required_tool_names: list[ToolName] = Field(min_length=1)
    optional_tool_names: list[ToolName] = Field(default_factory=list)
    allowed_root_causes: list[str] = Field(min_length=1)
    forbidden_root_causes: list[str] = Field(default_factory=list)
    minimum_history_matches: int = Field(default=1, ge=1, le=20)
    expect_history_conflict: bool = False

    @model_validator(mode="after")
    def validate_annotations(self) -> HistoryImpactEvalCase:
        """拒绝重复、矛盾或跨组件越界的 Action/根因标注。

        校验先保证集合唯一且 required/optional、allowed/forbidden 不重叠，再检查工具前缀属于已声明
        组件。冲突案例必须至少提供一个 forbidden 根因，使“实时证据优先”具有可执行判定标准。
        """

        collections = (
            self.components,
            self.required_tool_names,
            self.optional_tool_names,
            self.allowed_root_causes,
            self.forbidden_root_causes,
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("history impact eval annotations must not contain duplicates")
        if set(self.required_tool_names) & set(self.optional_tool_names):
            raise ValueError("required and optional history impact tools must not overlap")
        if set(self.allowed_root_causes) & set(self.forbidden_root_causes):
            raise ValueError("allowed and forbidden history impact roots must not overlap")
        if self.expect_history_conflict and not self.forbidden_root_causes:
            raise ValueError("history conflict cases require at least one forbidden root cause")

        # 工具名称的协议前缀就是组件标识；在加载 fixture 时拦截跨组件工具，避免评测脚本自己
        # 违反 capability 白名单后仍把失败误报为 Planner 退化。
        component_values = {component.value for component in self.components}
        annotated_tools = [*self.required_tool_names, *self.optional_tool_names]
        invalid_tools = [
            tool.value
            for tool in annotated_tools
            if tool.value.split(".", maxsplit=1)[0] not in component_values
        ]
        if invalid_tools:
            raise ValueError(
                f"history impact tools must belong to declared components: {invalid_tools}"
            )
        return self


class HistoryImpactEvalSuite(BaseModel):
    """封装版本化 Memory off/on 案例集并执行跨案例唯一性检查。

    首版至少要求三条案例，匹配产品设计中的三类长期记忆 Golden Case；同时至少包含一条历史冲突
    案例，防止评测只证明“案例能展示”却没有验证“旧结论不能覆盖实时 Observation”。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: Literal["history-impact-eval:v1"]
    cases: list[HistoryImpactEvalCase] = Field(min_length=3)

    @model_validator(mode="after")
    def validate_case_identity_and_coverage(self) -> HistoryImpactEvalSuite:
        """检查 case/query/scenario 唯一，并强制 suite 覆盖至少一个冲突保护场景。

        任一重复都可能让 runner 返回错误脚本或复用状态，因此在调用 LangGraph、数据库或模型前
        失败；缺少冲突案例同样拒绝，避免生成安全维度不完整的平均值。
        """

        case_ids = [case.case_id for case in self.cases]
        queries = [case.user_query for case in self.cases]
        scenario_ids = [case.scenario_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("history impact eval case IDs must be unique")
        if len(queries) != len(set(queries)):
            raise ValueError("history impact eval user queries must be unique")
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("history impact eval scenario IDs must be unique")
        if not any(case.expect_history_conflict for case in self.cases):
            raise ValueError("history impact eval suite requires a conflict guard case")
        return self


class HistoryImpactModeMetrics(BaseModel):
    """保存一条案例在单个记忆模式下的行为、质量和安全实测指标。

    Action 从真实 ``ToolEvent`` 提取，只有实际进入执行器的工具才计入覆盖；根因与引用从最终审计
    报告读取。历史投影按 raw recalled ID 与报告 similar case ID 的有序一致性计算。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: HistoryImpactMode
    executed_tools: list[ToolName]
    required_action_hits: list[ToolName]
    missing_required_actions: list[ToolName]
    unexpected_actions: list[ToolName]
    necessary_action_coverage: float = Field(ge=0, le=1)
    unexpected_action_rate: float = Field(ge=0, le=1)
    reported_root_causes: list[str]
    root_cause_top1_hit: bool
    forbidden_root_cause_hits: list[str]
    realtime_root_cause_citation_rate: float = Field(ge=0, le=1)
    history_match_count: int = Field(ge=0)
    projected_history_case_ids: list[str]
    history_projection_complete: bool
    conflict_guard_passed: bool | None = None


class HistoryImpactCaseReport(BaseModel):
    """保存一条案例的 off/on 指标、差值和跨模式不变量判定。

    ``top1_root_cause_preserved`` 要求两组 Top-1 根因相同且都命中允许集合；
    ``realtime_priority_preserved`` 进一步要求 memory-on 没有 forbidden 根因，且所有根因都有
    TOOL 引用。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    memory_off: HistoryImpactModeMetrics
    memory_on: HistoryImpactModeMetrics
    action_coverage_delta: float = Field(ge=-1, le=1)
    unexpected_action_rate_delta: float = Field(ge=-1, le=1)
    top1_root_cause_preserved: bool
    realtime_priority_preserved: bool
    action_regressed: bool


class HistoryImpactEvalReport(BaseModel):
    """汇总 suite 的逐案例报告和 macro 平均 Memory off/on 实测值。

    所有数值固定标记 ``measured``，只代表当前版本化合成集和 runner 配置。报告分开统计行为增益、
    根因稳定性、引用完整性和冲突安全，不把“显示了历史案例”直接等同于诊断准确率提升。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: Literal["history-impact-eval:v1"]
    metric_kind: Literal["measured"] = "measured"
    case_reports: list[HistoryImpactCaseReport] = Field(min_length=1)
    memory_off_macro_action_coverage: float = Field(ge=0, le=1)
    memory_on_macro_action_coverage: float = Field(ge=0, le=1)
    action_coverage_delta: float = Field(ge=-1, le=1)
    memory_off_macro_unexpected_action_rate: float = Field(ge=0, le=1)
    memory_on_macro_unexpected_action_rate: float = Field(ge=0, le=1)
    unexpected_action_rate_delta: float = Field(ge=-1, le=1)
    memory_off_root_cause_hit_rate: float = Field(ge=0, le=1)
    memory_on_root_cause_hit_rate: float = Field(ge=0, le=1)
    memory_off_realtime_citation_rate: float = Field(ge=0, le=1)
    memory_on_realtime_citation_rate: float = Field(ge=0, le=1)
    history_projection_pass_rate: float = Field(ge=0, le=1)
    conflict_guard_pass_rate: float = Field(ge=0, le=1)
    action_regression_count: int = Field(ge=0)
    realtime_priority_failure_count: int = Field(ge=0)


class HistoryImpactRunner(Protocol):
    """声明评测器执行同一案例两种记忆模式所需的最小异步接口。

    生产演示 runner、真实 LangGraph 集成 runner 和单元测试脚本均可实现；异常必须传播，评测器
    不能把 Provider、MCP 或数据库失败转换成零分后继续输出不完整平均值。
    """

    async def run(
        self,
        case: HistoryImpactEvalCase,
        *,
        mode: HistoryImpactMode,
    ) -> DiagnosisRunResult:
        """运行一条已校验案例并返回完整顶层诊断结果。

        runner 必须保持 ``case.user_query`` 不变，只根据 ``mode`` 切换历史召回触发；返回结果仍需
        通过 ``DiagnosisRunResult`` 的跨阶段、confirmed-only 和审计/记忆一致性校验。
        """

        ...


def load_history_impact_eval_suite(path: Path) -> HistoryImpactEvalSuite:
    """从 UTF-8 JSON 加载并完整校验版本化端到端历史影响评测集。

    文件不存在、JSON 语法错误、重复标识、组件越界工具或缺失冲突案例都会显式抛出；JSON 本身
    不支持注释，字段原理由本模块 docstring、实现指南和 Schema 单测共同说明。
    """

    if not path.is_file():
        raise FileNotFoundError(f"history impact eval suite does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return HistoryImpactEvalSuite.model_validate(payload)


async def evaluate_history_impact(
    suite: HistoryImpactEvalSuite,
    runner: HistoryImpactRunner,
) -> HistoryImpactEvalReport:
    """顺序运行每条案例的 memory-off/on 配对实验并汇总 macro 实测指标。

    每对实验共享同一个已校验 case，且先跑 off 再跑 on，方便 runner 用稳定脚本或隔离资源重放。
    评测器验证 trigger、查询文本和最小召回数，随后只读取结构化结果；任一对照污染立即失败。
    """

    case_reports: list[HistoryImpactCaseReport] = []
    for case in suite.cases:
        # 唯一批准变量是历史上下文开关；case 对象同时固定输入、必要 Action 和根因安全标注。
        memory_off_result = await runner.run(case, mode=HistoryImpactMode.MEMORY_OFF)
        memory_on_result = await runner.run(case, mode=HistoryImpactMode.MEMORY_ON)
        _validate_paired_results(case, memory_off_result, memory_on_result)

        memory_off = _measure_mode(case, memory_off_result, mode=HistoryImpactMode.MEMORY_OFF)
        memory_on = _measure_mode(case, memory_on_result, mode=HistoryImpactMode.MEMORY_ON)
        off_top1 = memory_off.reported_root_causes[:1]
        on_top1 = memory_on.reported_root_causes[:1]
        case_reports.append(
            HistoryImpactCaseReport(
                case_id=case.case_id,
                memory_off=memory_off,
                memory_on=memory_on,
                action_coverage_delta=(
                    memory_on.necessary_action_coverage - memory_off.necessary_action_coverage
                ),
                unexpected_action_rate_delta=(
                    memory_on.unexpected_action_rate - memory_off.unexpected_action_rate
                ),
                top1_root_cause_preserved=(
                    memory_off.root_cause_top1_hit
                    and memory_on.root_cause_top1_hit
                    and off_top1 == on_top1
                ),
                realtime_priority_preserved=(
                    memory_on.root_cause_top1_hit
                    and not memory_on.forbidden_root_cause_hits
                    and memory_on.realtime_root_cause_citation_rate == 1.0
                    and memory_on.conflict_guard_passed is not False
                ),
                action_regressed=(
                    memory_on.necessary_action_coverage < memory_off.necessary_action_coverage
                ),
            )
        )

    case_count = len(case_reports)
    conflict_reports = [
        report.memory_on
        for report in case_reports
        if report.memory_on.conflict_guard_passed is not None
    ]
    off_action_coverage = _average(
        [report.memory_off.necessary_action_coverage for report in case_reports]
    )
    on_action_coverage = _average(
        [report.memory_on.necessary_action_coverage for report in case_reports]
    )
    off_unexpected_rate = _average(
        [report.memory_off.unexpected_action_rate for report in case_reports]
    )
    on_unexpected_rate = _average(
        [report.memory_on.unexpected_action_rate for report in case_reports]
    )
    return HistoryImpactEvalReport(
        contract_id=HISTORY_IMPACT_EVAL_CONTRACT_ID,
        case_reports=case_reports,
        memory_off_macro_action_coverage=off_action_coverage,
        memory_on_macro_action_coverage=on_action_coverage,
        action_coverage_delta=on_action_coverage - off_action_coverage,
        memory_off_macro_unexpected_action_rate=off_unexpected_rate,
        memory_on_macro_unexpected_action_rate=on_unexpected_rate,
        unexpected_action_rate_delta=on_unexpected_rate - off_unexpected_rate,
        memory_off_root_cause_hit_rate=(
            sum(report.memory_off.root_cause_top1_hit for report in case_reports) / case_count
        ),
        memory_on_root_cause_hit_rate=(
            sum(report.memory_on.root_cause_top1_hit for report in case_reports) / case_count
        ),
        memory_off_realtime_citation_rate=_average(
            [report.memory_off.realtime_root_cause_citation_rate for report in case_reports]
        ),
        memory_on_realtime_citation_rate=_average(
            [report.memory_on.realtime_root_cause_citation_rate for report in case_reports]
        ),
        history_projection_pass_rate=(
            sum(report.memory_on.history_projection_complete for report in case_reports)
            / case_count
        ),
        conflict_guard_pass_rate=(
            sum(metrics.conflict_guard_passed is True for metrics in conflict_reports)
            / len(conflict_reports)
        ),
        action_regression_count=sum(report.action_regressed for report in case_reports),
        realtime_priority_failure_count=sum(
            not report.realtime_priority_preserved for report in case_reports
        ),
    )


def _validate_paired_results(
    case: HistoryImpactEvalCase,
    memory_off: DiagnosisRunResult,
    memory_on: DiagnosisRunResult,
) -> None:
    """确认一对结果只在历史触发语义上区分且 treatment 确实召回足量案例。

    off 组必须使用 not_requested 并保持空历史；on 组必须使用显式触发、记录查询且达到最小命中。
    两组最终状态的用户问题都必须精确等于 fixture，防止 runner 偷换输入制造虚假增益。
    """

    if memory_off.history_trigger is not HistoryTrigger.NOT_REQUESTED:
        raise ValueError("memory-off history impact result must use not_requested trigger")
    if memory_off.recalled_memories or memory_off.history_case_matches:
        raise ValueError("memory-off history impact result cannot contain recalled history")
    if memory_on.history_trigger is HistoryTrigger.NOT_REQUESTED:
        raise ValueError("memory-on history impact result must trigger history recall")
    if len(memory_on.recalled_memories) < case.minimum_history_matches:
        raise ValueError(
            f"memory-on case {case.case_id} returned fewer than the required history matches"
        )
    if memory_off.report.state.user_query != case.user_query:
        raise ValueError("memory-off result changed the eval user query")
    if memory_on.report.state.user_query != case.user_query:
        raise ValueError("memory-on result changed the eval user query")


def _measure_mode(
    case: HistoryImpactEvalCase,
    result: DiagnosisRunResult,
    *,
    mode: HistoryImpactMode,
) -> HistoryImpactModeMetrics:
    """从一个完整诊断结果提取实际工具执行、报告质量与历史安全指标。

    工具按首次 ToolEvent 出现去重，重试不会重复提高覆盖；根因只读取最终审计报告。实时引用率
    仅认可 TOOL Evidence，历史 case ID 或相似图边不能单独支撑本次根因结论。
    """

    report = result.report.state.draft_report
    if report is None:
        raise ValueError("history impact evaluation requires a final diagnosis report")

    # ToolEvent 代表工具已实际进入执行边界；Planner 仅提出但被策略门禁拦截的 Action 不计覆盖。
    executed_tools = _stable_unique_tools(
        [event.tool_name for event in result.react.state.tool_events]
    )
    required = set(case.required_tool_names)
    allowed_actions = required | set(case.optional_tool_names)
    required_hits = [tool for tool in case.required_tool_names if tool in executed_tools]
    missing_actions = [tool for tool in case.required_tool_names if tool not in executed_tools]
    unexpected_actions = [tool for tool in executed_tools if tool not in allowed_actions]

    reported_roots = [cause.root_cause for cause in report.root_causes]
    allowed_roots = set(case.allowed_root_causes)
    forbidden_roots = set(case.forbidden_root_causes)
    realtime_refs = {
        evidence.evidence_id
        for evidence in result.react.state.evidence
        if evidence.source_type is EvidenceSourceType.TOOL
    }
    realtime_cited_count = sum(
        bool(set(cause.evidence_refs) & realtime_refs) for cause in report.root_causes
    )
    realtime_citation_rate = (
        realtime_cited_count / len(report.root_causes) if report.root_causes else 0.0
    )

    raw_case_ids = [match.memory.memory_id for match in result.recalled_memories]
    projected_case_ids = [match.case_id for match in report.similar_cases]
    conflict_guard = _evaluate_conflict_guard(case, result)
    return HistoryImpactModeMetrics(
        mode=mode,
        executed_tools=executed_tools,
        required_action_hits=required_hits,
        missing_required_actions=missing_actions,
        unexpected_actions=unexpected_actions,
        necessary_action_coverage=len(required_hits) / len(case.required_tool_names),
        unexpected_action_rate=(
            len(unexpected_actions) / len(executed_tools) if executed_tools else 0.0
        ),
        reported_root_causes=reported_roots,
        root_cause_top1_hit=bool(reported_roots and reported_roots[0] in allowed_roots),
        forbidden_root_cause_hits=[root for root in reported_roots if root in forbidden_roots],
        realtime_root_cause_citation_rate=realtime_citation_rate,
        history_match_count=len(raw_case_ids),
        projected_history_case_ids=projected_case_ids,
        history_projection_complete=(raw_case_ids == projected_case_ids),
        conflict_guard_passed=conflict_guard,
    )


def _evaluate_conflict_guard(
    case: HistoryImpactEvalCase,
    result: DiagnosisRunResult,
) -> bool | None:
    """检查冲突历史是否在差异和避坑提示中明确阻止直接复用旧方案。

    非冲突案例返回 ``None``，不进入 conflict pass rate 分母。冲突案例先从 raw memory 的根因标注
    找到目标 case ID，再要求最终 SimilarCaseReference 同时出现根因差异和禁止直接复用提示。
    """

    if not case.expect_history_conflict:
        return None

    forbidden_roots = set(case.forbidden_root_causes)
    conflicting_ids = [
        match.memory.memory_id
        for match in result.recalled_memories
        if match.memory.root_cause in forbidden_roots
    ]
    if not conflicting_ids:
        return False

    report = result.report.state.draft_report
    if report is None:
        return False
    references = {reference.case_id: reference for reference in report.similar_cases}
    for case_id in conflicting_ids:
        reference = references.get(case_id)
        if reference is None:
            return False
        has_root_difference = any(
            "根因" in item and ("不一致" in item or "冲突" in item)
            for item in reference.differences
        )
        blocks_direct_reuse = any("禁止直接复用" in item for item in reference.pitfall_warnings)
        if not has_root_difference or not blocks_direct_reuse:
            return False
    return True


def _average(values: list[float]) -> float:
    """计算至少一个已校验案例指标的算术平均值。

    suite Schema 保证 cases 非空，但 helper 仍显式拒绝空列表，避免未来独立调用时用除零或默认零
    掩盖缺失结果。返回普通 float，最终范围由汇总 Pydantic 模型再次验证。
    """

    if not values:
        raise ValueError("history impact metric average requires at least one value")
    return sum(values) / len(values)


def _stable_unique_tools(items: list[ToolName]) -> list[ToolName]:
    """按实际首次执行顺序去重工具名，使重试不会重复增加 Action 覆盖。

    返回新列表而不修改 ToolEvent；同一工具不同参数仍只计一次名称级 Golden Action 命中，因为
    当前 fixture 的必要动作标注粒度就是产品设计定义的九个工具名。
    """

    return list(dict.fromkeys(items))
