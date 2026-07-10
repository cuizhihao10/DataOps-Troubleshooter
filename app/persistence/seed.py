"""人工知识种子的幂等数据库写入入口。

容器在迁移后、API 启动前执行本模块。节点和边使用 upsert，因此重复启动不会生成重复
记录；任何 Schema 或外键错误都会中止启动，而不是静默跳过坏数据。
"""

from __future__ import annotations

import asyncio

from app.core.settings import get_settings
from app.persistence.database import create_database_engine, create_session_factory
from app.retrieval.repository import PostgresGraphRepository
from app.retrieval.seeds import load_knowledge_seed


async def seed_database() -> tuple[int, int]:
    """加载人工知识 Bundle，在单事务中幂等 upsert，并返回最终节点/边数量。

    数据库 URL 缺失时立即失败；JSON 先通过领域 Schema，再创建异步引擎和仓储。节点与边全部写入
    成功后才提交，任何校验、外键或 SQL 错误都会让会话回滚并阻止 API 启动。finally 始终释放
    连接池，返回计数用于容器日志和健康验证而不暴露知识正文。
    """

    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("DATAOPS_DATABASE_URL is required to seed knowledge data")

    # 在连接数据库前完成文件与图引用校验，让坏种子以更清晰、低成本的错误提前失败。
    bundle = load_knowledge_seed(settings.knowledge_seed_file)
    engine = create_database_engine(settings.database_url.get_secret_value())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            repository = PostgresGraphRepository(session)

            # 节点必须先于边写入，整个 Bundle 只提交一次以保证图结构原子可见。
            await repository.upsert_seed_bundle(bundle)
            await session.commit()

            # 提交后计数验证数据库实际状态，而不是简单回报输入文件中的元素数量。
            return await repository.count_graph()
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
