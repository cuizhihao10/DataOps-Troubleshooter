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
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_seed_bundle(self, bundle: KnowledgeSeedBundle) -> None:
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
            statement = insert(KnowledgeNodeRecord).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[KnowledgeNodeRecord.node_id],
                set_={**values, "updated_at": func.now()},
            )
            await self._session.execute(statement)

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
        if not query.strip():
            raise ValueError("query must not be blank")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")

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
        result = await self._session.execute(
            statement,
            {
                "query": query,
                "pattern": f"%{query}%",
                "limit": limit,
            },
        )
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
        if max_hops not in {1, 2}:
            raise ValueError("max_hops must be 1 or 2")
        relations = allowed_relations or set(KnowledgeRelationType)
        if not relations:
            return []

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
        result = await self._session.execute(
            statement,
            {
                "seed_node_id": seed_node_id,
                "max_hops": max_hops,
                "relations": sorted(relation.value for relation in relations),
            },
        )
        rows = list(result)
        if not rows:
            return []

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
        nodes_by_id = {record.node_id: _node_from_record(record) for record in node_records}
        edges_by_id = {record.edge_id: _edge_from_record(record) for record in edge_records}

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
    digest = sha256("|".join(edge_ids).encode("utf-8")).hexdigest()[:16]
    return f"path_{digest}"
