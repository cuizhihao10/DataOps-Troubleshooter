"""根据结构化审计问题执行一次保守报告修订或生成最终安全降级报告。

修订器不调用 Planner、模型或工具，也不尝试创造缺失事实。遇到无效引用、冲突或语义不支持时
直接删除受影响结论，并把原因写入 uncertainties；这比重写成另一个未经证实的结论更安全。
"""

from __future__ import annotations

from app.domain.models import (
    AgentState,
    AuditIssue,
    AuditIssueCode,
    CaseMemory,
    DiagnosisReport,
    RemediationStep,
    RiskLevel,
)
from app.reporting.evidence import collect_valid_reference_ids
from app.retrieval.models import GraphEvidenceBundle


class SafeReportReviser:
    """以删除、过滤和显式降级实现最多一次确定性返工。

    Reviser 只收窄报告，不提升置信度或增加根因；即使 Auditor 指令含有新事实，也只读取有限
    AuditIssueCode 决定保守动作。该边界保证返工不会成为绕过证据门禁的第二个生成通道。
    """

    def revise(
        self,
        report: DiagnosisReport,
        issues: tuple[AuditIssue, ...],
        state: AgentState,
        *,
        evidence_bundle: GraphEvidenceBundle | None = None,
        confirmed_case_memories: tuple[CaseMemory, ...] = (),
    ) -> DiagnosisReport:
        """过滤无效内容并追加审计不确定性，返回仍需再次审计的新报告。

        INVALID_EVIDENCE_REF 会删除悬空引用和失去全部引用的结论；UNSUPPORTED/EVIDENCE_CONFLICT
        会移除全部根因与链路，避免依赖模型提供的不可信 claim_path 做局部保留。风险或案例问题
        则删除对应类别。输入报告不被就地修改，失败只可能来自强类型模型不变量。
        """

        valid_refs = collect_valid_reference_ids(
            state,
            evidence_bundle,
            confirmed_case_memories,
        )
        issue_codes = {issue.code for issue in issues}
        # 语义不支持或冲突不能靠过滤单个 ID 修复，最安全策略是删除整类根因和链路声明。
        remove_claims = bool(
            issue_codes & {AuditIssueCode.UNSUPPORTED_CLAIM, AuditIssueCode.EVIDENCE_CONFLICT}
        )

        # 先清理引用，再决定是否保留结论；任何引用为空的根因/链路都不能继续存在。
        root_causes = (
            []
            if remove_claims
            else [
                item.model_copy(
                    update={"evidence_refs": _valid_refs(item.evidence_refs, valid_refs)}
                )
                for item in report.root_causes
                if _valid_refs(item.evidence_refs, valid_refs)
            ]
        )
        fault_chain = (
            []
            if remove_claims
            else [
                item.model_copy(
                    update={"evidence_refs": _valid_refs(item.evidence_refs, valid_refs)}
                )
                for item in report.fault_chain
                if _valid_refs(item.evidence_refs, valid_refs)
            ]
        )
        if AuditIssueCode.MISSING_RISK_CONTROL in issue_codes:
            remediation_steps = [_readonly_follow_up_step()]
        else:
            remediation_steps = [
                item.model_copy(
                    update={
                        "order": order,
                        "evidence_refs": _valid_refs(item.evidence_refs, valid_refs),
                    }
                )
                for order, item in enumerate(report.remediation_steps, start=1)
                if item.risk_level is not RiskLevel.HIGH
                or _valid_refs(item.evidence_refs, valid_refs)
            ]
            if not remediation_steps:
                remediation_steps = [_readonly_follow_up_step()]
        # 未确认案例问题会清空全部案例引用，避免依赖模型提供的 claim_path 做不安全局部保留。
        similar_cases = (
            []
            if AuditIssueCode.UNCONFIRMED_CASE in issue_codes
            else [
                item.model_copy(
                    update={"evidence_refs": _valid_refs(item.evidence_refs, valid_refs)}
                )
                for item in report.similar_cases
            ]
        )
        evidence_refs = _aggregate_refs(root_causes, fault_chain, remediation_steps, similar_cases)
        uncertainties = list(report.uncertainties)
        uncertainties.extend(
            f"首次审计要求修订：{code.value}。" for code in sorted(issue_codes, key=str)
        )
        if remove_claims:
            uncertainties.append("未通过证据支撑审查的根因与链路已从修订稿删除。")
        return DiagnosisReport(
            summary="报告已按首次审计收窄；仅保留当前引用集合可支持的内容。",
            fault_chain=fault_chain,
            root_causes=root_causes,
            evidence_refs=evidence_refs,
            remediation_steps=remediation_steps,
            risks=["修订稿不放行高风险生产操作，后续只允许人工只读核验。"],
            uncertainties=list(dict.fromkeys(uncertainties)),
            similar_cases=similar_cases,
        )

    def degrade(
        self,
        report: DiagnosisReport,
        issues: tuple[AuditIssue, ...],
        state: AgentState,
        *,
        evidence_bundle: GraphEvidenceBundle | None = None,
        confirmed_case_memories: tuple[CaseMemory, ...] = (),
    ) -> DiagnosisReport:
        """在二次未通过或 Auditor 不可用时生成不含根因声明的最终降级报告。

        降级稿仅保留仍可寻址的原始报告引用、一个低风险只读核验步骤和有限问题代码；它不会把
        Auditor 消息原文或模型响应写给用户，也不会生成 memory candidate。该结果明确未获审计接受。
        """

        valid_refs = collect_valid_reference_ids(
            state,
            evidence_bundle,
            confirmed_case_memories,
        )
        retained_refs = _valid_refs(report.evidence_refs, valid_refs)
        issue_codes = sorted({issue.code.value for issue in issues})
        uncertainties = ["Auditor 未能放行报告，所有根因、链路和历史案例结论均已移除。"]
        uncertainties.extend(f"未解决审计问题：{code}。" for code in issue_codes)
        return DiagnosisReport(
            summary="本次诊断返回安全降级报告：保留证据索引，但不确认根因或生产修复方案。",
            fault_chain=[],
            root_causes=[],
            evidence_refs=retained_refs,
            remediation_steps=[_readonly_follow_up_step()],
            risks=["不得依据本降级报告执行生产写操作或自动修复。"],
            uncertainties=uncertainties,
            similar_cases=[],
        )


def _readonly_follow_up_step() -> RemediationStep:
    """构造审计失败后唯一允许保留的低风险只读核验步骤。

    步骤没有证据引用，因为它不是根因修复而是补证动作；前置条件、回滚和验证仍完整填写，保证
    UI 与人工执行不会把“继续核验”误解为已批准生产变更。每次调用返回新对象避免共享可变列表。
    """

    return RemediationStep(
        order=1,
        action="停止生产变更，仅通过白名单只读工具补齐 Auditor 指出的证据缺口。",
        risk_level=RiskLevel.LOW,
        evidence_refs=[],
        prerequisites=["保留当前 run_id、已有 Evidence 和 ToolEvent，确认补证范围不扩大。"],
        rollback="只读核验不修改系统；若工具异常，立即停止并保留失败事件。",
        verification="重新生成报告并由独立 Auditor 再次审核后，才允许形成可执行建议。",
    )


def _aggregate_refs(root_causes, fault_chain, remediation_steps, similar_cases) -> list[str]:
    """合并修订稿各结构中的引用，保持首次出现顺序并去重。

    参数是 DiagnosisReport 子模型序列；函数不接受自由字典，所有对象均已有 evidence_refs 属性。
    返回结果用于报告级索引，不增加或转换 ID，空结构合法得到空列表。
    """

    refs = [
        *(ref for item in root_causes for ref in item.evidence_refs),
        *(ref for item in fault_chain for ref in item.evidence_refs),
        *(ref for item in remediation_steps for ref in item.evidence_refs),
        *(ref for item in similar_cases for ref in item.evidence_refs),
    ]
    return list(dict.fromkeys(refs))


def _valid_refs(items: list[str], valid_refs: set[str]) -> list[str]:
    """过滤悬空引用并稳定去重，供修订和降级路径共享。

    函数只验证 ID 是否存在，不重新解释证据语义；语义问题由 issue code 驱动更保守的整类删除。
    输入列表保持不变，返回新列表便于 Pydantic model_copy 安全使用。
    """

    return list(dict.fromkeys(item for item in items if item in valid_refs))
