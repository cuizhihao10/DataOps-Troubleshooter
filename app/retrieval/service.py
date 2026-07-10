"""将种子召回与白名单关系扩展组合为首版 GraphRAG 结果。

服务不生成自然语言答案，只编排仓储查询、去重路径并应用跳数预算。后续语义检索会在
此层合并全文和向量种子，而递归图仓储无需了解 Embedding Provider。
"""

from app.retrieval.models import GraphPath, GraphRetrievalResult, KnowledgeRelationType
from app.retrieval.repository import PostgresGraphRepository


class GraphRetrievalService:
    """编排 lexical seed 召回、批准关系扩展、路径去重与确定性排序。

    服务层隔离“检索策略”和“SQL 实现”：仓储负责查询，服务决定哪些关系可进入当前诊断上下文。
    它返回结构化 GraphRetrievalResult，不生成自然语言结论，也不把知识路径覆盖实时工具观察。
    """

    def __init__(self, repository: PostgresGraphRepository) -> None:
        """注入 PostgreSQL 图仓储，保持服务本身无数据库会话生命周期职责。

        依赖注入便于测试替换仓储并验证编排、白名单和排序；事务、连接关闭及 SQL 异常由调用方与
        仓储边界管理，本构造器不执行 I/O。
        """

        self._repository = repository

    async def retrieve(
        self,
        query: str,
        *,
        seed_limit: int = 5,
        max_hops: int = 2,
    ) -> GraphRetrievalResult:
        """按查询召回种子，为每个种子扩图，按 path_id 去重后返回排序结果。

        seed_limit 与 max_hops 由仓储再次校验；关系白名单排除首版不需要的 SIMILAR_TO，减少无关
        扩展。多个种子可能到达同一路径，字典去重后按更深路径、较高分数和稳定 ID 排序，便于
        跨组件链优先展示和测试重放。无命中返回空集合，不伪造证据。
        """

        # 当前切片仅有 lexical 路径；未来向量种子会在此合并，而不改动递归图仓储。
        seeds = await self._repository.search_lexical_seeds(query, limit=seed_limit)

        # 只允许能表达执行、依赖、症状、根因和解决链路的关系进入诊断上下文。
        allowed_relations = {
            KnowledgeRelationType.DEPENDS_ON,
            KnowledgeRelationType.CAUSED_BY,
            KnowledgeRelationType.MANIFESTS_AS,
            KnowledgeRelationType.RESOLVED_BY,
            KnowledgeRelationType.RUNS_ON,
            KnowledgeRelationType.PRODUCES,
            KnowledgeRelationType.CONSUMES,
        }
        # 不同种子可能发现同一有序边路径，稳定 path_id 是跨种子去重键。
        paths_by_id: dict[str, GraphPath] = {}
        for seed in seeds:
            paths = await self._repository.expand_paths(
                seed.node.node_id,
                max_hops=max_hops,
                allowed_relations=allowed_relations,
            )
            for path in paths:
                paths_by_id[path.path_id] = path

        # 深路径优先展示跨组件链，分数与稳定 ID 提供其后的确定性排序。
        return GraphRetrievalResult(
            query=query,
            seeds=seeds,
            paths=sorted(
                paths_by_id.values(),
                key=lambda path: (-path.depth, -path.score, path.path_id),
            ),
        )
