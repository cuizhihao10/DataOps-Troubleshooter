"""把已确认长期案例确定性注册到 PostgreSQL GraphRAG，并维护案例相似关系。

本模块不新增 Agent，也不调用 Embedding Provider。它复用 ``case_memories`` 已审计、已持久化的
embedding，把 confirmed 案例投影为 ``case`` 节点，并在同一向量空间内建立双向 ``SIMILAR_TO``
边。调用方拥有 AsyncSession 和事务，因此任一节点/边写入失败都会连同记忆状态变更一起回滚。
"""

from __future__ import annotations

from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import CaseMemory, Component, MemoryStatus
from app.memory.models import StoredCaseMemory
from app.persistence.models import (
    CaseMemoryRecord,
    KnowledgeEdgeRecord,
    KnowledgeNodeRecord,
)
from app.retrieval.models import (
    KnowledgeEdge,
    KnowledgeNode,
    KnowledgeNodeType,
    KnowledgeRelationType,
)

CASE_GRAPH_SOURCE_ID = "case-memory-graph:v1"
CASE_GRAPH_NODE_RELIABILITY = 0.9
CASE_GRAPH_MAX_NEIGHBORS = 20


class CaseGraphRegistrationResult(BaseModel):
    """描述一次内部图同步实际产生的节点和相似边结果。

    该模型只用于 runtime 测试与可观测性扩展，不进入公开 ``case-memory:v2`` API。``node_id`` 是
    稳定的案例图标识，``neighbor_node_ids`` 与 ``edge_ids`` 均按确定性顺序返回；数据库或校验失败
    时不会构造部分结果，而是向上抛错并由外层事务回滚。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    neighbor_node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)


class PostgresCaseGraphRegistrar:
    """在调用方事务内维护 confirmed 案例节点和受控双向相似边。

    Registrar 直接使用与记忆 Service 相同的 AsyncSession，不自行 commit。注册时先 upsert 当前节点，
    再删除本组件拥有的旧相似边并按最新 confirmed 快照重建；拒绝时删除节点并依赖外键级联清边。
    这种“删后重建”策略以小规模作品集图为前提，换取幂等、无陈旧边且容易审计的行为。
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        similarity_threshold: float,
        max_neighbors: int = CASE_GRAPH_MAX_NEIGHBORS,
    ) -> None:
        """注入事务会话、最低 cosine 相似度和单案例最大邻居预算。

        阈值必须位于零到一，邻居预算限制为 1..100；构造器不执行 SQL。独立阈值应低于或等于
        记忆去重阈值，使“相似但不应合并”的案例可以保留为两个节点并建立关系。非法配置在应用
        启动或测试组装阶段失败，而不是等到用户确认后产生半写入。
        """

        if not 0 <= similarity_threshold <= 1:
            raise ValueError("case graph similarity threshold must be between zero and one")
        if not 1 <= max_neighbors <= 100:
            raise ValueError("case graph max neighbors must be between one and one hundred")
        self._session = session
        self._similarity_threshold = similarity_threshold
        self._max_neighbors = max_neighbors

    async def register_confirmed(
        self,
        stored: StoredCaseMemory,
    ) -> CaseGraphRegistrationResult:
        """注册一个 confirmed 案例节点，并按最新向量快照重建双向相似边。

        输入必须包含 confirmed 领域对象及其内部 embedding 元数据；pending/rejected 会被拒绝。方法
        复用该向量而不再次调用 Provider，候选只来自相同 Provider/维度且仍为 confirmed 的案例。
        节点冲突、pgvector 查询、外键或唯一约束失败都会传播，让外层记忆事务整体回滚。
        """

        if stored.memory.status is not MemoryStatus.CONFIRMED:
            raise ValueError("only confirmed case memories can be registered in the graph")

        node = case_graph_node(stored)
        await self._upsert_owned_node(node)

        # 相似度会随 confirmed 案例后续合并而变化；先删除本组件拥有且触及当前节点的旧边，才能
        # 保证重复 confirm 和重新 embedding 后都不会遗留已经低于阈值的关系。
        await self._session.execute(
            delete(KnowledgeEdgeRecord).where(
                KnowledgeEdgeRecord.relation_type == KnowledgeRelationType.SIMILAR_TO.value,
                KnowledgeEdgeRecord.source_id == CASE_GRAPH_SOURCE_ID,
                or_(
                    KnowledgeEdgeRecord.from_node_id == node.node_id,
                    KnowledgeEdgeRecord.to_node_id == node.node_id,
                ),
            )
        )

        neighbors = await self._find_similar_confirmed(stored)
        edge_ids: list[str] = []
        neighbor_node_ids: list[str] = []
        for neighbor, similarity in neighbors:
            # 候选节点也在写边前 upsert：这既满足外键，也会渐进回填升级前已经 confirmed 但尚未
            # 注册的旧案例；它们仍经过来源冲突检查，不能覆盖人工知识种子。
            neighbor_node = case_graph_node(neighbor)
            await self._upsert_owned_node(neighbor_node)
            neighbor_node_ids.append(neighbor_node.node_id)

            # PostgreSQL 图是有向边；相似关系的业务语义是对称的，因此为每对案例写两个方向，
            # 让从任一案例作为种子扩图时都能到达另一个案例。
            for from_node_id, to_node_id in (
                (node.node_id, neighbor_node.node_id),
                (neighbor_node.node_id, node.node_id),
            ):
                edge = case_similarity_edge(
                    from_node_id,
                    to_node_id,
                    similarity=similarity,
                )
                await self._upsert_edge(edge)
                edge_ids.append(edge.edge_id)

        return CaseGraphRegistrationResult(
            node_id=node.node_id,
            neighbor_node_ids=neighbor_node_ids,
            edge_ids=edge_ids,
        )

    async def remove(self, memory_id: str) -> bool:
        """删除指定记忆拥有的动态 case 节点，并依赖级联删除所有关联边。

        节点不存在时返回 ``False``，使重复 reject 保持幂等；存在时只允许删除 ``source_id`` 与
        memory_id 一致的 case 节点。若稳定 node_id 被其他来源占用则显式失败，避免取消确认误删
        人工知识。SQL/约束失败由外层事务回滚记忆状态变化。
        """

        node_id = case_graph_node_id(memory_id)
        existing = await self._session.scalar(
            select(KnowledgeNodeRecord)
            .where(KnowledgeNodeRecord.node_id == node_id)
            .with_for_update()
        )
        if existing is None:
            return False
        _assert_owned_case_node(existing, memory_id=memory_id)

        # knowledge_edges 两端外键均为 ON DELETE CASCADE，因此删除节点是清理所有入边/出边的
        # 单一原子操作，不需要先枚举关系并承担漏删风险。
        await self._session.delete(existing)
        await self._session.flush()
        return True

    async def _upsert_owned_node(self, node: KnowledgeNode) -> None:
        """校验稳定 ID 没有跨来源冲突后，幂等写入完整动态案例节点。

        ``node`` 已通过 Pydantic 验证；方法先锁定同 ID 行以防误覆盖人工种子，再用 PostgreSQL
        upsert 刷新正文、来源和向量。并发唯一冲突或数据库错误必须传播，调用方事务不会提交。
        """

        existing = await self._session.scalar(
            select(KnowledgeNodeRecord)
            .where(KnowledgeNodeRecord.node_id == node.node_id)
            .with_for_update()
        )
        if existing is not None:
            _assert_owned_case_node(existing, memory_id=node.source_id)

        values = {
            "node_id": node.node_id,
            "node_type": node.node_type.value,
            "name": node.name,
            "content": node.content,
            "aliases": list(node.aliases),
            "source_id": node.source_id,
            "source_span": node.source_span,
            "reliability": node.reliability,
            "embedding": list(node.embedding or []),
            "embedding_provider": node.embedding_provider,
            "embedding_dimensions": node.embedding_dimensions,
        }
        statement = insert(KnowledgeNodeRecord).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[KnowledgeNodeRecord.node_id],
            set_={**values, "updated_at": func.now()},
            # WHERE 同时保护“预检查之后、INSERT 之前”发生的并发冲突：错误来源不会被 upsert
            # 覆盖，而是产生 rowcount=0 并让整个记忆事务失败。
            where=and_(
                KnowledgeNodeRecord.node_type == KnowledgeNodeType.CASE.value,
                KnowledgeNodeRecord.source_id == node.source_id,
            ),
        )
        result = await self._session.execute(statement)
        if result.rowcount != 1:
            raise ValueError(
                f"case graph node ID collision for {node.node_id}: concurrent source mismatch"
            )

    async def _find_similar_confirmed(
        self,
        stored: StoredCaseMemory,
    ) -> list[tuple[StoredCaseMemory, float]]:
        """查询同向量空间中达到图阈值的 confirmed 邻居，并返回受控 top-k。

        查询排除自身，但不要求组件完全相同：组件一致且高度相似的候选通常已由记忆去重合并，图边
        主要表达跨组件或未达到合并阈值的可参考先例。零相似度没有关系意义且数据库禁止零权重，
        即使配置阈值为零也会过滤；Provider、维度或 SQL 失败均显式传播。
        """

        distance = CaseMemoryRecord.embedding.cosine_distance(stored.embedding)
        result = await self._session.execute(
            select(CaseMemoryRecord, distance.label("cosine_distance"))
            .where(
                CaseMemoryRecord.memory_id != stored.memory.memory_id,
                CaseMemoryRecord.status == MemoryStatus.CONFIRMED.value,
                CaseMemoryRecord.embedding_provider == stored.embedding_provider,
                CaseMemoryRecord.embedding_dimensions == stored.embedding_dimensions,
            )
            .order_by(distance, CaseMemoryRecord.updated_at.desc(), CaseMemoryRecord.memory_id)
            .limit(self._max_neighbors)
        )

        neighbors: list[tuple[StoredCaseMemory, float]] = []
        for record, raw_distance in result:
            similarity = _bounded_similarity(raw_distance)
            if similarity <= 0 or similarity < self._similarity_threshold:
                continue
            neighbors.append((_stored_from_record(record), similarity))
        return neighbors

    async def _upsert_edge(self, edge: KnowledgeEdge) -> None:
        """按稳定方向性 edge_id 幂等写入一条受本组件拥有的 SIMILAR_TO 边。

        两端节点必须已在当前事务中存在，权重来自有界 cosine 相似度。冲突时刷新关系字段和时间；
        唯一约束、外键或连接错误不会被吞掉，从而阻止记忆状态与图关系分叉。
        """

        values = {
            "edge_id": edge.edge_id,
            "from_node_id": edge.from_node_id,
            "to_node_id": edge.to_node_id,
            "relation_type": edge.relation_type.value,
            "weight": edge.weight,
            "source_id": edge.source_id,
            "source_span": edge.source_span,
        }
        statement = insert(KnowledgeEdgeRecord).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[KnowledgeEdgeRecord.edge_id],
            set_={**values, "updated_at": func.now()},
        )
        await self._session.execute(statement)


def case_graph_node_id(memory_id: str) -> str:
    """把 ``mem_<16hex>`` 长期记忆 ID 映射为稳定且可校验的 GraphRAG 节点 ID。

    映射保留完整记忆后缀，便于演示和排障时人工追踪；严格格式校验阻止任意 API 文本进入图主键，
    也避免两个非规范 memory_id 在 ``removeprefix`` 后意外碰撞。非法输入抛出 ``ValueError``。
    """

    prefix = "mem_"
    suffix = memory_id.removeprefix(prefix)
    if not memory_id.startswith(prefix) or len(suffix) != 16:
        raise ValueError("case graph memory_id must use mem_<16 hex> format")
    if any(character not in "0123456789abcdef" for character in suffix):
        raise ValueError("case graph memory_id suffix must be lowercase hexadecimal")
    return f"case_{suffix}"


def case_graph_node(stored: StoredCaseMemory) -> KnowledgeNode:
    """把内部案例存储快照投影为带来源、正文和复用向量的 GraphRAG case 节点。

    字段顺序稳定，正文最多 4000 字符，名称最多 300 字符；aliases 只使用组件与标签，不把证据 ID
    当成搜索别名。embedding/provider/dimensions 原样复用，避免确认动作再次访问远端模型或让图与
    记忆处于不同数学空间。Pydantic 会拒绝损坏向量或非法节点字段。
    """

    memory = stored.memory
    node_id = case_graph_node_id(memory.memory_id)
    aliases = list(
        dict.fromkeys(
            [
                *(component.value for component in memory.components),
                *(tag for tag in memory.tags if tag.strip()),
            ]
        )
    )
    content = _bounded_case_content(memory)
    name_prefix = "历史案例："
    name = name_prefix + memory.root_cause[: 300 - len(name_prefix)]
    return KnowledgeNode(
        node_id=node_id,
        node_type=KnowledgeNodeType.CASE,
        name=name,
        content=content,
        aliases=aliases,
        source_id=memory.memory_id,
        source_span=(
            f"来自已确认结构化案例 {memory.memory_id}；字段由审计通过的诊断报告投影，"
            "不包含模型原始思维链。"
        ),
        reliability=CASE_GRAPH_NODE_RELIABILITY,
        embedding=list(stored.embedding),
        embedding_provider=stored.embedding_provider,
        embedding_dimensions=stored.embedding_dimensions,
    )


def case_similarity_edge(
    from_node_id: str,
    to_node_id: str,
    *,
    similarity: float,
) -> KnowledgeEdge:
    """构造一条方向明确、ID 稳定且来源可解释的 ``SIMILAR_TO`` 关系。

    ``similarity`` 必须位于 ``(0, 1]``；方向参与 SHA-256 输入，因此 A→B 与 B→A 得到不同但稳定
    的 edge_id。摘要只用于小型仓库标识而非安全签名，Pydantic/数据库继续校验自环、长度和权重。
    """

    if from_node_id == to_node_id:
        raise ValueError("case similarity edge cannot be a self-loop")
    if not 0 < similarity <= 1:
        raise ValueError("case similarity must be greater than zero and at most one")
    digest = sha256(f"{CASE_GRAPH_SOURCE_ID}|{from_node_id}|{to_node_id}".encode()).hexdigest()[:16]
    return KnowledgeEdge(
        edge_id=f"edge_case_similar_{digest}",
        from_node_id=from_node_id,
        to_node_id=to_node_id,
        relation_type=KnowledgeRelationType.SIMILAR_TO,
        weight=similarity,
        source_id=CASE_GRAPH_SOURCE_ID,
        source_span=(
            f"confirmed 案例节点 {from_node_id} 与 {to_node_id} 位于相同 embedding 空间，"
            f"cosine similarity={similarity:.6f}。"
        ),
    )


def _bounded_case_content(memory: CaseMemory) -> str:
    """按稳定语义顺序生成案例节点正文，并裁剪到知识节点 4000 字符上限。

    根因和症状放在最前，确保极长路径/方案被裁剪时仍保留最关键检索语义；管理时间、状态和出现
    次数不进入正文，避免相同事实仅因确认时间变化而改变全文检索内容。空列表用“无”显式表示。
    """

    sections = [
        "症状：" + ("；".join(memory.symptoms) or "无"),
        "根因：" + memory.root_cause,
        "故障路径：" + ("；".join(memory.fault_path) or "无"),
        "解决方案：" + ("；".join(memory.solution_steps) or "无"),
        "组件：" + "；".join(component.value for component in memory.components),
        "标签：" + ("；".join(memory.tags) or "无"),
        "证据引用：" + "；".join(memory.evidence_refs),
    ]
    return "\n".join(sections)[:4000]


def _stored_from_record(record: CaseMemoryRecord) -> StoredCaseMemory:
    """把 confirmed ORM 行重建为受校验内部存储模型，供邻居节点渐进注册。

    转换显式复制 JSONB 和 pgvector 值并恢复领域枚举；数据库若包含未知状态、无时区时间或损坏向量，
    Pydantic 会立即失败并回滚当前图同步，而不是把污染数据扩散到知识图。
    """

    memory = CaseMemory(
        memory_id=record.memory_id,
        symptoms=list(record.symptoms),
        root_cause=record.root_cause,
        fault_path=list(record.fault_path),
        solution_steps=list(record.solution_steps),
        components=[Component(item) for item in record.components],
        tags=list(record.tags),
        evidence_refs=list(record.evidence_refs),
        status=MemoryStatus(record.status),
        occurrence_count=record.occurrence_count,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
    return StoredCaseMemory(
        memory=memory,
        signature=record.signature,
        embedding=list(record.embedding),
        embedding_provider=record.embedding_provider,
        embedding_dimensions=record.embedding_dimensions,
    )


def _assert_owned_case_node(record: KnowledgeNodeRecord, *, memory_id: str) -> None:
    """确认稳定节点 ID 仍属于预期记忆来源，防止 upsert/reject 覆盖人工知识。

    合法动态节点必须同时满足 ``node_type=case`` 与 ``source_id=memory_id``。任何冲突都抛出
    ``ValueError``，让外层事务回滚；静默接管或删除别的来源会破坏 GraphRAG 可追溯性。
    """

    if record.node_type != KnowledgeNodeType.CASE.value or record.source_id != memory_id:
        raise ValueError(
            f"case graph node ID collision for {record.node_id}: existing source is not {memory_id}"
        )


def _bounded_similarity(raw_distance: object) -> float:
    """把 pgvector cosine distance 转为零到一的关系权重并吸收浮点边界误差。

    cosine 原始相似度理论上可为负，而图边权契约只允许正值；本函数先计算 ``1-distance`` 再裁剪，
    调用方会进一步排除零。无法转换的数据库值抛出 ``TypeError``/``ValueError`` 并触发事务回滚。
    """

    return max(0.0, min(1.0, 1.0 - float(raw_distance)))
