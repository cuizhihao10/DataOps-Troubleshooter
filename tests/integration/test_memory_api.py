"""验证长期记忆 API 的数据库禁用、confirm/reject Schema 和 confirmed 搜索响应。

测试进入真实 FastAPI lifespan 完成 Fixture/MCP 审计；无数据库路径验证 503，随后注入满足 runtime
协议的强类型替身验证 HTTP 路由，不伪造 PostgreSQL SQL 行为。
"""

from datetime import UTC, datetime

import httpx
import pytest

from app.api.main import app
from app.domain.models import CaseMemory, Component, MemoryStatus
from app.memory.models import (
    CaseMemoryMatch,
    MemoryCounts,
    MemoryDecision,
    MemoryRetrievalChannel,
)

NOW = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)


class FakeMemoryRuntime:
    """提供 API 路由需要的 decide/search/counts 方法并记录调用。

    替身只保存一个合成案例，不生成 embedding 或访问数据库；状态切换使用 CaseMemory Pydantic 对象，
    search 仍只在 confirmed 时返回，模拟生产 SQL 过滤。
    """

    def __init__(self) -> None:
        """初始化 pending 合成案例和空搜索/决策调用记录。

        时间带 UTC，列表字段满足生产领域约束；实例由单个测试独占。构造函数不访问数据库或
        Embedding Provider，因此失败只可能来自领域模型校验，并会直接暴露测试夹具不合法的问题。
        """

        self.memory = CaseMemory(
            memory_id="mem_api_001",
            symptoms=["LTS 任务等待上游"],
            root_cause="上游数据未按时就绪",
            components=[Component.LTS],
            evidence_refs=["ev_api_memory_001"],
            status=MemoryStatus.PENDING,
            occurrence_count=1,
            created_at=NOW,
            updated_at=NOW,
        )
        self.decisions: list[tuple[str, MemoryDecision]] = []
        self.searches: list[tuple[str, int | None]] = []

    async def decide(
        self,
        memory_id: str,
        decision: MemoryDecision,
    ) -> CaseMemory | None:
        """记录决策并切换合成案例状态，未知 ID 返回 None。

        confirm/reject 映射与生产 Service 一致；方法不允许恢复 pending，也不修改 occurrence。
        """

        self.decisions.append((memory_id, decision))
        if memory_id != self.memory.memory_id:
            return None
        status = (
            MemoryStatus.CONFIRMED if decision is MemoryDecision.CONFIRM else MemoryStatus.REJECTED
        )
        self.memory = self.memory.model_copy(update={"status": status})
        return self.memory

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> list[CaseMemoryMatch]:
        """记录查询并仅在当前案例 confirmed 时返回一个固定相似度命中。

        limit 只用于断言 API 透传；替身不实现排序或 pgvector，这些由 PostgreSQL 专项测试覆盖。
        """

        self.searches.append((query, limit))
        if self.memory.status is not MemoryStatus.CONFIRMED:
            return []
        return [
            CaseMemoryMatch(
                memory=self.memory,
                similarity=0.91,
                retrieval_channels=[MemoryRetrievalChannel.VECTOR],
                direct_similarity=0.91,
            )
        ]

    async def counts(self) -> MemoryCounts:
        """根据当前单案例状态返回三类计数，供决策路由刷新健康快照。

        结果不读取 app.state，避免测试形成循环依赖。返回值严格复用生产 ``MemoryCounts``；若测试
        把案例状态改成领域枚举之外的值，模型构造应立即失败，而不是让健康断言得到伪造计数。
        """

        return MemoryCounts(
            pending=int(self.memory.status is MemoryStatus.PENDING),
            confirmed=int(self.memory.status is MemoryStatus.CONFIRMED),
            rejected=int(self.memory.status is MemoryStatus.REJECTED),
        )

    async def delete(self, memory_id: str) -> CaseMemory | None:
        """模拟事务删除并返回删除前快照，覆盖 DELETE API 的 404/200 映射。

        替身把当前内存对象替换为脱敏占位，避免测试误以为客户端仍能读取已删除
        embedding；真实主表、证据外键和图节点删除由 PostgreSQL 集成测试覆盖。
        """

        if memory_id != self.memory.memory_id:
            return None
        deleted = self.memory
        self.memory = self.memory.model_copy(update={"memory_id": "mem_deleted"})
        return deleted


@pytest.mark.asyncio
async def test_memory_api_returns_503_when_postgres_is_disabled() -> None:
    """验证默认无数据库 lifespan 下，决策与搜索均明确返回 503。

    该行为区别于“搜索成功但没有案例”，防止演示者误以为长期记忆已启用；响应不泄露数据库 URL。
    """

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            decision = await client.post(
                "/api/v1/memories/mem_missing/confirm",
                json={"decision": "confirm"},
            )
            search = await client.get(
                "/api/v1/memories/search",
                params={"query": "上游未就绪"},
            )

    assert decision.status_code == 503
    assert search.status_code == 503
    assert "configured PostgreSQL" in decision.json()["detail"]


@pytest.mark.asyncio
async def test_memory_api_confirms_rejects_and_searches_only_visible_case() -> None:
    """验证产品路径可确认、搜索、拒绝并立即改变默认召回可见性。

    先注入 pending runtime，confirm 后搜索返回 confirmed 案例和相似度；reject 后搜索为空。响应均
    携带 `case-memory:v2` 和向量/图评分来源，且 API 把 query/limit 原样传给 runtime。
    """

    runtime = FakeMemoryRuntime()
    async with app.router.lifespan_context(app):
        app.state.memory_runtime = runtime
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            confirmed = await client.post(
                "/api/v1/memories/mem_api_001/confirm",
                json={"decision": "confirm"},
            )
            visible = await client.get(
                "/api/v1/memories/search",
                params={"query": "上游未就绪", "limit": 3},
            )
            rejected = await client.post(
                "/api/v1/memories/mem_api_001/confirm",
                json={"decision": "reject"},
            )
            hidden = await client.get(
                "/api/v1/memories/search",
                params={"query": "上游未就绪", "limit": 3},
            )

    assert confirmed.status_code == 200
    assert confirmed.json()["memory"]["status"] == "confirmed"
    assert visible.status_code == 200
    assert visible.json()["contract_id"] == "case-memory:v2"
    assert visible.json()["matches"][0]["memory"]["memory_id"] == "mem_api_001"
    assert visible.json()["matches"][0]["similarity"] == 0.91
    assert visible.json()["matches"][0]["retrieval_channels"] == ["vector"]
    assert visible.json()["matches"][0]["direct_similarity"] == 0.91
    assert visible.json()["matches"][0]["graph_score"] is None
    assert visible.json()["matches"][0]["graph_edge_refs"] == []
    assert rejected.status_code == 200
    assert rejected.json()["memory"]["status"] == "rejected"
    assert hidden.json()["matches"] == []
    assert runtime.searches == [("上游未就绪", 3), ("上游未就绪", 3)]


@pytest.mark.asyncio
async def test_memory_search_rejects_whitespace_query_before_runtime_call() -> None:
    """验证 HTTP 参数层把纯空白查询拒绝为 422，且不会调用 Embedding/数据库 runtime。

    FastAPI 的正则约束要求至少一个非空白字符；该测试防止领域层 ``ValueError`` 穿透为 500，
    同时通过空调用记录证明无效公开输入不会消耗 Provider 或数据库资源。
    """

    runtime = FakeMemoryRuntime()
    async with app.router.lifespan_context(app):
        app.state.memory_runtime = runtime
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/memories/search",
                params={"query": "   "},
            )

    assert response.status_code == 422
    assert runtime.searches == []


@pytest.mark.asyncio
async def test_memory_delete_route_returns_safe_deletion_contract() -> None:
    """验证永久删除只返回 memory_id/deleted，不回显 embedding 或 ORM 字段。

    成功请求必须返回 case-memory contract 与布尔结果，未知 ID 必须是 404；响应
    文本不能包含向量或持久化实现细节，保持前端契约最小且可审计。
    """

    runtime = FakeMemoryRuntime()
    async with app.router.lifespan_context(app):
        app.state.memory_runtime = runtime
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            deleted = await client.delete("/api/v1/memories/mem_api_001")
            missing = await client.delete("/api/v1/memories/missing")

    assert deleted.status_code == 200
    assert deleted.json() == {
        "contract_id": "case-memory:v2",
        "memory_id": "mem_api_001",
        "deleted": True,
    }
    assert missing.status_code == 404
    assert "embedding" not in deleted.text
