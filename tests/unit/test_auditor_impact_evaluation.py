"""验证 Auditor off/on 增量安全评测的 Schema、指标和配对污染门禁。

单元测试只构造强类型 ``AuditorImpactRun``，不运行 LangGraph 或模型；它锁定三类缺陷覆盖、预期
问题发现率、危险内容残留、安全处置、accepted/degraded 计数，以及草稿或规则预检漂移失败语义。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.models import (
    AuditIssue,
    AuditIssueCode,
    DiagnosisReport,
    RemediationStep,
    RiskLevel,
    RootCauseConclusion,
)
from app.orchestration import (
    AuditorImpactEvalCase,
    AuditorImpactEvalSuite,
    AuditorImpactMode,
    AuditorImpactRun,
    evaluate_auditor_impact,
    load_auditor_impact_eval_suite,
)
from app.orchestration.auditor_evaluation import AuditorImpactOutcome

SUITE_PATH = Path("data/evals/auditor_impact_cases.json")


class ScriptedAuditorImpactRunner:
    """按 fixture 返回规则对照和完整审计合成观察，并支持定向污染配对条件。

    正常 off 原样保留危险草稿且不产生 issue；on 命中全部预期 code 并移除 marker。故障开关用于
    验证相同草稿门禁和“发现问题但未安全清除”指标，而不是制造无效 Pydantic 对象。
    """

    def __init__(
        self,
        *,
        drift_on_draft: bool = False,
        retain_on_unsafe_content: bool = False,
        inject_deterministic_issue: bool = False,
    ) -> None:
        """保存三个独立故障注入选项并初始化空调用记录。

        构造不创建报告或运行评测；每次 run 依据 case/mode 新建对象。注入的对象本身仍满足模式
        Schema，使错误由 paired evaluator 或安全指标发现，而不是提前被无关字段校验截断。
        """

        self._drift_on_draft = drift_on_draft
        self._retain_on_unsafe_content = retain_on_unsafe_content
        self._inject_deterministic_issue = inject_deterministic_issue
        self.calls: list[tuple[str, AuditorImpactMode]] = []

    async def run(
        self,
        case: AuditorImpactEvalCase,
        *,
        mode: AuditorImpactMode,
    ) -> AuditorImpactRun:
        """记录调用并返回符合指定模式的合成审计影响观察。

        off 始终使用原始危险草稿；on 默认使用安全报告并记录预期 issue。若启用草稿漂移，仅修改
        on 的 initial draft 摘要；确定性问题注入则让两组保持相同问题以触发增量归因门禁。
        """

        self.calls.append((case.case_id, mode))
        draft = _unsafe_draft(case)
        deterministic_issues = (
            (
                AuditIssue(
                    code=AuditIssueCode.REPORT_INCOMPLETE,
                    claim_path="summary",
                    message="合成确定性预检问题。",
                ),
            )
            if self._inject_deterministic_issue
            else ()
        )
        if mode is AuditorImpactMode.AUDITOR_OFF:
            return AuditorImpactRun(
                case_id=case.case_id,
                mode=mode,
                draft_report=draft,
                deterministic_issues=deterministic_issues,
                final_report=draft,
                outcome=AuditorImpactOutcome.CONTROL_UNREVIEWED,
                auditor_called=False,
            )

        on_draft = (
            draft.model_copy(update={"summary": "故意漂移的实验组草稿。"})
            if self._drift_on_draft
            else draft
        )
        final_report = draft if self._retain_on_unsafe_content else _safe_report(case)
        outcome = (
            AuditorImpactOutcome.ACCEPTED
            if case.expected_on_outcome.value == "accepted"
            else AuditorImpactOutcome.DEGRADED
        )
        return AuditorImpactRun(
            case_id=case.case_id,
            mode=mode,
            draft_report=on_draft,
            deterministic_issues=deterministic_issues,
            final_report=final_report,
            outcome=outcome,
            audit_issue_codes=list(case.expected_issue_codes),
            revision_count=1,
            auditor_called=True,
        )


def test_auditor_impact_suite_loads_all_defect_types_and_rejects_missing_markers() -> None:
    """确认 v1 suite 覆盖三种语义缺陷，并拒绝没有危险根因或动作 marker 的案例。

    测试复制 JSON 后清空第一例 unsafe root；Schema 必须在 runner 前失败，防止危险残留率用零分母
    得到看似安全的结果。
    """

    suite = load_auditor_impact_eval_suite(SUITE_PATH)

    assert suite.contract_id == "auditor-impact-eval:v1"
    assert len(suite.cases) == 3
    assert len({case.defect_type for case in suite.cases}) == 3

    payload = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    payload["cases"][0]["unsafe_root_causes"] = []
    with pytest.raises(ValidationError, match="unsafe marker"):
        AuditorImpactEvalSuite.model_validate(payload)


@pytest.mark.asyncio
async def test_auditor_impact_report_measures_incremental_detection_and_safe_resolution() -> None:
    """验证三案例 macro 发现、危险残留、处置和 accepted/degraded 数量。

    规则对照没有 Auditor issue 且原样保留三个危险 marker；完整审计命中全部预期 code 并清除内容，
    因此发现率 0→1、危险率 1→0、安全处置率 0→1，三例均为增量发现且均执行一次返工。
    """

    suite = load_auditor_impact_eval_suite(SUITE_PATH)
    runner = ScriptedAuditorImpactRunner()

    report = await evaluate_auditor_impact(suite, runner)

    assert report.metric_kind == "measured"
    assert report.auditor_off_macro_issue_detection_rate == 0
    assert report.auditor_on_macro_issue_detection_rate == 1
    assert report.issue_detection_delta == 1
    assert report.auditor_off_macro_unsafe_item_rate == 1
    assert report.auditor_on_macro_unsafe_item_rate == 0
    assert report.unsafe_item_rate_delta == -1
    assert report.auditor_off_safe_resolution_rate == 0
    assert report.auditor_on_safe_resolution_rate == 1
    assert report.safe_resolution_delta == 1
    assert report.deterministic_clean_case_count == 3
    assert report.incremental_detection_case_count == 3
    assert report.auditor_on_revision_case_count == 3
    assert report.auditor_on_accepted_case_count == 2
    assert report.auditor_on_degraded_case_count == 1
    assert len(runner.calls) == 6


@pytest.mark.asyncio
async def test_auditor_impact_eval_rejects_paired_draft_drift() -> None:
    """确认实验组若使用不同初始草稿，评测立即失败而不是归因给 Auditor。

    on 草稿只改变摘要且自身合法，但这已破坏唯一变量原则；异常应在第一条 paired 校验中抛出，
    不继续计算后续案例平均值。
    """

    suite = load_auditor_impact_eval_suite(SUITE_PATH)
    runner = ScriptedAuditorImpactRunner(drift_on_draft=True)

    with pytest.raises(ValueError, match="same initial draft"):
        await evaluate_auditor_impact(suite, runner)


@pytest.mark.asyncio
async def test_auditor_impact_eval_rejects_cases_already_caught_by_rules() -> None:
    """确认确定性预检已发现问题的案例不能用于声明 Auditor 增量贡献。

    两组使用相同规则问题，因此 paired 输入本身一致；评测仍必须拒绝，因为此类问题应归入
    ReportPolicyValidator 测试，而不是重复计入独立 Agent 发现率。
    """

    suite = load_auditor_impact_eval_suite(SUITE_PATH)
    runner = ScriptedAuditorImpactRunner(inject_deterministic_issue=True)

    with pytest.raises(ValueError, match="must pass deterministic precheck"):
        await evaluate_auditor_impact(suite, runner)


@pytest.mark.asyncio
async def test_auditor_impact_eval_requires_resolution_not_only_issue_reporting() -> None:
    """确认 on 组即使命中预期 issue，危险内容未删除仍不能算安全处置。

    该测试区分“检测”与“处理”：发现率仍为 1，但危险残留率保持 1、安全处置率为 0，防止只统计
    revise 事件而忽略最终报告是否真正收窄。
    """

    suite = load_auditor_impact_eval_suite(SUITE_PATH)
    runner = ScriptedAuditorImpactRunner(retain_on_unsafe_content=True)

    report = await evaluate_auditor_impact(suite, runner)

    assert report.auditor_on_macro_issue_detection_rate == 1
    assert report.auditor_on_macro_unsafe_item_rate == 1
    assert report.auditor_on_safe_resolution_rate == 0


def _unsafe_draft(case: AuditorImpactEvalCase) -> DiagnosisReport:
    """根据 fixture marker 构造通过 Pydantic、但保留目标语义缺陷的报告草稿。

    根因案例引用一个稳定合成 Evidence ID；风险案例使用字段完整的 medium 步骤，说明 Schema 合法
    不等于语义安全。函数不调用 Validator 或模型，输出只服务 evaluator 单元测试。
    """

    evidence_ref = f"ev_{case.case_id}"
    roots = [
        RootCauseConclusion(
            root_cause=root,
            confidence=0.9,
            evidence_refs=[evidence_ref],
        )
        for root in case.unsafe_root_causes
    ]
    steps = [
        RemediationStep(
            order=index,
            action=f"{fragment}并立即继续同步。",
            risk_level=RiskLevel.MEDIUM,
            evidence_refs=[evidence_ref],
            prerequisites=["确认存在合成快照。"],
            rollback="恢复合成快照。",
            verification="重新运行只读一致性检查。",
        )
        for index, fragment in enumerate(case.unsafe_action_fragments, start=1)
    ]
    return DiagnosisReport(
        summary="包含目标语义缺陷的合成报告草稿。",
        root_causes=roots,
        evidence_refs=[evidence_ref] if roots or steps else [],
        remediation_steps=steps,
        risks=["字段已填写，但仍需要独立语义审计。"],
        uncertainties=[] if roots else ["当前仅用于风险建议审计。"],
    )


def _safe_report(case: AuditorImpactEvalCase) -> DiagnosisReport:
    """构造已移除 fixture 危险 marker 的修订或降级报告。

    accepted 与 degraded 在 evaluator 中由 outcome 区分；两者的安全检查都只要求危险根因/动作不再
    出现。报告保留一个低风险只读步骤和明确不确定性，不编造替代根因。
    """

    return DiagnosisReport(
        summary="独立审计后已移除未放行内容。",
        evidence_refs=[],
        remediation_steps=[
            RemediationStep(
                order=1,
                action="停止写操作，仅继续白名单只读核验。",
                risk_level=RiskLevel.LOW,
                prerequisites=["保留当前 Evidence 和 run_id。"],
                rollback="只读检查不修改系统。",
                verification="补证后重新生成并审计报告。",
            )
        ],
        risks=["不得依据未审计草稿执行生产操作。"],
        uncertainties=[f"已处理合成审计案例：{case.case_id}。"],
    )
