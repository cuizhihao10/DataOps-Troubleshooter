"""PostgreSQL 全文种子召回和递归图路径仓储。

仓储使用 JSONB 保存别名、GIN 全文表达式召回种子，并用 WITH RECURSIVE 扩展最多两跳。
路径数组用于防环和保序，随后再加载完整节点/边生成可追溯 GraphPath。
"""

from __future__ import annotations

from hashlib import sha256

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import String

from app.persistence.models import KnowledgeEdgeRecord, KnowledgeNodeRecord
from app.retrieval.models import (
    GraphPath,
    KnowledgeEdge,
    KnowledgeNode,
    KnowledgeRelationType,
    KnowledgeSeedBundle,
    LexicalSeedMatch,
)


class PostgresGraphRepository:
    """封装知识种子 upsert、全文种子召回和有界递归图路径加载。

    仓储只负责数据库查询与 Record/领域模型转换，不生成排障结论。AsyncSession 由调用方管理事务，
    写入不自动 commit；所有 SQL 参数绑定并限制 top-k、关系白名单和最大两跳，以控制成本和边界。
    """

    def __init__(self, session: AsyncSession) -> None:
        """注入调用方拥有的异步会话，使多个仓储操作可共享同一事务。

        构造器不打开连接、不提交也不回滚；这种所有权边界允许种子写入原子提交，也允许集成测试
        在事务中删边后回滚消融。会话失效或 SQL 错误会原样传播给上层生命周期。
        """

        self._session = session

    async def upsert_seed_bundle(self, bundle: KnowledgeSeedBundle) -> None:
        """按稳定主键先 upsert 全部节点、再 upsert 全部边，但不提交事务。

        PostgreSQL `ON CONFLICT DO UPDATE` 让容器重复启动保持幂等，并刷新 updated_at；节点先写保证
        新边外键可满足。Bundle 已完成跨元素校验，但数据库约束仍是最终防线。任何错误中断当前
        会话事务，是否回滚/提交由调用方依据整个种子操作的原子性决定。
        """

        # 节点先于边写入是外键依赖要求；逐条 upsert 保留教学可读性和精确失败位置。
        for node in bundle.nodes:
            values = {
                "node_id": node.node_id,
                "node_type": node.node_type.value,
                "name": node.name,
                "content": node.content,
                "aliases": node.aliases,
                "source_id": node.source_id,
                "source_span": node.source_span,
                "reliability": node.reliability,
                "embedding": node.embedding,
            }
            # 冲突更新完整来源字段，避免旧版本种子在重复部署后残留过时内容。
            statement = insert(KnowledgeNodeRecord).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[KnowledgeNodeRecord.node_id],
                set_={**values, "updated_at": func.now()},
            )
            await self._session.execute(statement)

        # 所有节点已进入同一事务后再写边，尚未 commit 的节点对该会话仍可满足外键检查。
        for edge in bundle.edges:
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

    async def count_graph(self) -> tuple[int, int]:
        """查询当前事务视图中的知识节点数和边数，并规范化为空时的返回值。

        两个独立 COUNT 便于健康检查明确报告图规模；`scalar` 理论上返回整数，但使用 `or 0` 防守
        方言或测试替身返回 None。方法不提交事务，也不加载完整图内容。
        """

        node_count = await self._session.scalar(
            select(func.count()).select_from(KnowledgeNodeRecord)
        )
        edge_count = await self._session.scalar(
            select(func.count()).select_from(KnowledgeEdgeRecord)
        )
        return int(node_count or 0), int(edge_count or 0)

    async def search_lexical_seeds(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[LexicalSeedMatch]:
        """使用 PostgreSQL 全文排名与名称/别名包含 bonus 召回有界种子节点。

        空查询和越界 limit 在发 SQL 前拒绝；`websearch_to_tsquery('simple')` 提供稳定英文标识符
        检索，LIKE bonus 补足短组件名和别名。参数全部绑定防止注入，结果按分数、可靠性和 ID
        确定性排序。当前返回的是 lexical score，不声称已完成 embedding 语义召回。
        """

        if not query.strip():
            raise ValueError("query must not be blank")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")

        # CTE 先计算全文分数和精确短标识 bonus，外层再过滤、排序并应用 top-k 预算。
        statement = text(
            """
            WITH ranked AS (
                SELECT
                    node_id,
                    node_type,
                    name,
                    content,
                    aliases,
                    source_id,
                    source_span,
                    reliability,
                    embedding,
                    ts_rank(
                        to_tsvector(
                            'simple',
                            coalesce(name, '') || ' ' || coalesce(content, '') ||
                            ' ' || coalesce(aliases::text, '')
                        ),
                        websearch_to_tsquery('simple', :query)
                    ) AS text_rank,
                    CASE
                        WHEN lower(name) LIKE lower(:pattern) THEN 0.5
                        WHEN lower(aliases::text) LIKE lower(:pattern) THEN 0.25
                        ELSE 0
                    END AS lexical_bonus
                FROM knowledge_nodes
            )
            SELECT *, greatest(text_rank + lexical_bonus, 0.001) AS lexical_score
            FROM ranked
            WHERE text_rank > 0 OR lexical_bonus > 0
            ORDER BY lexical_score DESC, reliability DESC, node_id
            LIMIT :limit
            """
        )
        # 查询文本、LIKE 模式和 limit 都通过绑定参数传入，用户输入不会拼接进 SQL 结构。
        result = await self._session.execute(
            statement,
            {
                "query": query,
                "pattern": f"%{query}%",
                "limit": limit,
            },
        )
        # 每行立即转换成领域模型，在仓储边界拒绝数据库中的类型或约束漂移。
        return [
            LexicalSeedMatch(
                node=_node_from_mapping(row._mapping),
                lexical_score=float(row.lexical_score),
            )
            for row in result
        ]

    async def expand_paths(
        self,
        seed_node_id: str,
        *,
        max_hops: int = 2,
        allowed_relations: set[KnowledgeRelationType] | None = None,
    ) -> list[GraphPath]:
        """从一个种子节点沿允许关系扩展一至两跳并返回完整、可引用路径。

        递归 CTE 用 node_ids 数组保序并防环，用 edge_ids 数组支持稳定 path_id；边权逐跳相乘形成
        路径分数。查询先返回轻量 ID 数组，再批量加载涉及的节点/边，避免递归行重复携带大文本。
        max_hops 超出产品预算会在 SQL 前失败，空关系白名单直接返回空结果。
        """

        if max_hops not in {1, 2}:
            raise ValueError("max_hops must be 1 or 2")

        # None 表示采用完整批准枚举；显式空集合表示调用方禁止所有扩展，二者语义不同。
        relations = set(KnowledgeRelationType) if allowed_relations is None else allowed_relations
        if not relations:
            return []

        # 递归项只从当前路径末节点继续，并拒绝目标已在 node_ids 中，形成简单有向路径。
        statement = text(
            """
            WITH RECURSIVE graph_paths AS (
                SELECT
                    ARRAY[e.from_node_id, e.to_node_id]::varchar[] AS node_ids,
                    ARRAY[e.edge_id]::varchar[] AS edge_ids,
                    1 AS depth,
                    e.weight::double precision AS path_score
                FROM knowledge_edges e
                WHERE e.from_node_id = :seed_node_id
                  AND e.relation_type = ANY(:relations)

                UNION ALL

                SELECT
                    gp.node_ids || e.to_node_id,
                    gp.edge_ids || e.edge_id,
                    gp.depth + 1,
                    (gp.path_score * e.weight)::double precision
                FROM graph_paths gp
                JOIN knowledge_edges e ON e.from_node_id = gp.node_ids[array_length(gp.node_ids, 1)]
                WHERE gp.depth < :max_hops
                  AND e.relation_type = ANY(:relations)
                  AND NOT e.to_node_id = ANY(gp.node_ids)
            )
            SELECT node_ids, edge_ids, depth, path_score
            FROM graph_paths
            ORDER BY depth, path_score DESC, edge_ids
            """
        ).bindparams(bindparam("relations", type_=ARRAY(String())))
        # ARRAY(String) 明确告诉 SQLAlchemy 如何绑定关系白名单，避免驱动猜测数组元素类型。
        result = await self._session.execute(
            statement,
            {
                "seed_node_id": seed_node_id,
                "max_hops": max_hops,
                "relations": sorted(relation.value for relation in relations),
            },
        )
        # 先物化轻量路径行；无边时立即返回，避免额外节点/边查询。
        rows = list(result)
        if not rows:
            return []

        # 批量加载所有唯一实体，避免为每条路径产生 N+1 查询。
        node_ids = {node_id for row in rows for node_id in row.node_ids}
        edge_ids = {edge_id for row in rows for edge_id in row.edge_ids}
        node_records = (
            await self._session.scalars(
                select(KnowledgeNodeRecord).where(KnowledgeNodeRecord.node_id.in_(node_ids))
            )
        ).all()
        edge_records = (
            await self._session.scalars(
                select(KnowledgeEdgeRecord).where(KnowledgeEdgeRecord.edge_id.in_(edge_ids))
            )
        ).all()
        # 映射表用于按 SQL 路径数组原顺序重建领域对象，保留方向和逐跳关系。
        nodes_by_id = {record.node_id: _node_from_record(record) for record in node_records}
        edges_by_id = {record.edge_id: _edge_from_record(record) for record in edge_records}

        # GraphPath 再次执行 Pydantic 校验，确保数据库查询结果满足一至两跳领域契约。
        paths: list[GraphPath] = []
        for row in rows:
            path_nodes = [nodes_by_id[node_id] for node_id in row.node_ids]
            path_edges = [edges_by_id[edge_id] for edge_id in row.edge_ids]
            source_ids = sorted({item.source_id for item in [*path_nodes, *path_edges]})
            paths.append(
                GraphPath(
                    path_id=_path_id(row.edge_ids),
                    nodes=path_nodes,
                    edges=path_edges,
                    depth=int(row.depth),
                    score=float(row.path_score),
                    source_ids=source_ids,
                )
            )
        return paths


def _node_from_mapping(mapping) -> KnowledgeNode:
    """把原生 SQL RowMapping 转换为受校验 KnowledgeNode，并规范化 pgvector 值。

    全文查询返回 mapping 而非 ORM Record；pgvector 可能提供专用序列对象，因此在非空时显式转成
    list 以满足可序列化领域模型。字段缺失或非法枚举会在 Pydantic 构造时显式失败。
    """

    embedding = mapping["embedding"]
    return KnowledgeNode(
        node_id=mapping["node_id"],
        node_type=mapping["node_type"],
        name=mapping["name"],
        content=mapping["content"],
        aliases=mapping["aliases"],
        source_id=mapping["source_id"],
        source_span=mapping["source_span"],
        reliability=mapping["reliability"],
        embedding=list(embedding) if embedding is not None else None,
    )


def _node_from_record(record: KnowledgeNodeRecord) -> KnowledgeNode:
    """把 SQLAlchemy 节点 Record 转换成与协议层无关的 Pydantic 领域节点。

    显式字段映射避免 ORM 内部状态泄漏到检索结果，并把可选 pgvector 转成普通列表；如果数据库
    被其他写入者污染，领域模型会在这里拒绝非法类型、ID 或可靠性。
    """

    return KnowledgeNode(
        node_id=record.node_id,
        node_type=record.node_type,
        name=record.name,
        content=record.content,
        aliases=record.aliases,
        source_id=record.source_id,
        source_span=record.source_span,
        reliability=record.reliability,
        embedding=list(record.embedding) if record.embedding is not None else None,
    )


def _edge_from_record(record: KnowledgeEdgeRecord) -> KnowledgeEdge:
    """把 SQLAlchemy 边 Record 显式转换成带来源和权重的领域关系。

    转换后服务层不再依赖数据库会话或 ORM lazy loading；Pydantic 会重新验证关系枚举、权重和
    标识格式，形成数据库约束之外的应用边界防线。
    """

    return KnowledgeEdge(
        edge_id=record.edge_id,
        from_node_id=record.from_node_id,
        to_node_id=record.to_node_id,
        relation_type=record.relation_type,
        weight=record.weight,
        source_id=record.source_id,
        source_span=record.source_span,
    )


def _path_id(edge_ids: list[str]) -> str:
    """根据有序 edge_id 序列生成可重放的短 SHA-256 图路径引用。

    边顺序决定路径方向，因此不排序；相同有序路径跨查询得到相同 ID，删边或换序则改变引用。
    16 位摘要适合小型作品集规模，不用于安全签名或跨系统全局唯一标识。
    """

    digest = sha256("|".join(edge_ids).encode("utf-8")).hexdigest()[:16]
    return f"path_{digest}"
