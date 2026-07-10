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
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("DATAOPS_DATABASE_URL is required to seed knowledge data")

    bundle = load_knowledge_seed(settings.knowledge_seed_file)
    engine = create_database_engine(settings.database_url.get_secret_value())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            repository = PostgresGraphRepository(session)
            await repository.upsert_seed_bundle(bundle)
            await session.commit()
            return await repository.count_graph()
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(seed_database())


if __name__ == "__main__":
    main()
