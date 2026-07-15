"""管理长期记忆 API/工作流调用的短事务 AsyncSession 生命周期。

进程级 runtime 只保存 async_sessionmaker、Embedding Provider 和配置；每次写操作使用独立事务，
搜索使用只读会话。这样 FastAPI 可以安全复用入口，而不会跨请求共享 AsyncSession。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import CaseMemory, MemoryStatus
from app.memory.graph_registration import PostgresCaseGraphRegistrar
from app.memory.models import (
    CaseMemoryMatch,
    MemoryCounts,
    MemoryDecision,
    MemoryRetrievalMode,
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
        graph_similarity_threshold: float = 0.75,
    ) -> None:
        """保存会话工厂、Provider 和经过范围校验的记忆预算。

        构造不连接数据库或生成向量；两个 threshold 均为零到一，limit 为一到二十。图阈值独立
        于去重阈值，使相似但不应合并的案例仍可建立关系；非法配置在 FastAPI lifespan 发布
        runtime 前失败。
        """

        if not 0 <= dedup_similarity_threshold <= 1:
            raise ValueError("memory dedup threshold must be between zero and one")
        if not 1 <= default_search_limit <= 20:
            raise ValueError("memory default search limit must be between one and twenty")
        if not 0 <= graph_similarity_threshold <= 1:
            raise ValueError("case graph similarity threshold must be between zero and one")
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._dedup_similarity_threshold = dedup_similarity_threshold
        self._graph_similarity_threshold = graph_similarity_threshold
        self.default_search_limit = default_search_limit

    async def stage(self, result: ReportRunResult) -> MemoryStageResult:
        """在一个原子事务中暂存或合并 accepted 报告案例。

        Service 的 advisory lock、查重、写主表和 evidence 关联共享同一 AsyncSession；任何 Provider、
        SQL 或 Pydantic 失败都会由 begin context 回滚，不留下部分 occurrence 或孤立关联。
        """

        # staging 会同时改主表、出现次数和 Evidence 关联，因此必须共享一个 begin 上下文；
        # 任何一步失败都由 SQLAlchemy 回滚，避免产生“案例已增加但审计来源缺失”的状态。
        async with self._session_factory.begin() as session:
            repository = PostgresCaseMemoryRepository(session)
            service = self._service(session, repository=repository)
            stage_result = await service.stage_from_report(result)

            # 新候选默认 pending，不进入图；若重复报告合并到已 confirmed 的 canonical 案例，正文和
            # embedding 已变化，必须在同一事务立即重建节点/边，避免长期记忆与 GraphRAG 分叉。
            if (
                stage_result.memory is not None
                and stage_result.memory.status is MemoryStatus.CONFIRMED
            ):
                stored = await repository.get_stored(
                    stage_result.memory.memory_id,
                    for_update=True,
                )
                if stored is None:  # pragma: no cover - same transaction just returned the row.
                    raise LookupError(
                        f"confirmed case memory disappeared: {stage_result.memory.memory_id}"
                    )
                await self._graph_registrar(session).register_confirmed(stored)
            return stage_result

    async def decide(self, memory_id: str, decision: MemoryDecision) -> CaseMemory | None:
        """在事务中执行用户 confirm/reject 决策并提交状态更新时间。

        未命中返回 None 且事务无变更；confirm 同步注册节点/边，reject 删除节点并级联清边。状态与
        图共享事务，任一图写入异常都会回滚状态；方法不允许返回 embedding 或 ORM Record。
        """

        async with self._session_factory.begin() as session:
            repository = PostgresCaseMemoryRepository(session)
            service = self._service(session, repository=repository)
            memory = await service.decide(memory_id, decision)
            if memory is None:
                return None

            registrar = self._graph_registrar(session)
            if decision is MemoryDecision.CONFIRM:
                # set_status 已锁定并 flush 当前行；再次读取内部快照不会跨事务，且能保证图节点复用
                # 与最终持久化内容完全相同的 embedding/provider/dimensions。
                stored = await repository.get_stored(memory_id, for_update=True)
                if stored is None:  # pragma: no cover - guarded by the successful status update.
                    raise LookupError(f"confirmed case memory disappeared: {memory_id}")
                await registrar.register_confirmed(stored)
            else:
                await registrar.remove(memory_id)
            return memory

    async def delete(self, memory_id: str) -> CaseMemory | None:
        """在单一事务内永久删除案例、证据关联和动态案例图节点。

        先锁定并读取 embedding 所在的持久化行，再调用图注册器清理节点，最后删除
        主记录；任一步失败都会回滚，避免出现“数据库已删但图仍可召回”的分裂状态。
        该能力只用于用户明确请求的清理，不会被诊断流程自动调用。
        """

        async with self._session_factory.begin() as session:
            repository = PostgresCaseMemoryRepository(session)
            stored = await repository.get_stored(memory_id, for_update=True)
            if stored is None:
                return None
            # Graph registrar 的 remove 对不存在节点保持幂等，兼容历史数据只写入主表的情况。
            await self._graph_registrar(session).remove(memory_id)
            return await repository.delete(memory_id)

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        mode: MemoryRetrievalMode = MemoryRetrievalMode.VECTOR_GRAPH,
    ) -> list[CaseMemoryMatch]:
        """在短只读会话中搜索 confirmed 向量/图融合案例，缺省使用集中配置 limit。

        直接 pgvector 与可选 SIMILAR_TO join 共享同一事务快照且不显式 commit；``mode`` 仅供内部
        评测显式关闭图扩展，API 不透传它。离开会话释放连接，SQL/Provider 异常原样传播给错误
        边界，不能伪装为空历史。
        """

        selected_limit = self.default_search_limit if limit is None else limit
        # 搜索只需要事务一致性快照，不需要显式 commit；短会话在上下文退出时归还连接，避免 FastAPI
        # 并发请求复用同一个非线程安全 AsyncSession。
        async with self._session_factory() as session:
            service = self._service(session)
            return await service.search_confirmed(query, limit=selected_limit, mode=mode)

    async def counts(self) -> MemoryCounts:
        """在短只读会话中返回三种状态计数，供健康接口和测试使用。

        方法不生成 embedding 或加载案例 JSONB；会话离开上下文后归还连接。返回值始终使用
        ``MemoryCounts`` 的 pending/confirmed/rejected 三字段契约，数据库连接或 SQL 失败会原样向上
        传播，由 API 生命周期边界决定是否降级为不可用状态，而不是在这里伪造零计数。
        """

        async with self._session_factory() as session:
            service = self._service(session)
            return await service.count_by_status()

    def _service(
        self,
        session: AsyncSession,
        *,
        repository: PostgresCaseMemoryRepository | None = None,
    ) -> CaseMemoryService:
        """为一个请求会话组装仓储和 CaseMemoryService。

        每次调用返回新 Service，防止 AsyncSession 跨请求共享；写路径可传入已创建的 repository，
        让状态服务和图注册读取同一对象/事务。Provider/阈值是不可变进程配置。
        """

        return CaseMemoryService(
            repository or PostgresCaseMemoryRepository(session),
            self._embedding_provider,
            dedup_similarity_threshold=self._dedup_similarity_threshold,
        )

    def _graph_registrar(self, session: AsyncSession) -> PostgresCaseGraphRegistrar:
        """为当前事务构造确定性案例图注册器，不共享 AsyncSession 或提交所有权。

        注册器复用集中配置的图相似阈值；每次调用创建轻量对象，不执行 I/O。SQL、约束或向量空间
        错误由调用者的 ``begin`` 上下文统一回滚，避免 Registrar 私自提交破坏原子性。
        """

        return PostgresCaseGraphRegistrar(
            session,
            similarity_threshold=self._graph_similarity_threshold,
        )
