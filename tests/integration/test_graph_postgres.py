"""PostgreSQL/pgvector 图存储、路径扩展和删边消融集成测试。

该测试使用 postgres marker 与快速测试隔离。它真实检查 vector 扩展、Provider 溯源、cosine
召回、全文/向量混合评分和两跳路径，并在事务中删除关键边证明三组件链路依赖显式图关系。
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError

from app.persistence.database import create_database_engine, create_session_factory
from app.persistence.models import KnowledgeEdgeRecord
from app.retrieval.ablation import evaluate_graph_ablation, load_graph_ablation_cases
from app.retrieval.budget import build_evidence_bundle
from app.retrieval.embeddings import DeterministicHashEmbeddingProvider, embed_knowledge_bundle
from app.retrieval.models import EvidenceBundleBudget, RetrievalChannel, RetrievalMode
from app.retrieval.repository import PostgresGraphRepository
from app.retrieval.seeds import load_knowledge_seed
from app.retrieval.service import GraphRetrievalService

DATABASE_URL = os.getenv("DATAOPS_TEST_DATABASE_URL")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_graph_seed_search_expansion_and_key_edge_ablation() -> None:
    """验证真实 PostgreSQL 中从迁移能力、幂等种子到两跳路径和删边消融的闭环。

    测试先确认 pgvector 扩展，再用默认可替换 Provider 嵌入人工 Bundle；随后验证数据库 cosine
    查询、双路种子、混合分和 LTS→BDS→FlashSync 路径。最后在未提交事务中删除关键边并重查，
    若路径仍存在则说明实现没有真实依赖图关系。rollback 与 finally 保证隔离和资源释放。
    """

    if DATABASE_URL is None:
        pytest.fail("DATAOPS_TEST_DATABASE_URL is required for postgres tests")

    engine = create_database_engine(DATABASE_URL)
    factory = create_session_factory(engine)
    try:
        # 扩展检查证明迁移已真正启用 pgvector，而不是仅在 ORM 中声明 Vector 类型。
        async with engine.connect() as connection:
            vector_version = await connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
        assert vector_version

        async with factory() as session:
            repository = PostgresGraphRepository(session)
            embedding_provider = DeterministicHashEmbeddingProvider(dimensions=128)

            # 原始人工 JSON 保持无向量，入库前通过 Provider 批量生成并记录同一向量空间元数据。
            embedded_bundle = await embed_knowledge_bundle(
                load_knowledge_seed(Path("data/knowledge/cross_chain_graph.json")),
                embedding_provider,
            )
            await repository.upsert_seed_bundle(embedded_bundle)
            await session.commit()

            node_count, edge_count = await repository.count_graph()
            assert node_count == 11
            assert edge_count == 13
            assert (
                await repository.count_embedded_nodes(
                    provider_id=embedding_provider.provider_id,
                    dimensions=embedding_provider.dimensions,
                )
                == 11
            )

            # 直接绕过 Pydantic 篡改维度，确认数据库 CheckConstraint 仍能拒绝不兼容向量元数据。
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "UPDATE knowledge_nodes SET embedding_dimensions = 127 "
                        "WHERE node_id = 'component_lts'"
                    )
                )
            await session.rollback()

            # 直接调用向量仓储，证明排序由 PostgreSQL cosine distance 而非 Python 全表扫描完成。
            query_embedding = (await embedding_provider.embed_texts(["duplicate key"]))[0]
            vector_matches = await repository.search_vector_seeds(
                query_embedding,
                provider_id=embedding_provider.provider_id,
                limit=5,
            )
            assert any(
                match.node.node_id == "root_cause_primary_key_conflict" for match in vector_matches
            )
            assert all(match.embedding_dimensions == 128 for match in vector_matches)
            assert all(
                match.embedding_provider == embedding_provider.provider_id
                for match in vector_matches
            )
            assert all(match.node.embedding is None for match in vector_matches)
            assert (
                await repository.search_vector_seeds(
                    query_embedding,
                    provider_id="different-space:v1",
                    limit=5,
                )
                == []
            )

            # 从组件短标识符执行双路召回，并要求递归 CTE 返回方向正确的三组件两跳链。
            service = GraphRetrievalService(repository, embedding_provider)
            result = await service.retrieve("LTS", seed_limit=5, max_hops=2)
            lts_seed = next(seed for seed in result.seeds if seed.node.node_id == "component_lts")
            assert RetrievalChannel.LEXICAL in lts_seed.channels
            assert RetrievalChannel.VECTOR in lts_seed.channels
            assert lts_seed.semantic_score > 0
            assert lts_seed.lexical_score > 0
            component_path = next(
                path
                for path in result.paths
                if [node.node_id for node in path.nodes]
                == ["component_lts", "component_bds", "component_flashsync"]
            )
            assert component_path.depth == 2
            assert [edge.relation_type.value for edge in component_path.edges] == [
                "DEPENDS_ON",
                "DEPENDS_ON",
            ]
            assert component_path.path_id.startswith("path_")
            assert component_path.hybrid_score > component_path.score * result.score_weights.path
            assert component_path.seed_node_id == "component_lts"

            # 使用同一查询和预算运行 vector-only/vector+graph，结构化记录图扩展的实测增益。
            ablation_case = load_graph_ablation_cases(
                Path("data/evals/graphrag_ablation_cases.json")
            )[0]
            vector_only = await service.retrieve(
                ablation_case.query,
                seed_limit=ablation_case.seed_limit,
                max_hops=ablation_case.max_hops,
                mode=RetrievalMode.VECTOR_ONLY,
            )
            vector_graph = await service.retrieve(
                ablation_case.query,
                seed_limit=ablation_case.seed_limit,
                max_hops=ablation_case.max_hops,
                mode=RetrievalMode.VECTOR_GRAPH,
            )
            ablation_report = evaluate_graph_ablation(
                ablation_case,
                vector_only=vector_only,
                vector_graph=vector_graph,
            )
            assert vector_only.paths == []
            assert ablation_report.vector_graph.root_cause_hit is True
            assert ablation_report.root_cause_hit_delta >= 0
            assert ablation_report.vector_only.chain_completeness == 0
            assert ablation_report.vector_graph.chain_completeness == 1
            assert ablation_report.chain_completeness_delta == 1

            # Bundle 必须在默认预算中原子保留完整因果路径，并公开所有稳定证据引用。
            evidence_bundle = build_evidence_bundle(
                vector_graph,
                budget=EvidenceBundleBudget(),
            )
            causal_path = next(
                path
                for path in evidence_bundle.selected_paths
                if path.node_ids == ablation_case.required_path_node_ids
            )
            assert causal_path.evidence_id == causal_path.path_id
            assert evidence_bundle.used_bytes <= evidence_bundle.budget.max_bytes
            assert set(causal_path.node_ids) <= {
                node.node_id for node in evidence_bundle.selected_nodes
            }

            # 删除只在当前事务可见且稍后回滚，用消融证明路径不是提示词或硬编码结果。
            await session.execute(
                delete(KnowledgeEdgeRecord).where(
                    KnowledgeEdgeRecord.edge_id == "edge_bds_depends_flashsync"
                )
            )
            await session.flush()
            ablated = await service.retrieve("LTS", seed_limit=5, max_hops=2)
            assert not any(
                [node.node_id for node in path.nodes]
                == ["component_lts", "component_bds", "component_flashsync"]
                for path in ablated.paths
            )
            await session.rollback()
    finally:
        # 即使断言失败也关闭 asyncpg 池，防止后续测试因连接泄漏出现假故障。
        await engine.dispose()
