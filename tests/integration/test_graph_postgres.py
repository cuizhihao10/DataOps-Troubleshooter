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
        # 动态 confirmed 案例与人工 seed 共用 knowledge_nodes；本专项只验证固定 seed 数量，因此先
        # 清理测试运行可能残留的 case 节点，外键级联会同步删除其 SIMILAR_TO 边。
        async with factory.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM knowledge_nodes "
                    "WHERE node_type = 'case' AND source_id LIKE 'mem_%'"
                )
            )

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
            assert node_count == 30
            assert edge_count == 35
            assert (
                await repository.count_embedded_nodes(
                    provider_id=embedding_provider.provider_id,
                    dimensions=embedding_provider.dimensions,
                )
                == 30
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

            # 精确症状查询应从新增 v2 节点出发，沿真实递归 CTE 得到两跳根因和解决方案，而不是
            # 仅因三个关键词分别出现在种子结果中就被误判为 GraphRAG 路径。
            parameter_result = await service.retrieve(
                "LTS 参数校验失败 partition_date",
                seed_limit=5,
                max_hops=2,
            )
            parameter_path = next(
                path
                for path in parameter_result.paths
                if [node.node_id for node in path.nodes]
                == [
                    "symptom_lts_parameter_validation_failure",
                    "root_cause_lts_invalid_partition_parameter",
                    "solution_validate_lts_runtime_parameters",
                ]
            )
            assert [edge.relation_type.value for edge in parameter_path.edges] == [
                "CAUSED_BY",
                "RESOLVED_BY",
            ]
            assert parameter_path.depth == 2

            # v3 的 BDS 查询必须得到另一条独立两跳路径；使用节点序列和关系序列双重断言，避免
            # LTS v2 路径通过后掩盖新边没有真正写入 PostgreSQL 的问题。
            skew_result = await service.retrieve(
                "BDS 执行阶段长尾 数据倾斜",
                seed_limit=5,
                max_hops=2,
            )
            skew_path = next(
                path
                for path in skew_result.paths
                if [node.node_id for node in path.nodes]
                == [
                    "symptom_bds_long_tail_stage",
                    "root_cause_bds_data_skew",
                    "solution_rebalance_bds_skew",
                ]
            )
            assert [edge.relation_type.value for edge in skew_path.edges] == [
                "CAUSED_BY",
                "RESOLVED_BY",
            ]
            assert skew_path.depth == 2

            # v4 的 FlashSync 查询验证高风险恢复知识也通过同一 pgvector/递归 CTE 数据路径；这里
            # 只证明路径存在，是否允许执行恢复仍由报告风险与人工变更流程控制。
            checkpoint_result = await service.retrieve(
                "FlashSync 检查点落后 位点回退",
                seed_limit=5,
                max_hops=2,
            )
            checkpoint_path = next(
                path
                for path in checkpoint_result.paths
                if [node.node_id for node in path.nodes]
                == [
                    "symptom_flashsync_checkpoint_lag",
                    "root_cause_flashsync_checkpoint_regression",
                    "solution_validate_flashsync_checkpoint_restore",
                ]
            )
            assert [edge.relation_type.value for edge in checkpoint_path.edges] == [
                "CAUSED_BY",
                "RESOLVED_BY",
            ]
            assert checkpoint_path.depth == 2

            # v5 查询要求 Schema 故障也形成独立两跳路径；节点和关系双重匹配避免仅凭错误码文本
            # 命中根因节点就误称已经得到映射验证方案。
            schema_result = await service.retrieve(
                "FlashSync Schema 记录拒绝 字段映射滞后",
                seed_limit=5,
                max_hops=2,
            )
            schema_path = next(
                path
                for path in schema_result.paths
                if [node.node_id for node in path.nodes]
                == [
                    "symptom_flashsync_schema_rejection",
                    "root_cause_flashsync_schema_mapping_outdated",
                    "solution_validate_flashsync_schema_mapping",
                ]
            )
            assert [edge.relation_type.value for edge in schema_path.edges] == [
                "CAUSED_BY",
                "RESOLVED_BY",
            ]
            assert schema_path.depth == 2

            # v6 先以客户画像 LTS 任务作精确种子，要求递归 CTE 沿两条真实依赖边抵达 FlashSync。
            # 该断言与通用组件链不同，证明新场景的任务身份和方向并非只存在于 Fixture 文本中。
            customer_task_result = await service.retrieve(
                "dws_customer_profile_daily",
                seed_limit=5,
                max_hops=2,
            )
            customer_task_path = next(
                path
                for path in customer_task_result.paths
                if [node.node_id for node in path.nodes]
                == [
                    "task_lts_customer_profile_report",
                    "task_bds_customer_profile_aggregate",
                    "task_flashsync_customer_profile_delta",
                ]
            )
            assert [edge.relation_type.value for edge in customer_task_path.edges] == [
                "DEPENDS_ON",
                "DEPENDS_ON",
            ]
            assert customer_task_path.depth == 2

            # 再从 FlashSync 任务出发验证 MANIFESTS_AS→CAUSED_BY，确保跨组件任务链能接入 v5
            # Schema 根因，而不是形成一个与故障知识断开的平行子图。
            customer_schema_result = await service.retrieve(
                "flashsync_customer_profile_delta",
                seed_limit=5,
                max_hops=2,
            )
            customer_schema_path = next(
                path
                for path in customer_schema_result.paths
                if [node.node_id for node in path.nodes]
                == [
                    "task_flashsync_customer_profile_delta",
                    "symptom_flashsync_schema_rejection",
                    "root_cause_flashsync_schema_mapping_outdated",
                ]
            )
            assert [edge.relation_type.value for edge in customer_schema_path.edges] == [
                "MANIFESTS_AS",
                "CAUSED_BY",
            ]
            assert customer_schema_path.depth == 2

            # v7 从 BDS 客户状态任务出发，必须沿 DEPENDS_ON→PRODUCES 到达同步数据集；该路径直接
            # 对应 Golden 的 BDS→FlashSync 交付链，不能由通用组件边或相似文本替代。
            customer_status_result = await service.retrieve(
                "bds_customer_status_snapshot_hourly",
                seed_limit=5,
                max_hops=2,
            )
            customer_status_path = next(
                path
                for path in customer_status_result.paths
                if [node.node_id for node in path.nodes]
                == [
                    "task_bds_customer_status_snapshot",
                    "task_flashsync_customer_status_delta",
                    "dataset_ods_customer_status_delta",
                ]
            )
            assert [edge.relation_type.value for edge in customer_status_path.edges] == [
                "DEPENDS_ON",
                "PRODUCES",
            ]
            assert customer_status_path.depth == 2

            # 从同步任务再验证 MANIFESTS_AS→CAUSED_BY，使 v7 任务拓扑接入 v4 检查点根因；如果只
            # 新增任务和数据集而缺这条连接，跨组件报告无法用图解释输入缺失的直接原因。
            checkpoint_manifest_result = await service.retrieve(
                "flashsync_customer_status_delta",
                seed_limit=5,
                max_hops=2,
            )
            checkpoint_manifest_path = next(
                path
                for path in checkpoint_manifest_result.paths
                if [node.node_id for node in path.nodes]
                == [
                    "task_flashsync_customer_status_delta",
                    "symptom_flashsync_checkpoint_lag",
                    "root_cause_flashsync_checkpoint_regression",
                ]
            )
            assert [edge.relation_type.value for edge in checkpoint_manifest_path.edges] == [
                "MANIFESTS_AS",
                "CAUSED_BY",
            ]
            assert checkpoint_manifest_path.depth == 2

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
            # 固定 Provider/seed/预算下锁定当前实测快照，使文档中的字节数和省略数量不能在图扩展后
            # 静默漂移；若知识内容合理变化，应重跑本测试并同步解释新候选排序，而不是放宽断言。
            assert evidence_bundle.used_bytes == 5881
            assert len(evidence_bundle.selected_nodes) == 8
            assert len(evidence_bundle.selected_paths) == 4
            assert len(evidence_bundle.omitted_path_ids) == 6
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
