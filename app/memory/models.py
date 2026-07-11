"""定义长期记忆服务、PostgreSQL 仓储和 API 共享的强类型结果模型。

CaseMemory 本身不携带 embedding，避免大向量进入 Planner Prompt；内部 StoredCaseMemory 单独保存
向量空间元数据。所有状态和匹配类型使用有限枚举，API 无需解析自然语言。
"""

from __future__ import annotations

from enum import StrEnum
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.models import CaseMemory, MemoryStatus

CASE_MEMORY_CONTRACT_ID = "case-memory:v1"


class MemoryDecision(StrEnum):
    """限定用户对 pending/历史案例记忆可做的显式确认或拒绝操作。

    confirm 使案例进入默认召回，reject 立即移出；两种决策都由用户/API 触发，不允许模型自行选择。
    """

    CONFIRM = "confirm"
    REJECT = "reject"


class MemoryStageStatus(StrEnum):
    """区分候选新增、重复合并和两个无写入的安全跳过原因。

    skipped 状态让调用方解释为什么没有 memory_candidate，而不是返回 None 后猜测；只有 staged 和
    merged 含实际 CaseMemory。
    """

    STAGED = "staged"
    MERGED = "merged"
    SKIPPED_NOT_ACCEPTED = "skipped_not_accepted"
    SKIPPED_NO_ROOT_CAUSE = "skipped_no_root_cause"


class MemoryDuplicateType(StrEnum):
    """标记候选是新记录、精确签名命中还是 pgvector 相似命中。

    类型进入测试和 API 审计，但不改变 status；重复案例保持已有 pending/confirmed/rejected 状态，
    防止新模型输出自动重新确认旧记录。
    """

    NONE = "none"
    EXACT_SIGNATURE = "exact_signature"
    VECTOR_SIMILARITY = "vector_similarity"


class StoredCaseMemory(BaseModel):
    """封装数据库内部 CaseMemory、签名和不进入 Prompt 的 embedding 元数据。

    向量必须非空、有限、非零并与 dimensions 一致；Provider ID 与签名用于隔离数学空间和精确去重。
    该模型只在 memory/persistence 层传递，不应直接作为 API 响应。
    """

    model_config = ConfigDict(extra="forbid")

    memory: CaseMemory
    signature: str = Field(pattern=r"^[a-f0-9]{64}$")
    embedding: list[float] = Field(min_length=8, max_length=4096)
    embedding_provider: str = Field(min_length=1, max_length=100)
    embedding_dimensions: int = Field(ge=8, le=4096)

    @model_validator(mode="after")
    def validate_embedding_space(self) -> StoredCaseMemory:
        """校验向量长度、有限值和非零范数，拒绝不可比较的数据库载荷。

        Provider 生成错误或数据库污染会在仓储边界失败，不会把全零/NaN 向量交给 cosine 查询；
        模型不要求单位范数，因为不同合法 Provider 可选择自己的标准化策略。
        """

        if len(self.embedding) != self.embedding_dimensions:
            raise ValueError("memory embedding length must match dimensions")
        if not all(isfinite(value) for value in self.embedding):
            raise ValueError("memory embedding values must be finite")
        if not any(value != 0 for value in self.embedding):
            raise ValueError("memory embedding must not be all zero")
        return self


class MemoryDuplicateMatch(BaseModel):
    """表示仓储发现的一个精确或向量重复候选及相似度。

    exact 签名相似度固定为一；vector 匹配来自 PostgreSQL cosine。模型冻结，Service 可据此合并
    字段但不能修改匹配类型或原存储对象。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    stored: StoredCaseMemory
    duplicate_type: MemoryDuplicateType
    similarity: float = Field(ge=0, le=1)


class MemoryStageResult(BaseModel):
    """返回审计后记忆暂存的新增、合并或安全跳过结果。

    staged/merged 必须携带 memory，跳过状态必须为空；duplicate_type 和 similarity 解释去重路径。
    该结果可写入运行事件，但不包含 embedding 或原始模型输出。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MemoryStageStatus
    memory: CaseMemory | None = None
    duplicate_type: MemoryDuplicateType = MemoryDuplicateType.NONE
    similarity: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_status_payload(self) -> MemoryStageResult:
        """绑定写入/跳过状态与 memory、重复信息，形成无歧义结果。

        新增必须 duplicate=none 且 similarity 为空；合并必须说明匹配类型和分数；跳过不能伪装已
        创建对象。矛盾组合在返回 API 或更新 AgentState 前失败。
        """

        if self.status is MemoryStageStatus.STAGED:
            if self.memory is None or self.duplicate_type is not MemoryDuplicateType.NONE:
                raise ValueError("staged memory requires a new memory and no duplicate type")
            if self.similarity is not None:
                raise ValueError("new memory cannot have duplicate similarity")
            return self
        if self.status is MemoryStageStatus.MERGED:
            if self.memory is None or self.duplicate_type is MemoryDuplicateType.NONE:
                raise ValueError("merged memory requires a duplicate match")
            if self.similarity is None:
                raise ValueError("merged memory requires similarity")
            return self
        if self.memory is not None or self.duplicate_type is not MemoryDuplicateType.NONE:
            raise ValueError("skipped memory stage cannot return a stored memory")
        if self.similarity is not None:
            raise ValueError("skipped memory stage cannot return similarity")
        return self


class CaseMemoryMatch(BaseModel):
    """表示默认历史检索返回的一个 confirmed 案例和 cosine 相似度。

    模型拒绝 pending/rejected 记录，从响应 Schema 层保证历史 capability 不会看到未确认记忆；
    embedding 不进入输出，后续比较共同点/差异点时只使用结构化案例字段。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory: CaseMemory
    similarity: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_confirmed_memory(self) -> CaseMemoryMatch:
        """保证搜索命中只能是 confirmed，避免仓储过滤漂移污染 Planner。

        相似度零仍是合法有界结果，但仓储通常按 limit 返回最高分；状态错误直接抛出而不是静默
        过滤，使数据库或查询缺陷能被测试发现。
        """

        if self.memory.status is not MemoryStatus.CONFIRMED:
            raise ValueError("default memory matches must be confirmed")
        return self


class MemoryCounts(BaseModel):
    """保存健康检查公开的 pending/confirmed/rejected 记忆数量。

    计数来自同一数据库事务快照，不包含案例内容或向量；非负约束防止错误聚合被健康接口接受。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pending: int = Field(ge=0)
    confirmed: int = Field(ge=0)
    rejected: int = Field(ge=0)
