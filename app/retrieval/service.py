"""将种子召回与白名单关系扩展组合为首版 GraphRAG 结果。

服务不生成自然语言答案，只编排仓储查询、去重路径并应用跳数预算。后续语义检索会在
此层合并全文和向量种子，而递归图仓储无需了解 Embedding Provider。
"""

from app.retrieval.models import GraphRetrievalResult, KnowledgeRelationType
from app.retrieval.repository import PostgresGraphRepository


class GraphRetrievalService:
    def __init__(self, repository: PostgresGraphRepository) -> None:
        self._repository = repository

    async def retrieve(
        self,
        query: str,
        *,
        seed_limit: int = 5,
        max_hops: int = 2,
    ) -> GraphRetrievalResult:
        seeds = await self._repository.search_lexical_seeds(query, limit=seed_limit)
        allowed_relations = {
            KnowledgeRelationType.DEPENDS_ON,
            KnowledgeRelationType.CAUSED_BY,
            KnowledgeRelationType.MANIFESTS_AS,
            KnowledgeRelationType.RESOLVED_BY,
            KnowledgeRelationType.RUNS_ON,
            KnowledgeRelationType.PRODUCES,
            KnowledgeRelationType.CONSUMES,
        }
        paths_by_id = {}
        for seed in seeds:
            paths = await self._repository.expand_paths(
                seed.node.node_id,
                max_hops=max_hops,
                allowed_relations=allowed_relations,
            )
            for path in paths:
                paths_by_id[path.path_id] = path

        return GraphRetrievalResult(
            query=query,
            seeds=seeds,
            paths=sorted(
                paths_by_id.values(),
                key=lambda path: (-path.depth, -path.score, path.path_id),
            ),
        )
