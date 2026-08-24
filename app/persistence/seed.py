"""人工知识种子与运维文档语料的幂等数据库写入入口。

容器在迁移后、API 启动前执行本模块。节点、边和文档使用 upsert，因此重复启动不会生成重复
记录；任何 Schema 或外键错误都会中止启动，而不是静默跳过坏数据。知识图与文档语料在同一个
事务里提交，避免出现"图已就绪但文档缺失"的中间状态——那种状态下检索仍会返回结果，只是永远
少了一条通道，而这在演示时几乎不可能被发现。
"""

from __future__ import annotations

import asyncio

from app.core.settings import get_settings
from app.persistence.database import create_database_engine, create_session_factory
from app.retrieval.document_repository import PostgresDocumentRepository
from app.retrieval.document_seeds import load_document_library
from app.retrieval.embeddings import (
    create_embedding_provider,
    embed_document_library,
    embed_knowledge_bundle,
)
from app.retrieval.repository import PostgresGraphRepository
from app.retrieval.seeds import load_knowledge_seed


async def seed_database() -> tuple[int, int, int, int]:
    """加载知识 Bundle 与文档语料，在单事务中幂等 upsert，并返回节点/边/文档/切片数量。

    数据库 URL 缺失时立即失败；JSON 与 Markdown 先通过领域 Schema 与确定性切片，再创建异步引擎和
    仓储。四类记录全部写入成功后才提交，任何校验、外键或 SQL 错误都会让会话回滚并阻止 API 启动。
    finally 始终释放连接池，返回计数用于容器日志和健康验证而不暴露知识正文。
    """

    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("DATAOPS_DATABASE_URL is required to seed knowledge data")

    # 在连接数据库前完成文件与图引用校验，让坏种子以更清晰、低成本的错误提前失败。
    bundle = load_knowledge_seed(settings.knowledge_seed_file)
    library = load_document_library(settings.document_manifest_file)
    embedding_provider = create_embedding_provider(
        settings.embedding_provider,
        dimensions=settings.embedding_dimensions,
        model=settings.embedding_model,
        base_url=str(settings.embedding_base_url),
        api_key=settings.embedding_api_key,
        timeout_seconds=settings.embedding_timeout_seconds,
        batch_size=settings.embedding_batch_size,
    )

    # 原始 JSON/Markdown 保持人工可审查且不固化某个模型向量；启动时按当前 Provider 批量生成
    # 并标记向量空间。
    embedded_bundle = await embed_knowledge_bundle(bundle, embedding_provider)
    embedded_library = await embed_document_library(library, embedding_provider)
    # 远程 Provider 持有 httpx 连接池；种子是短进程，向量生成完成后立刻释放而不是等解释器退出。
    if hasattr(embedding_provider, "aclose"):
        await embedding_provider.aclose()
    engine = create_database_engine(settings.database_url.get_secret_value())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            repository = PostgresGraphRepository(session)
            document_repository = PostgresDocumentRepository(session)

            # 节点必须先于边写入，整个 Bundle 只提交一次以保证图结构原子可见。
            await repository.upsert_seed_bundle(embedded_bundle)
            await document_repository.upsert_document_library(embedded_library)
            await session.commit()

            # 提交后计数验证数据库实际状态，而不是简单回报输入文件中的元素数量。
            node_count, edge_count = await repository.count_graph()
            document_count, chunk_count = await document_repository.count_documents()
            return node_count, edge_count, document_count, chunk_count
    finally:
        # 命令行脚本是短进程，显式 dispose 可让失败路径也干净关闭 asyncpg 连接。
        await engine.dispose()


def main() -> None:
    """把异步种子流程桥接为 `python -m app.persistence.seed` 命令行入口。

    `asyncio.run` 为短生命周期脚本创建并关闭事件循环；任何异常保持非零退出，让 Docker 启动链
    在迁移或种子失败时停止，而不是继续启动一个知识图不完整的 API。
    """

    asyncio.run(seed_database())


if __name__ == "__main__":
    main()
