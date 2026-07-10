"""PostgreSQL/pgvector 图存储、路径扩展和删边消融集成测试。

该测试使用 postgres marker 与快速测试隔离。它真实检查 vector 扩展、幂等种子、全文
召回和两跳路径，并在事务中删除关键边证明三组件链路依赖显式图关系。
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import delete, text

from app.persistence.database import create_database_engine, create_session_factory
from app.persistence.models import KnowledgeEdgeRecord
from app.retrieval.repository import PostgresGraphRepository
from app.retrieval.seeds import load_knowledge_seed
from app.retrieval.service import GraphRetrievalService

DATABASE_URL = os.getenv("DATAOPS_TEST_DATABASE_URL")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_graph_seed_search_expansion_and_key_edge_ablation() -> None:
    """验证真实 PostgreSQL 中从迁移能力、幂等种子到两跳路径和删边消融的闭环。

    测试先确认 pgvector 扩展，再写入人工 Bundle 并检查固定规模；随后从 LTS lexical seed 扩展出
    LTS→BDS→FlashSync 路径。最后在未提交事务中删除关键边并重查，若路径仍存在则说明实现没有
    真实依赖图关系。rollback 与 finally 保证测试不永久改变种子且始终释放连接池。
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

            # 重复部署依赖 upsert 幂等；提交后计数验证数据库实际持久化的完整图规模。
            await repository.upsert_seed_bundle(
                load_knowledge_seed(Path("data/knowledge/cross_chain_graph.json"))
            )
            await session.commit()

            node_count, edge_count = await repository.count_graph()
            assert node_count == 11
            assert edge_count == 13

            # 从组件短标识符召回种子，并要求递归 CTE 返回方向和关系都正确的两跳链。
            service = GraphRetrievalService(repository)
            result = await service.retrieve("LTS", seed_limit=5, max_hops=2)
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
