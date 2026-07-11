"""公开经审计长期案例记忆的候选、去重、状态决策和检索服务。

本包只在 Auditor accepted 后暂存 pending 记忆，使用 PostgreSQL/pgvector 去重，并只召回 confirmed
案例；confirmed 还会在同一事务注册 GraphRAG case 节点和相似边。它不是第三个 Agent，也不覆盖
本次实时 Observation。
"""

from app.memory.evaluation import (
    MEMORY_RECALL_EVAL_CONTRACT_ID,
    evaluate_memory_recall,
    load_memory_recall_eval_suite,
)
from app.memory.models import (
    CASE_MEMORY_CONTRACT_ID,
    CaseMemoryMatch,
    MemoryCounts,
    MemoryDecision,
    MemoryDuplicateType,
    MemoryRetrievalChannel,
    MemoryRetrievalMode,
    MemoryStageResult,
    MemoryStageStatus,
)
from app.memory.runtime import PostgresMemoryRuntime
from app.memory.service import CaseMemoryService

__all__ = [
    "CASE_MEMORY_CONTRACT_ID",
    "MEMORY_RECALL_EVAL_CONTRACT_ID",
    "CaseMemoryMatch",
    "CaseMemoryService",
    "MemoryCounts",
    "MemoryDecision",
    "MemoryDuplicateType",
    "MemoryRetrievalChannel",
    "MemoryRetrievalMode",
    "MemoryStageResult",
    "MemoryStageStatus",
    "PostgresMemoryRuntime",
    "evaluate_memory_recall",
    "load_memory_recall_eval_suite",
]
