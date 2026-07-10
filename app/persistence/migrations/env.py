"""Alembic 异步迁移运行环境。

迁移 URL 只从 pydantic-settings 的 SecretStr 获取，不写入 alembic.ini。在线模式通过
异步引擎运行迁移，离线模式保留生成 SQL 的能力，两种模式共享 ORM metadata。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.settings import get_settings
from app.persistence.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """在不建立数据库连接的情况下配置 Alembic 生成带字面量的迁移 SQL。

    连接 URL 仍从 SecretStr 配置读取而不写入 ini；`literal_binds` 便于审阅部署 SQL，
    `compare_type` 让类型漂移可被检测。缺少 URL 时显式失败，事务上下文保证整段生成过程一致。
    """

    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("DATAOPS_DATABASE_URL is required for migrations")

    # 离线模式只把 URL 交给 Alembic 方言生成 SQL，不创建 asyncpg 网络连接。
    context.configure(
        url=settings.database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection) -> None:
    """在 Alembic 提供的同步连接桥接层中配置 metadata 并执行迁移事务。

    SQLAlchemy 的 `run_sync` 会把异步连接安全暴露给 Alembic 同步 API；该函数不创建或关闭引擎，
    只在调用方已拥有的连接上运行。异常传播给在线入口，使容器启动中止并由事务回滚。
    """

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """创建无连接池异步引擎，通过同步桥执行迁移，并在结束后释放资源。

    迁移是一次性管理任务，使用 NullPool 避免为短进程保留空闲连接；URL 缺失立即失败。连接内通过
    `run_sync` 调用 Alembic，同步迁移异常向上传播，finally 式顺序确保正常路径完成后引擎关闭。
    """

    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("DATAOPS_DATABASE_URL is required for migrations")
    # 迁移进程无需长期池化，NullPool 让每个连接在使用后立即关闭。
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        poolclass=pool.NullPool,
    )
    try:
        async with engine.connect() as connection:
            # Alembic API 是同步的，run_sync 在同一异步连接上下文中完成安全桥接。
            await connection.run_sync(_run_migrations)
    finally:
        # 即使迁移 SQL 失败也释放引擎，避免测试或容器重试遗留连接。
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
