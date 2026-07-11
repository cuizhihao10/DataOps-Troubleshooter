"""把 confirmed 案例召回结果确定性解释为共同点、差异点、参考方案和避坑提示。

向量直接分与 SIMILAR_TO 图传播分都只说明检索相关，不能证明两次故障等价。本模块比较当前组件、
问题、假设、实时 Evidence 和历史结构，生成可审计 ``SimilarCaseReference``；它不调用第三个
Agent，也不改变候选集合或分数，保持“检索决定候选、规则解释边界、实时事实优先”的职责分离。
"""

from __future__ import annotations

import re

from app.domain.models import (
    AgentState,
    Component,
    EvidenceSourceType,
    HypothesisStatus,
    SimilarCaseReference,
)
from app.memory.models import CaseMemoryMatch, MemoryRetrievalChannel

_WHITESPACE = re.compile(r"\s+")


def explain_case_matches(
    matches: tuple[CaseMemoryMatch, ...],
    state: AgentState,
    *,
    current_components: tuple[Component, ...],
) -> tuple[SimilarCaseReference, ...]:
    """按原召回顺序把 confirmed raw matches 转换为完整可解释历史匹配。

    输入候选已由向量/图仓储排序和 confirmed 门禁验证；函数不重新排序、过滤或修改 similarity。
    每个输出至少包含一个共同点、一个差异/未确认边界、参考动作、避坑提示和案例引用。状态中的
    TOOL Evidence 优先进入引用，帮助 Auditor 检查历史结论是否与本次实时 Observation 冲突。
    """

    if len(current_components) != len(set(current_components)):
        raise ValueError("history comparison components must not contain duplicates")
    if not current_components and matches:
        raise ValueError("history comparison requires current components when matches exist")

    return tuple(
        _explain_one_match(match, state, current_components=current_components) for match in matches
    )


def _explain_one_match(
    match: CaseMemoryMatch,
    state: AgentState,
    *,
    current_components: tuple[Component, ...],
) -> SimilarCaseReference:
    """比较一个历史案例与当前状态，并保留“相似不等于相同”的显式边界。

    结构化组件和根因使用精确规范化比较；症状只做保守的双向包含，不引入未批准分词/LLM 依赖。
    无法确认差异时也返回“仍需实时复核”，而不是把空 differences 误解为完全一致。
    """

    memory = match.memory
    current_component_values = {item.value for item in current_components}
    memory_component_values = {item.value for item in memory.components}
    overlap = sorted(current_component_values & memory_component_values)
    current_only = sorted(current_component_values - memory_component_values)
    memory_only = sorted(memory_component_values - current_component_values)

    common_points = [f"历史候选最终排序分为 {match.similarity:.3f}，该分数不等于事实置信度。"]
    if MemoryRetrievalChannel.VECTOR in match.retrieval_channels:
        common_points.append(f"案例直接 pgvector cosine 相似度为 {match.direct_similarity:.3f}。")
    if MemoryRetrievalChannel.GRAPH in match.retrieval_channels:
        common_points.append(
            "案例由已确认先例沿 SIMILAR_TO 关系扩展，"
            f"查询相关度与边权相乘后的图传播分为 {match.graph_score:.3f}；"
            f"关系引用：{', '.join(match.graph_edge_refs)}。"
        )
    if overlap:
        common_points.append(f"本次与历史案例共同涉及组件：{', '.join(overlap)}。")

    # 只比较已进入强类型状态的公开文本；不会读取 Prompt、模型原始输出或长期记忆 embedding。
    current_text = _normalized_context_text(state)
    matched_symptoms = [
        symptom for symptom in memory.symptoms if _texts_overlap(symptom, current_text)
    ]
    if matched_symptoms:
        common_points.append("当前上下文复现历史症状：" + "；".join(matched_symptoms) + "。")

    current_roots = {
        _normalize_text(hypothesis.candidate_root_cause)
        for hypothesis in state.hypotheses
        if hypothesis.status is not HypothesisStatus.REJECTED
    }
    memory_root = _normalize_text(memory.root_cause)
    if memory_root in current_roots:
        common_points.append("当前候选假设与历史根因一致，但仍需本次实时证据独立支持。")

    differences: list[str] = []
    if current_only:
        differences.append(f"本次额外涉及组件：{', '.join(current_only)}。")
    if memory_only:
        differences.append(f"历史案例额外涉及组件：{', '.join(memory_only)}。")
    if current_roots and memory_root not in current_roots:
        differences.append(
            "当前候选根因与历史根因不一致；必须服从本次实时 Observation，不能复制历史结论。"
        )
    elif not current_roots:
        differences.append("本次尚未形成可比较的根因假设，历史根因只能作为待验证先例。")
    if not matched_symptoms:
        differences.append("当前结构化上下文尚未明确复现历史症状，需要继续核对实时 Observation。")
    if not differences:
        differences.append("当前结构化字段未发现明确差异，仍不能把语义相似度当作事实等价。")

    reference_actions = list(memory.solution_steps) or [
        "历史案例未记录可复用处置步骤，本次不得据此生成生产写操作。"
    ]
    pitfall_warnings = [
        "历史方案只用于人工参考；执行前必须以本次实时 Observation 重新验证前置条件。"
    ]
    if current_roots and memory_root not in current_roots:
        pitfall_warnings.append("根因存在结构化冲突，禁止直接复用历史修复方案。")
    if current_only or memory_only:
        pitfall_warnings.append("组件范围不同，需逐项检查方案是否会影响本次未覆盖链路。")

    # 案例 ID 是历史来源；本次 TOOL Evidence 放在其后，供报告同时展示旧先例与实时事实。
    realtime_refs = [
        evidence.evidence_id
        for evidence in state.evidence
        if evidence.source_type is EvidenceSourceType.TOOL
    ]
    evidence_refs = _stable_unique([memory.memory_id, *realtime_refs[:7]])
    return SimilarCaseReference(
        case_id=memory.memory_id,
        similarity=match.similarity,
        confirmed=True,
        common_points=_stable_unique(common_points),
        differences=_stable_unique(differences),
        reference_actions=_stable_unique(reference_actions),
        pitfall_warnings=_stable_unique(pitfall_warnings),
        evidence_refs=evidence_refs,
    )


def _normalized_context_text(state: AgentState) -> str:
    """组合当前问题、假设和实时证据为只用于保守包含比较的规范文本。

    CASE_MEMORY Evidence 被排除，避免历史内容递归证明自身；GraphRAG/TOOL 等当前上下文可以参与。
    结果不做语义扩写，空白折叠和 casefold 只消除大小写/排版差异。
    """

    segments = [state.user_query]
    for hypothesis in state.hypotheses:
        segments.extend([hypothesis.symptom, hypothesis.candidate_root_cause])
    segments.extend(
        evidence.content
        for evidence in state.evidence
        if evidence.source_type is not EvidenceSourceType.CASE_MEMORY
    )
    return _normalize_text("\n".join(segments))


def _texts_overlap(left: str, normalized_right: str) -> bool:
    """用规范化双向包含判断两个非空短文本是否存在保守重叠。

    不使用字符集合比例，避免中文常见字造成伪相似；短于两个字符的片段不参与，降低“失败”等
    单字/短词误命中。该规则宁可漏报共同点，也不编造语义等价。
    """

    normalized_left = _normalize_text(left)
    if len(normalized_left) < 2 or len(normalized_right) < 2:
        return False
    return normalized_left in normalized_right or normalized_right in normalized_left


def _normalize_text(value: str) -> str:
    """执行 casefold、首尾清理和连续空白折叠，稳定确定性文本比较。

    函数保留标点和词序，不做同义词映射或自动翻译；更宽松相似度已由 pgvector 提供，此处只负责
    解释可以客观确认的字段重合。
    """

    return _WHITESPACE.sub(" ", value.casefold().strip())


def _stable_unique(items: list[str]) -> list[str]:
    """按首次出现顺序去重解释文本或引用，不改变召回优先级。

    返回新列表；空输入合法，但调用方的 SimilarCaseReference Schema 会对必填解释字段再次约束。
    """

    return list(dict.fromkeys(items))
