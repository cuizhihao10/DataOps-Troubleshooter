"""验证 confirmed 历史案例的确定性共同点、差异点、参考方案和避坑提示生成。

测试不调用模型、数据库或 embedding；raw similarity 视为已由仓储给出的候选排序事实。本文件重点
证明实时 Observation 优先、结构化根因冲突可见、案例引用必带 case_id，且 matcher 不改变候选顺序。
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.models import (
    AgentState,
    CaseMemory,
    Component,
    DiagnosisReport,
    Evidence,
    EvidenceSourceType,
    FaultHypothesis,
    HypothesisStatus,
    MemoryStatus,
    SimilarCaseReference,
)
from app.memory.matcher import explain_case_matches
from app.memory.models import CaseMemoryMatch, MemoryRetrievalChannel
from app.reporting import DeterministicReportBuilder, ReportPolicyValidator

NOW = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)


def _raw_match() -> CaseMemoryMatch:
    """构造一个带历史处置和 confirmed 状态的合成 pgvector 命中。

    similarity 由测试固定为 0.91，不包含 embedding；memory 的证据引用是历史内部来源，不会直接
    冒充本次实时 Evidence，matcher 输出必须额外引用 memory_id。
    """

    return CaseMemoryMatch(
        memory=CaseMemory(
            memory_id="mem_history_match_001",
            symptoms=["LTS 任务等待上游数据"],
            root_cause="上游数据未按时就绪",
            fault_path=["LTS 依赖上游数据集"],
            solution_steps=["先补齐上游数据，再人工复核是否恢复调度。"],
            components=[Component.LTS],
            tags=["lts", "upstream_wait"],
            evidence_refs=["ev_historical_001"],
            status=MemoryStatus.CONFIRMED,
            occurrence_count=2,
            created_at=NOW,
            updated_at=NOW,
        ),
        similarity=0.91,
        retrieval_channels=[MemoryRetrievalChannel.VECTOR],
        direct_similarity=0.91,
    )


def _state(*, root_cause: str, components: list[Component]) -> AgentState:
    """构造带实时工具 Evidence 和可比较 supported 根因的当前状态。

    components 同时进入假设，调用方另通过 matcher 参数提供路由组件；两者可以故意与历史范围
    不同，以验证差异和避坑提示。文本全部为合成内容。
    """

    evidence = Evidence(
        evidence_id="ev_current_tool_001",
        source_type=EvidenceSourceType.TOOL,
        source_id="lts.get_task_status",
        content="实时 Observation 显示 LTS 任务等待上游数据。",
        observed_at=NOW,
        reliability=0.96,
    )
    return AgentState(
        run_id="run_history_match_001",
        session_id="session_history_match_001",
        user_query="检查 LTS 任务等待上游数据",
        hypotheses=[
            FaultHypothesis(
                hypothesis_id="hyp_history_match_001",
                symptom="LTS 任务等待上游数据",
                candidate_root_cause=root_cause,
                components=components,
                supporting_evidence=[evidence.evidence_id],
                status=HypothesisStatus.SUPPORTED,
                confidence=0.85,
            )
        ],
        evidence=[evidence],
    )


def test_matcher_preserves_similarity_and_explains_supported_overlap() -> None:
    """验证相同组件、症状和根因生成完整解释并保留原始 similarity。

    输出必须包含组件/症状/根因共同点、非空差异边界、历史动作和实时优先警告；引用同时包含
    case_id 与当前工具 Evidence，供报告和 Auditor 追溯两侧事实。
    """

    result = explain_case_matches(
        (_raw_match(),),
        _state(root_cause="上游数据未按时就绪", components=[Component.LTS]),
        current_components=(Component.LTS,),
    )[0]

    assert result.case_id == "mem_history_match_001"
    assert result.similarity == 0.91
    assert any("共同涉及组件" in item for item in result.common_points)
    assert any("历史根因一致" in item for item in result.common_points)
    assert result.differences
    assert result.reference_actions == ["先补齐上游数据，再人工复核是否恢复调度。"]
    assert result.pitfall_warnings
    assert result.evidence_refs == ["mem_history_match_001", "ev_current_tool_001"]


def test_matcher_surfaces_root_and_component_conflicts_without_overwriting_live_facts() -> None:
    """验证本次根因/组件与历史不同时，差异和禁止复用警告明确可见。

    matcher 不修改当前假设或历史案例；它只在 differences 标出冲突，并要求服从实时 Observation。
    这证明历史方案不会因为 0.91 similarity 自动成为本次结论。
    """

    result = explain_case_matches(
        (_raw_match(),),
        _state(root_cause="BDS 资源不足", components=[Component.LTS, Component.BDS]),
        current_components=(Component.LTS, Component.BDS),
    )[0]

    assert any("本次额外涉及组件：bds" in item for item in result.differences)
    assert any("当前候选根因与历史根因不一致" in item for item in result.differences)
    assert any("禁止直接复用" in item for item in result.pitfall_warnings)
    assert any("组件范围不同" in item for item in result.pitfall_warnings)


def test_similar_case_schema_rejects_unconfirmed_or_untraceable_explanation() -> None:
    """验证报告级相似案例必须 confirmed 且 evidence_refs 包含自身 case_id。

    两个失败输入分别阻止未确认案例和只引用本次 Evidence 的伪历史来源；错误在进入 Prompt、报告
    或 API 前由 Pydantic 暴露，不依赖自然语言规则。
    """

    base = {
        "case_id": "mem_history_match_001",
        "similarity": 0.91,
        "common_points": ["共同涉及 LTS。"],
        "differences": ["仍需实时复核。"],
        "reference_actions": ["仅供参考。"],
        "pitfall_warnings": ["不得覆盖实时事实。"],
        "evidence_refs": ["mem_history_match_001"],
    }
    with pytest.raises(ValidationError, match="must be confirmed"):
        SimilarCaseReference.model_validate({**base, "confirmed": False})
    with pytest.raises(ValidationError, match="must include case_id"):
        SimilarCaseReference.model_validate(
            {**base, "confirmed": True, "evidence_refs": ["ev_current_tool_001"]}
        )


def test_report_policy_rejects_similarity_or_explanation_drift() -> None:
    """验证报告中的历史匹配必须与确定性 matcher 输出逐字段一致。

    先用生产 Builder 生成合法报告并确认规则通过，再只提高 similarity；case_id 与引用仍合法，但
    Validator 必须返回 evidence_conflict，证明模型或后续节点不能美化分数或删改差异。
    """

    raw = _raw_match()
    state = _state(root_cause="上游数据未按时就绪", components=[Component.LTS])
    match = explain_case_matches(
        (raw,),
        state,
        current_components=(Component.LTS,),
    )[0]
    builder = DeterministicReportBuilder()
    report = builder.build(
        state,
        confirmed_case_memories=(raw.memory,),
        history_case_matches=(match,),
    )
    validator = ReportPolicyValidator()
    assert (
        validator.validate(
            report,
            state,
            confirmed_case_memories=(raw.memory,),
            history_case_matches=(match,),
        )
        == ()
    )

    drifted = DiagnosisReport.model_validate(
        {
            **report.model_dump(),
            "similar_cases": [match.model_copy(update={"similarity": 0.99})],
        }
    )
    issues = validator.validate(
        drifted,
        state,
        confirmed_case_memories=(raw.memory,),
        history_case_matches=(match,),
    )

    assert [issue.code.value for issue in issues] == ["evidence_conflict"]
    assert issues[0].claim_path == "similar_cases[0]"
