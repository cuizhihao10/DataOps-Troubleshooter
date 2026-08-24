"""图检索与文档检索共用的评分通道枚举、分数裁剪与二阶段融合不变量。

图节点和文档切片是两条独立的召回通道，但"最终分必须能被解释"这条要求对两者完全相同：任何偏离
一阶段混合分的排序值都必须有一个真实存在的重排分数作为解释。把这些规则放进独立的底层模块，而
不是让 `documents.py` 反向导入 `models.py` 的私有函数，既消除了循环导入，也保证两条通道不会因为
各自复制一份实现而在容差、裁剪范围或融合公式上悄悄分叉——那种分叉在评测里表现为无法归因的名次
变化，是最难发现的一类问题。

本模块只做纯函数与枚举，不感知任何数据库、Provider 或 Pydantic 模型，因此可被检索包内任何层引用。
"""

from __future__ import annotations

from enum import StrEnum


class RetrievalChannel(StrEnum):
    """标记一个候选由全文、向量或两种检索通道中的哪些通道命中。

    通道信息随结果返回，使 Planner、Auditor 和评测能够区分关键词命中与 embedding 相似度，
    防止把融合后的单个分数误解为不可解释的模型判断；字符串枚举便于 API 稳定序列化。
    """

    LEXICAL = "lexical"
    VECTOR = "vector"


def bounded_score(value: float) -> float:
    """把数据库或浮点组合分裁剪到统一零到一范围，避免边界误差破坏 Schema。

    PostgreSQL ts_rank 没有固定上界，cosine 和小数加权也可能出现极小浮点越界；集中裁剪使所有
    对外分数遵守契约。该函数不做归一化或重新排序，因此不会掩盖配置权重错误。
    """

    return max(0.0, min(1.0, float(value)))


def blend_scores(hybrid_score: float, rerank_score: float, *, blend: float) -> float:
    """按显式权重线性融合一阶段混合分与二阶段重排分，并保持结果落在零到一。

    选择线性融合而不是直接用重排分覆盖，是因为 cross-encoder 只看查询与文本的语义匹配，完全不知道
    节点可靠性、图路径强度和检索通道；两者相加才能既吸收精排的判别力，又保留知识库自身的可信度
    信息。权重进入检索结果契约，因此任何名次变化都能被归因到具体的一个数字而不是隐藏策略。
    """

    return bounded_score((1 - blend) * hybrid_score + blend * rerank_score)


def default_final_score(data: object) -> object:
    """在校验前把缺失的 `final_score` 填充为同一负载中的 `hybrid_score`。

    只处理 dict 负载，其它输入（已构造模型、ORM 对象）原样交给 Pydantic，避免这里替 Pydantic 猜测
    未知输入形态。`hybrid_score` 缺失时也不做补齐，让原本的 missing 错误照常报告在正确字段上。
    """

    if isinstance(data, dict) and "final_score" not in data and "hybrid_score" in data:
        return {**data, "final_score": data["hybrid_score"]}
    return data


def validate_rerank_consistency(
    hybrid_score: float,
    rerank_score: float | None,
    final_score: float,
) -> None:
    """校验"最终分偏离一阶段分"必须由存在的二阶段分数解释，否则拒绝构造。

    容差按浮点线性融合的量级取 1e-9，只吸收二进制表示误差而不放过真实的分数改写。该函数不修正
    数值：静默归一化会让一个错误的融合权重看起来完全正常，而这正是评测最需要发现的问题。
    """

    if rerank_score is None and abs(final_score - hybrid_score) > 1e-9:
        raise ValueError("final_score must equal hybrid_score when no rerank score is present")
