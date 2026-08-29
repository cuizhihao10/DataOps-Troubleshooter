"""为真实模型 Golden 评测预置历史案例，使四个记忆类指标拥有真实分母。

Golden 的记忆类案例要求召回若干 confirmed 历史案例。确定性评测由脚本替身直接合成
``CaseMemoryMatch``，而 live 模式必须走生产 confirmed-only 检索路径；数据库里没有对应案例时
``history_recall_coverage`` / ``confirmed_only_recall_rate`` / ``history_projection_pass_rate`` /
``realtime_priority_rate`` 会全部落到 0——那是"没测"，不是"模型没做到"。本模块在任何付费聊天调用
之前把 Golden 标注投影成真实数据库行：required 记忆经生产 confirm 事务变成 confirmed（因此同时注册
动态 case 图节点与 ``SIMILAR_TO`` 边），forbidden 记忆留在 pending 与 rejected，用来真正验证状态
过滤，而不是靠"向量刚好不相似"侥幸通过。

预置内容只来自案例的用户问题与 ``history_expectation`` 标注，不写入 ``allowed_root_causes``、
``required_tools``、必要证据来源、必要路径或停止原因。但 ``historical_root_cause`` 本身是评测输入的
一部分：非冲突案例的历史根因与本次正确根因相同，冲突案例故意不同。因此预置会改变记忆类案例的根因
指标口径——报告里的 ``history_seed`` 字段就是让读者看见这一点，不能把开了预置的运行与未开预置的
历史运行放在同一列比较。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import CaseMemory, Component, MemoryStatus
from app.domain.scenarios import GoldenCaseSpec
from app.memory.models import MemoryDecision, StoredCaseMemory
from app.memory.repository import PostgresCaseMemoryRepository
from app.memory.service import memory_embedding_text, memory_signature
from app.retrieval.embeddings import EmbeddingProvider

LIVE_HISTORY_SEED_SOURCE_RUN_ID = "run_live_golden_history_seed"
LIVE_HISTORY_SEED_TAG = "live_golden_history_seed"
# 签名参与 `find_exact` 精确去重，而 Golden 标注的多条历史案例完全可能共享同一组件与根因文本。
# 把 memory_id 混入签名输入，使预置行之间不会撞唯一约束，也不会与后续真实 staging 静默合并——
# 预置数据必须能被单独识别和整体清除，否则下一轮评测无法判断某条 confirmed 案例的来源。
LIVE_HISTORY_SEED_SIGNATURE_NAMESPACE = "live-golden-history-seed"


class SeedMemoryRepository(Protocol):
    """声明预置写入所需的唯一仓储方法，使插入逻辑可以在没有数据库时被验证。

    协议只暴露 ``insert``：预置不允许更新既有行或改状态（那两件事分别由"先删除"和生产决策路径
    负责），因此把可用能力限制到一个方法本身就是边界声明，而不只是为了方便测试注入替身。
    """

    async def insert(self, stored: StoredCaseMemory, *, source_run_id: str) -> None:
        """写入一条 pending 案例及其来源 run 证据关联，不提交事务。

        事务由调用方的 ``session_factory.begin()`` 上下文统一提交或回滚，因此任一行失败都不会留下
        半套预置历史；实现必须让唯一约束与向量维度校验在这里失败，而不是等到检索阶段。
        """

        ...


class SeededMemoryRuntime(Protocol):
    """声明预置流程需要的最小记忆 runtime 接口：删除与用户级 confirm/reject 决策。

    协议刻意只包含生产 API，使预置无法绕过状态机自己写 confirmed 行：confirm 必须经过与用户点击
    完全相同的事务（状态更新 + 动态图节点/边注册），否则"图通道能召回"这件事在 live 评测里就是
    未被验证的假设。测试可注入只记录调用的替身。
    """

    async def delete(self, memory_id: str) -> CaseMemory | None:
        """永久删除案例、证据关联与动态图节点，缺失时返回 ``None`` 保持幂等。

        预置在写入前先删除同 ID 旧行，使重复运行不会因主键或签名唯一约束失败，也不会把上一轮的
        occurrence、证据关联或图边继承到本轮，从而让"这一轮召回了什么"始终可归因到本轮预置。
        """

        ...

    async def decide(self, memory_id: str, decision: MemoryDecision) -> CaseMemory | None:
        """把 confirm/reject 决策应用到已存在案例并返回更新后的领域对象。

        返回 ``None`` 表示案例不存在，对预置来说属于基础设施失败而不是评分结果，调用方必须抛错
        而不是继续跑一轮"记忆指标恰好为 0"的付费评测。
        """

        ...


class LiveHistorySeedReport(BaseModel):
    """记录一次历史预置实际写入了哪些案例，作为记忆类指标的分母说明。

    三个 ID 元组按状态分开公开，读者因此能判断 forbidden 命中为 0 到底是"过滤有效"还是"根本没写
    进去"。向量空间同时记录 Provider ID 与维度：只有与本轮检索完全同空间的行才可能被 pgvector
    召回，空间不一致时召回为 0 与模型行为无关。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    confirmed_memory_ids: tuple[str, ...] = Field(default=())
    pending_memory_ids: tuple[str, ...] = Field(default=())
    rejected_memory_ids: tuple[str, ...] = Field(default=())
    embedding_provider: str = Field(min_length=1, max_length=100)
    embedding_dimensions: int = Field(ge=8, le=4096)


class _SeedRow(BaseModel):
    """描述一条待写入的预置案例及其目标状态，供批量嵌入与逐条决策复用。

    目标状态用 :class:`MemoryStatus` 表达而不是布尔标记，因为 forbidden 记忆需要覆盖 pending 与
    rejected 两种非 confirmed 情况；插入阶段一律先写 pending，再由生产决策路径转成目标状态。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory: CaseMemory
    target_status: MemoryStatus


def build_seed_rows(cases: Sequence[GoldenCaseSpec], *, now: datetime) -> tuple[_SeedRow, ...]:
    """把记忆类案例的 required/forbidden 标注投影为待写入的预置行。

    required 记忆逐条对应一行 confirmed 目标，症状取本案例用户问题、根因取标注的历史根因。
    forbidden ID 在多条案例间共享，因此按首次出现去重成一行，症状合并所有声明它的案例问题，
    组件取这些案例的并集——只有和查询足够相似，"非 confirmed 不得被召回"才是被真正验证过的门禁，
    而不是因为向量不相关而自动成立。非记忆类案例不产生任何预置行。
    """

    rows: list[_SeedRow] = []
    forbidden_symptoms: dict[str, list[str]] = {}
    forbidden_components: dict[str, list[Component]] = {}
    for index, case in enumerate(cases):
        if case.history_expectation is None:
            continue
        components = [Component(value) for value in case.requested_components]
        for offset, expectation in enumerate(case.history_expectation.required_memories):
            created = now - timedelta(days=index + offset + 3)
            rows.append(
                _SeedRow(
                    memory=CaseMemory(
                        memory_id=expectation.memory_id,
                        symptoms=[f"历史合成症状：{case.user_query}"],
                        root_cause=expectation.historical_root_cause,
                        fault_path=["历史合成故障路径（预置数据，不代表本次结论）"],
                        solution_steps=["仅在隔离环境人工复核历史方案。"],
                        components=components,
                        tags=[LIVE_HISTORY_SEED_TAG, case.scenario_id],
                        evidence_refs=[f"ev_seed_{expectation.memory_id}"],
                        status=MemoryStatus.PENDING,
                        occurrence_count=2,
                        created_at=created,
                        updated_at=created + timedelta(days=1),
                    ),
                    target_status=MemoryStatus.CONFIRMED,
                )
            )
        for memory_id in case.history_expectation.forbidden_memory_ids:
            forbidden_symptoms.setdefault(memory_id, []).append(f"历史合成症状：{case.user_query}")
            for component in components:
                if component not in forbidden_components.setdefault(memory_id, []):
                    forbidden_components[memory_id].append(component)

    # forbidden 行只需要"不是 confirmed"，pending 与 rejected 由声明顺序交替分配：两者被同一条 SQL
    # 状态过滤排除，具体落在哪一档不属于 Golden 契约，因此不从 ID 命名去猜，也不新增数据字段。
    for order, (memory_id, symptoms) in enumerate(forbidden_symptoms.items()):
        created = now - timedelta(days=order + 2)
        rows.append(
            _SeedRow(
                memory=CaseMemory(
                    memory_id=memory_id,
                    symptoms=list(dict.fromkeys(symptoms)),
                    root_cause="（合成）未经复核的历史结论，禁止进入本次诊断。",
                    fault_path=["历史合成故障路径（预置数据，不代表本次结论）"],
                    solution_steps=["该案例未通过复核，不提供任何处置建议。"],
                    components=forbidden_components[memory_id],
                    tags=[LIVE_HISTORY_SEED_TAG, "forbidden"],
                    evidence_refs=[f"ev_seed_{memory_id}"],
                    status=MemoryStatus.PENDING,
                    occurrence_count=1,
                    created_at=created,
                    updated_at=created,
                ),
                target_status=(
                    MemoryStatus.PENDING if order % 2 == 0 else MemoryStatus.REJECTED
                ),
            )
        )
    return tuple(rows)


async def seed_live_golden_history(
    cases: Sequence[GoldenCaseSpec],
    *,
    memory_runtime: SeededMemoryRuntime,
    session_factory: async_sessionmaker[AsyncSession],
    embedding_provider: EmbeddingProvider,
    now: datetime | None = None,
    repository_factory: Callable[[AsyncSession], SeedMemoryRepository] = (
        PostgresCaseMemoryRepository
    ),
) -> LiveHistorySeedReport:
    """在付费聊天调用之前把 Golden 历史标注写成真实数据库行并返回分母说明。

    流程固定为"先按 ID 删除旧行 → 单事务批量插入 pending → 逐条走生产决策转成目标状态"。删除在
    插入之前保证重复运行幂等；插入共享一个事务，任一行的向量、签名或约束失败都整批回滚，不会留下
    "只预置了一半历史"的数据库状态，因为那会让记忆召回率变成无法解释的中间值。所有失败都向上抛出
    并终止评测，绝不降级为一轮记忆指标为 0 的运行。
    """

    rows = build_seed_rows(cases, now=now or datetime.now(UTC))
    if not rows:
        return LiveHistorySeedReport(
            embedding_provider=embedding_provider.provider_id,
            embedding_dimensions=embedding_provider.dimensions,
        )

    texts = [memory_embedding_text(row.memory) for row in rows]
    vectors = await embedding_provider.embed_texts(texts)
    if len(vectors) != len(rows):
        raise RuntimeError("live history seed embedding provider returned mismatched vectors")

    for row in rows:
        await memory_runtime.delete(row.memory.memory_id)

    async with session_factory.begin() as session:
        repository = repository_factory(session)
        for row, vector in zip(rows, vectors, strict=True):
            signature = memory_signature(
                row.memory.components,
                f"{row.memory.root_cause}|{LIVE_HISTORY_SEED_SIGNATURE_NAMESPACE}"
                f":{row.memory.memory_id}",
            )
            await repository.insert(
                StoredCaseMemory(
                    memory=row.memory,
                    signature=signature,
                    embedding=vector,
                    embedding_provider=embedding_provider.provider_id,
                    embedding_dimensions=embedding_provider.dimensions,
                ),
                source_run_id=LIVE_HISTORY_SEED_SOURCE_RUN_ID,
            )

    confirmed: list[str] = []
    rejected: list[str] = []
    pending: list[str] = []
    for row in rows:
        memory_id = row.memory.memory_id
        if row.target_status is MemoryStatus.PENDING:
            pending.append(memory_id)
            continue
        decision = (
            MemoryDecision.CONFIRM
            if row.target_status is MemoryStatus.CONFIRMED
            else MemoryDecision.REJECT
        )
        decided = await memory_runtime.decide(memory_id, decision)
        if decided is None or decided.status is not row.target_status:
            raise RuntimeError(f"live history seed could not apply decision: {memory_id}")
        (confirmed if row.target_status is MemoryStatus.CONFIRMED else rejected).append(memory_id)

    return LiveHistorySeedReport(
        confirmed_memory_ids=tuple(confirmed),
        pending_memory_ids=tuple(pending),
        rejected_memory_ids=tuple(rejected),
        embedding_provider=embedding_provider.provider_id,
        embedding_dimensions=embedding_provider.dimensions,
    )
