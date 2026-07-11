"""管理长期记忆 API/工作流调用的短事务 AsyncSession 生命周期。

进程级 runtime 只保存 async_sessionmaker、Embedding Provider 和配置；每次写操作使用独立事务，
搜索使用只读会话。这样 FastAPI 可以安全复用入口，而不会跨请求共享 AsyncSession。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import CaseMemory
from app.memory.models import (
    CaseMemoryMatch,
    MemoryCounts,
    MemoryDecision,
    MemoryStageResult,
)
from app.memory.repository import PostgresCaseMemoryRepository
from app.memory.service import CaseMemoryService
from app.orchestration.report_models import ReportRunResult
from app.retrieval.embeddings import EmbeddingProvider


class PostgresMemoryRuntime:
    """为 staging、决策、搜索和健康计数创建独立数据库会话与 Service。

    Runtime 不持有打开的连接；写方法用 `factory.begin()` 自动 commit/rollback，读方法离开会话即
    归还连接。Embedding Provider 是无会话进程依赖，可安全复用。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider,
        *,
        dedup_similarity_threshold: float,
        default_search_limit: int,
    ) -> None:
        """保存会话工厂、Provider 和经过范围校验的记忆预算。

        构造不连接数据库或生成向量；threshold 为零到一，limit 为一到二十。非法配置在 FastAPI
        lifespan 发布 runtime 前失败。
        """

        if not 0 <= dedup_similarity_threshold <= 1:
            raise ValueError("memory dedup threshold must be between zero and one")
        if not 1 <= default_search_limit <= 20:
            raise ValueError("memory default search limit must be between one and twenty")
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._dedup_similarity_threshold = dedup_similarity_threshold
        self.default_search_limit = default_search_limit

    async def stage(self, result: ReportRunResult) -> MemoryStageResult:
        """在一个原子事务中暂存或合并 accepted 报告案例。

        Service 的 advisory lock、查重、写主表和 evidence 关联共享同一 AsyncSession；任何 Provider、
        SQL 或 Pydantic 失败都会由 begin context 回滚，不留下部分 occurrence 或孤立关联。
        """

        # staging 会同时改主表、出现次数和 Evidence 关联，因此必须共享一个 begin 上下文；
        # 任何一步失败都由 SQLAlchemy 回滚，避免产生“案例已增加但审计来源缺失”的状态。
        async with self._session_factory.begin() as session:
            service = self._service(session)
            return await service.stage_from_report(result)

    async def decide(self, memory_id: str, decision: MemoryDecision) -> CaseMemory | None:
        """在事务中执行用户 confirm/reject 决策并提交状态更新时间。

        未命中返回 None 且事务无变更；异常自动回滚。方法不允许返回 embedding 或 ORM Record。
        """

        async with self._session_factory.begin() as session:
            service = self._service(session)
            return await service.decide(memory_id, decision)

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> list[CaseMemoryMatch]:
        """在短只读会话中搜索 confirmed 案例，缺省使用集中配置 limit。

        只读路径不显式 commit；离开会话释放连接，SQL/Provider 异常原样传播给 API 错误边界。
        """

        selected_limit = self.default_search_limit if limit is None else limit
        # 搜索只需要事务一致性快照，不需要显式 commit；短会话在上下文退出时归还连接，避免 FastAPI
        # 并发请求复用同一个非线程安全 AsyncSession。
        async with self._session_factory() as session:
            service = self._service(session)
            return await service.search_confirmed(query, limit=selected_limit)

    async def counts(self) -> MemoryCounts:
        """在短只读会话中返回三种状态计数，供健康接口和测试使用。

        方法不生成 embedding 或加载案例 JSONB；会话离开上下文后归还连接。返回值始终使用
        ``MemoryCounts`` 的 pending/confirmed/rejected 三字段契约，数据库连接或 SQL 失败会原样向上
        传播，由 API 生命周期边界决定是否降级为不可用状态，而不是在这里伪造零计数。
        """

        async with self._session_factory() as session:
            service = self._service(session)
            return await service.count_by_status()

    def _service(self, session: AsyncSession) -> CaseMemoryService:
        """为一个请求会话组装仓储和 CaseMemoryService。

        每次调用返回新 Service，防止 AsyncSession 跨请求共享；Provider/阈值是不可变进程配置。
        """

        return CaseMemoryService(
            PostgresCaseMemoryRepository(session),
            self._embedding_provider,
            dedup_similarity_threshold=self._dedup_similarity_threshold,
        )
