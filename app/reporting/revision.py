"""根据结构化审计问题执行一次保守报告修订或生成最终安全降级报告。

修订器不调用 Planner、模型或工具，也不尝试创造缺失事实。遇到无效引用、冲突或语义不支持时
直接删除受影响结论，并把"证据不足"表述为面向用户的不确定性；这比重写成另一个未经证实的结论
更安全。删除按 `claim_path` 定位到具体条目：修订器只会删内容，删除方向永远偏安全，而修订稿必须
再过一遍确定性规则与独立 Auditor 才可能被接受，因此"精确删除"不会成为放行不良结论的通道；反过来
把整类根因一起删掉才会制造新问题——空报告与现有 Observation 自相矛盾，第二轮审计以
evidence_conflict 再否决一次，返工预算必然耗尽。修订稿不写审计问题码——那属于公开事件，
写进报告正文只会让第二轮审计再否决一次。
"""

from __future__ import annotations

import re

from app.domain.models import (
    AgentState,
    AuditIssue,
    AuditIssueCode,
    CaseMemory,
    DiagnosisReport,
    FaultChainStep,
    RemediationStep,
    RiskLevel,
    RootCauseConclusion,
    SimilarCaseReference,
)
from app.reporting.draft import derive_report_risks
from app.reporting.evidence import collect_valid_reference_ids
from app.retrieval.models import GraphEvidenceBundle

# 只有这些代码代表"某条声明不能留"；invalid_evidence_ref 由引用过滤处理，report_incomplete 表示
# 缺内容，再删只会让报告更空，auditor_unavailable 根本不进入修订路径。
_CLAIM_REMOVING_CODES = frozenset(
    {
        AuditIssueCode.UNSUPPORTED_CLAIM,
        AuditIssueCode.EVIDENCE_CONFLICT,
        AuditIssueCode.UNCONFIRMED_CASE,
        AuditIssueCode.MISSING_RISK_CONTROL,
    }
)
# summary、risks 与报告级 evidence_refs 全部由保留内容重新推导，指向它们的问题靠重算解决；
# 若把它们当成删除信号，一句被判为无支撑的摘要就会连带删光本来有引用支撑的根因。
_DERIVED_FIELDS = frozenset({"summary", "risks", "evidence_refs"})
_LIST_FIELDS = frozenset(
    {"root_causes", "fault_chain", "remediation_steps", "similar_cases", "uncertainties"}
)
# 受限路径语法：可选 `$.` 前缀、字段名、可选 `[i]` 或 `[i:j]`。模型实际会写出 `$.root_causes[0]`、
# `summary`、`$.uncertainties[2:5]` 三种形态，无法解析时退回整类删除而不是猜测。
_CLAIM_PATH_PATTERN = re.compile(
    r"^\$?\.?(?P<field>[a-z_]+)(?:\[(?P<start>\d+)(?::(?P<stop>\d+))?\])?$"
)


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
        history_case_matches: tuple[SimilarCaseReference, ...] = (),
    ) -> DiagnosisReport:
        """按 `claim_path` 删除被否决条目，再按保留内容重写摘要与风险，返回仍需再次审计的新报告。

        INVALID_EVIDENCE_REF 删除悬空引用和失去全部引用的结论；UNSUPPORTED/EVIDENCE_CONFLICT/
        UNCONFIRMED_CASE/MISSING_RISK_CONTROL 只删除 `claim_path` 指到的那一条，路径无法解析或指向
        未知字段时才退回整类删除。指向 summary/risks/evidence_refs 的问题不触发删除：这三项由保留
        内容重算，删根因反而会制造空报告与 Observation 的自相矛盾。摘要、风险与不确定性全部由保留
        内容推导，不复述审计过程，否则修订稿会携带只有审计结果才能支持的陈述，被第二轮审计当成新的
        无支撑声明。输入报告不被就地修改。
        """

        valid_refs = collect_valid_reference_ids(
            state,
            evidence_bundle,
            confirmed_case_memories,
        )
        targets, wholesale = _removal_targets(issues)
        if wholesale:
            # 路径不可解析意味着"哪一条不成立"这一信息本身缺失，只能退回旧的整类删除；这是兜底而
            # 不是常态路径，确定性规则给出的路径始终带索引，模型路径也覆盖了受限语法的三种形态。
            targets["root_causes"] = None
            targets["fault_chain"] = None
            targets.setdefault("similar_cases", None)

        # 先按定位删除，再清理引用；任何引用为空的根因/链路都不能继续存在。
        root_causes = [
            item.model_copy(update={"evidence_refs": _valid_refs(item.evidence_refs, valid_refs)})
            for item in _survivors(report.root_causes, targets, "root_causes")
            if _valid_refs(item.evidence_refs, valid_refs)
        ]
        fault_chain = [
            item.model_copy(update={"evidence_refs": _valid_refs(item.evidence_refs, valid_refs)})
            for item in _survivors(report.fault_chain, targets, "fault_chain")
            if _valid_refs(item.evidence_refs, valid_refs)
        ]
        retained_steps = [
            item
            for item in _survivors(report.remediation_steps, targets, "remediation_steps")
            if item.risk_level is not RiskLevel.HIGH or _valid_refs(item.evidence_refs, valid_refs)
        ]
        # order 在删除之后重新编号：沿用原下标会留下 1、3、4 这样的空洞，人工执行时无法判断
        # 中间那一步是被删了还是漏渲染了。
        remediation_steps = [
            item.model_copy(
                update={
                    "order": order,
                    "evidence_refs": _valid_refs(item.evidence_refs, valid_refs),
                }
            )
            for order, item in enumerate(retained_steps, start=1)
        ]
        if not remediation_steps:
            remediation_steps = [_readonly_follow_up_step()]
        similar_cases = [
            item.model_copy(update={"evidence_refs": _valid_refs(item.evidence_refs, valid_refs)})
            for item in _survivors(report.similar_cases, targets, "similar_cases")
        ]
        evidence_refs = _aggregate_refs(root_causes, fault_chain, remediation_steps, similar_cases)
        # 审计问题码刻意不再写进 uncertainties：它们已经通过 audit_completed / revision_applied 公开
        # 事件对外暴露，而"首次审计要求修订：unsupported_claim"这类句子本身不是本轮证据支持的事实。
        # 第二轮审计会以 unsupported_claim 指向 uncertainties 把修订稿自己写的这几句话再否决一次，
        # 于是返工预算必然耗尽——首次真实模型评测三个案例全部 safe_degraded 就有这条自我否决回路。
        uncertainties = _survivors(report.uncertainties, targets, "uncertainties")
        if not root_causes and not fault_chain:
            uncertainties.append("当前引用集合不足以支撑可审计的根因与传播链路，相关结论已移除。")
        elif len(root_causes) < len(report.root_causes) or len(fault_chain) < len(
            report.fault_chain
        ):
            uncertainties.append("审计否决的结论已按定位删除，保留部分只覆盖仍有引用支撑的判断。")
        if not uncertainties:
            uncertainties.append("修订稿只保留现有引用可支持的内容，其余判断需要补充只读取证。")
        return DiagnosisReport(
            summary=_revision_summary(root_causes, fault_chain),
            fault_chain=fault_chain,
            root_causes=root_causes,
            evidence_refs=evidence_refs,
            remediation_steps=remediation_steps,
            # 风险文本与草稿共享同一推导规则：写死"只允许人工只读核验"会与保留下来的中风险步骤
            # 直接矛盾，而这正好构成 Auditor 的 evidence_conflict，把一次本可放行的修订推向降级。
            risks=derive_report_risks(remediation_steps),
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
        history_case_matches: tuple[SimilarCaseReference, ...] = (),
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


def _revision_summary(
    root_causes: list[RootCauseConclusion],
    fault_chain: list[FaultChainStep],
) -> str:
    """用修订稿实际保留的结论生成摘要，而不是描述"报告已被修订"这一过程事实。

    摘要是 Auditor 逐项核对的字段之一：只写流程状态会被判为 report_incomplete（实测评语是"摘要
    仅描述报告修订状态，没有回答用户要求判断的问题"），因此这里必须复述保留下来的根因或链路。
    根因优先，其次链路首段，两者都为空时明确说明只剩补证动作，让降级语义对用户可见且可复算。
    """

    if root_causes:
        head = root_causes[0].root_cause.strip()
        if len(root_causes) > 1:
            return f"修订后保留 {len(root_causes)} 项有引用支撑的根因，其中首要根因为：{head}"
        return f"修订后保留唯一有引用支撑的根因：{head}"
    if fault_chain:
        head = fault_chain[0].description.strip()
        return (
            f"修订后保留 {len(fault_chain)} 段可追溯的传播链路，但尚不能收敛到单一根因；"
            f"链路起点为：{head}"
        )
    return "当前引用集合不足以支撑根因或传播链路结论，修订稿只保留只读补证步骤。"


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


def _removal_targets(
    issues: tuple[AuditIssue, ...],
) -> tuple[dict[str, frozenset[int] | None], bool]:
    """把审计问题翻译成"哪个字段的哪几个下标必须删除"，并给出是否需要退回整类删除。

    返回值第一项按字段聚合下标集合，值为 `None` 表示该字段整类删除；第二项为 True 时调用方还要
    额外清空根因与链路。只有 `_CLAIM_REMOVING_CODES` 参与定位；指向派生字段的问题被忽略（它们靠
    重算解决），字段名未知或路径不符合受限语法则触发兜底，避免把模型写错的路径当成"无需删除"。
    """

    targets: dict[str, frozenset[int] | None] = {}
    wholesale = False
    for issue in issues:
        if issue.code not in _CLAIM_REMOVING_CODES:
            continue
        field, indices = _parse_claim_path(issue.claim_path)
        if field in _DERIVED_FIELDS:
            continue
        if field is None or field not in _LIST_FIELDS:
            wholesale = True
            continue
        if indices is None:
            targets[field] = None
            continue
        existing = targets.get(field, frozenset())
        # 已经决定整类删除的字段不必再累加下标，否则会把 None 覆盖成更宽松的集合。
        if existing is None:
            continue
        targets[field] = existing | indices
    return targets, wholesale


def _parse_claim_path(claim_path: str) -> tuple[str | None, frozenset[int] | None]:
    """解析受限 claim_path 语法，返回字段名与被指到的下标集合。

    支持 `root_causes[0]`、`$.uncertainties[2:5]`、`summary` 三种实际出现过的形态：字段名不带下标
    时下标返回 `None` 表示整类，切片按半开区间展开为具体下标。语法不符或切片方向相反都返回
    `(None, None)`，由调用方退回整类删除——猜测下标会删错条目，而删错的方向不一定偏安全。
    """

    matched = _CLAIM_PATH_PATTERN.match(claim_path.strip())
    if matched is None:
        return (None, None)
    field = matched.group("field")
    start_text = matched.group("start")
    if start_text is None:
        return (field, None)
    start = int(start_text)
    stop_text = matched.group("stop")
    if stop_text is None:
        return (field, frozenset({start}))
    stop = int(stop_text)
    if stop <= start:
        return (None, None)
    return (field, frozenset(range(start, stop)))


def _survivors(items: list, targets: dict[str, frozenset[int] | None], field: str) -> list:
    """按定位结果返回保留下来的条目列表，字段未被审计指到时原样保留。

    `targets` 里没有该字段表示本轮没有任何问题指向它；值为 `None` 表示整类删除；值为下标集合时
    逐项过滤。始终返回新列表，调用方可以安全追加或继续过滤，输入报告的列表不会被就地修改。
    """

    if field not in targets:
        return list(items)
    removed = targets[field]
    if removed is None:
        return []
    return [item for index, item in enumerate(items) if index not in removed]


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
