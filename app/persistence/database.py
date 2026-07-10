"""异步 SQLAlchemy 引擎与会话工厂。

统一工厂确保所有数据库 I/O 使用 asyncpg 和异步会话，避免同时维护同步驱动。连接检查
只执行 SELECT 1，不泄露 URL；调用方负责在生命周期结束时显式 dispose 引擎。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_database_engine(database_url: str) -> AsyncEngine:
    """根据已解密连接串创建 asyncpg SQLAlchemy 引擎，并启用借出前连接探活。

    本函数只构造连接池，不立即访问数据库；`pool_pre_ping` 可在容器重启或空闲连接失效后先执行
    轻量探测，减少把断连暴露为业务查询错误。调用方拥有引擎生命周期并必须在停机时 dispose，
    连接串不得写入日志或健康响应。
    """

    return create_async_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """从共享异步引擎创建类型化 AsyncSession 工厂。

    `expire_on_commit=False` 让提交后已加载字段仍可用于响应组装，避免离开事务后触发隐式异步查询；
    工厂本身不打开连接，仓储应按请求或任务创建短生命周期会话并显式提交写操作。
    """

    return async_sessionmaker(engine, expire_on_commit=False)


async def check_database_connection(engine: AsyncEngine) -> None:
    """执行不读取业务表的 `SELECT 1`，验证引擎可建立并使用数据库连接。

    该检查用于 FastAPI lifespan：认证、网络或数据库不可用时让应用启动失败；它不验证迁移和种子
    完整性，后续图计数查询承担那部分检查。SQLAlchemy 异常原样传播，且不会暴露连接 URL。
    """

    # context manager 确保探活完成或失败后连接都归还池中，不为健康检查泄漏资源。
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """以异步生成器形式提供一个自动关闭、但不自动提交的会话作用域。

    调用方显式决定 commit 或 rollback，避免通用基础设施把部分失败写入数据库；离开上下文时
    AsyncSession 会释放连接，未提交事务由 SQLAlchemy 回滚。该辅助函数适合依赖注入，不吞异常。
    """

    # 只管理资源生命周期，不隐式提交；事务语义必须留在了解业务原子性的调用层。
    async with factory() as session:
        yield session
