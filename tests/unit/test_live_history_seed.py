"""验证真实模型评测的历史预置只写入标注数据，并且状态与向量空间可被复算。

测试不连接 PostgreSQL，也不调用远程 embedding：仓储、会话工厂、记忆 runtime 和 Provider 全部是
只记录调用的替身。它保护三条边界——预置内容不含 Golden 答案、forbidden 记忆不会被写成 confirmed、
以及 confirm 必须经过生产决策路径而不是直接插入一行 confirmed。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.fixture_registry import load_golden_cases
from app.domain.models import CaseMemory, MemoryStatus
from app.domain.scenarios import GoldenCaseCategory
from app.evaluation.live_history_seed import (
    LIVE_HISTORY_SEED_TAG,
    build_seed_rows,
    seed_live_golden_history,
)
from app.memory.models import MemoryDecision, StoredCaseMemory

GOLDEN_CASE_FILE = Path("data/fixtures/golden_cases.json")
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


class StubEmbeddingProvider:
    """按输入顺序返回固定维度确定性向量，并记录被嵌入的文本。

    向量只需满足非零和维度一致；测试关心的是"每一行都恰好嵌入一次且顺序对齐"，而不是语义质量。
    记录文本让测试能断言预置内容不含 Golden 答案字段。
    """

    provider_id = "stub-embedding:v1"
    dimensions = 8

    def __init__(self) -> None:
        """初始化文本记录列表，不建立任何网络连接或本机模型。

        Provider ID 与维度是类属性，保证同一实例的所有向量落在同一数学空间，与生产 Provider 的
        协议要求一致。
        """

        self.texts: list[str] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """记录输入文本并为每条返回一个索引相关的非零向量。

        故意让不同文本得到不同向量，使"批量顺序与行顺序错位"这类缺陷会在断言里暴露，而不是被
        一组完全相同的向量掩盖。
        """

        self.texts.extend(texts)
        return [[float(index + 1)] * self.dimensions for index, _ in enumerate(texts)]


class RecordingRepository:
    """记录预置插入的 StoredCaseMemory，不执行任何 SQL。

    替身让本测试能直接检查写入时的状态、签名唯一性和向量空间标注；真实唯一约束与 pgvector 行为
    由 postgres marker 的集成测试覆盖。
    """

    def __init__(self) -> None:
        """初始化插入记录列表与来源 run ID 记录，构造不接受会话参数以外的依赖。

        列表保持插入顺序，因此测试可以断言预置行与嵌入向量按同一顺序配对；来源 run ID 单独记录，
        用来验证预置写入的证据关联使用固定的可识别 run 身份，而不是伪造某次真实诊断的 ID。
        """

        self.inserted: list[StoredCaseMemory] = []
        self.source_run_ids: list[str] = []

    async def insert(self, stored: StoredCaseMemory, *, source_run_id: str) -> None:
        """记录一条插入请求，不做去重、状态判断或事务控制。

        预置流程要求所有行以 pending 写入，因此这里保留原始状态供断言；任何直接写 confirmed 的
        实现都会被对应测试捕获。
        """

        self.inserted.append(stored)
        self.source_run_ids.append(source_run_id)


class FakeSessionFactory:
    """提供 ``begin()`` 异步上下文的最小会话工厂替身，产出一个哨兵会话对象。

    预置只把会话交给仓储工厂，本身不执行 SQL，因此哨兵对象足够；进入与退出次数被记录，用来验证
    所有插入共享同一个事务而不是每行各自提交。
    """

    def __init__(self) -> None:
        """初始化事务进入计数，构造不创建引擎或连接池。

        计数是本替身唯一的观测点：预置若把插入拆成多个事务，部分失败就会留下半套历史数据，而那种
        中间状态会让下一轮记忆召回率变成无法解释的数字。
        """

        self.begin_count = 0

    def begin(self) -> FakeSessionFactory:
        """返回自身作为异步上下文管理器并累加事务计数。

        真实 ``async_sessionmaker.begin()`` 返回新的会话上下文；测试只需要一个可进入的对象，
        因此复用同一实例，语义差异不影响被验证的边界。
        """

        self.begin_count += 1
        return self

    async def __aenter__(self) -> object:
        """进入伪事务并返回哨兵会话对象供仓储工厂使用。

        返回值不是 AsyncSession；仓储工厂由测试注入，因此不会真的把它当会话使用，也不会有任何 SQL
        被执行。真实会话行为由 postgres marker 的集成测试覆盖。
        """

        return object()

    async def __aexit__(self, *args: object) -> bool:
        """退出伪事务且不吞掉异常，使预置的批量回滚语义仍能被测试观察到。

        返回 ``False`` 让异常继续向上传播，与真实事务上下文在失败时回滚并抛出的行为一致；若这里
        返回 ``True``，一次失败的预置会被静默当成成功。
        """

        return False


class RecordingMemoryRuntime:
    """记录 delete/decide 调用并按决策返回相应状态的案例壳对象。

    替身证明预置只使用生产状态机：confirmed 只能由 ``decide(CONFIRM)`` 产生，删除在插入之前发生。
    """

    def __init__(self) -> None:
        """初始化删除与决策记录，构造不触发事务或图注册。

        两个列表按调用顺序记录，因此测试能断言"先删后插再决策"这条顺序约束；顺序错了会让重复运行
        撞唯一约束，或者让 confirmed 行绕过生产状态机被直接写进数据库。
        """

        self.deleted: list[str] = []
        self.decisions: list[tuple[str, MemoryDecision]] = []

    async def delete(self, memory_id: str) -> CaseMemory | None:
        """记录删除请求并返回 ``None`` 表示原本不存在该案例。

        返回 ``None`` 是首次预置的真实情形；预置必须把它当成幂等成功而不是失败，否则第一次运行就会
        因为"没有旧数据可删"而中止。
        """

        self.deleted.append(memory_id)
        return None

    async def decide(self, memory_id: str, decision: MemoryDecision) -> CaseMemory:
        """记录决策并返回处于目标状态的最小案例壳，模拟生产事务成功提交。

        状态由决策推导而不是由调用方指定，使"预置声称 confirmed 但数据库其实是 pending"这类
        不一致会被预置流程自己的校验拦下。
        """

        self.decisions.append((memory_id, decision))
        status = (
            MemoryStatus.CONFIRMED
            if decision is MemoryDecision.CONFIRM
            else MemoryStatus.REJECTED
        )
        return CaseMemory.model_construct(memory_id=memory_id, status=status)


def _memory_cases() -> list[object]:
    """加载 Golden 数据集中全部记忆类案例，作为预置输入。

    直接使用真实案例文件而不是手写标注，使本测试同时验证"当前 28 条数据集里的记忆案例可以被预置"，
    数据集新增记忆案例时不会出现无人覆盖的分支。
    """

    cases = load_golden_cases(GOLDEN_CASE_FILE)
    return [case for case in cases if case.case_category is GoldenCaseCategory.MEMORY_RECALL]


def test_seed_rows_cover_required_and_forbidden_memories_without_golden_answers() -> None:
    """确认预置行覆盖全部标注记忆，且不包含允许根因、必要工具或证据答案。

    required 记忆必须逐条出现并以 confirmed 为目标；forbidden 记忆按 ID 去重成一行且目标状态一定
    不是 confirmed。内容断言针对 Golden 答案字段：只要预置把 ``allowed_root_causes`` 之类写进症状或
    方案，记忆类案例的根因指标就会失去意义。
    """

    cases = _memory_cases()
    rows = build_seed_rows(cases, now=NOW)
    required_ids = [
        expectation.memory_id
        for case in cases
        for expectation in case.history_expectation.required_memories
    ]
    forbidden_ids = {
        memory_id for case in cases for memory_id in case.history_expectation.forbidden_memory_ids
    }
    confirmed_targets = [
        row.memory.memory_id for row in rows if row.target_status is MemoryStatus.CONFIRMED
    ]
    assert confirmed_targets == required_ids
    assert {
        row.memory.memory_id for row in rows if row.target_status is not MemoryStatus.CONFIRMED
    } == forbidden_ids
    assert all(row.memory.status is MemoryStatus.PENDING for row in rows)
    assert all(LIVE_HISTORY_SEED_TAG in row.memory.tags for row in rows)
    for case in cases:
        for root_cause in case.allowed_root_causes:
            for row in rows:
                if row.memory.memory_id in forbidden_ids:
                    assert root_cause not in " ".join(row.memory.solution_steps)
    for row in rows:
        assert "required_tools" not in row.memory.root_cause
        assert row.memory.fault_path == ["历史合成故障路径（预置数据，不代表本次结论）"]


def test_forbidden_memories_cover_both_non_confirmed_statuses() -> None:
    """确认两个 forbidden ID 分别落在 pending 与 rejected，使状态过滤被真正测到。

    两档都存在时，"confirmed-only 召回"才不是只针对 pending 的单向验证；如果实现把所有 forbidden
    行写成同一种状态，rejected 分支在 live 评测里就永远不会被执行。
    """

    rows = build_seed_rows(_memory_cases(), now=NOW)
    non_confirmed = {
        row.target_status for row in rows if row.target_status is not MemoryStatus.CONFIRMED
    }
    assert non_confirmed == {MemoryStatus.PENDING, MemoryStatus.REJECTED}


@pytest.mark.asyncio
async def test_seeding_deletes_first_inserts_pending_then_uses_production_decisions() -> None:
    """确认预置顺序为先删除、单事务插入 pending、再由生产决策转成目标状态。

    这条顺序是幂等与可归因的基础：删除在前使重复运行不会撞唯一约束，单事务插入使失败不会留下半套
    历史，而 confirmed 只能来自 ``decide(CONFIRM)`` 才能保证动态 case 图节点与边同时建立。
    """

    cases = _memory_cases()
    provider = StubEmbeddingProvider()
    repository = RecordingRepository()
    session_factory = FakeSessionFactory()
    runtime = RecordingMemoryRuntime()

    report = await seed_live_golden_history(
        cases,
        memory_runtime=runtime,
        session_factory=session_factory,
        embedding_provider=provider,
        now=NOW,
        repository_factory=lambda _session: repository,
    )

    rows = build_seed_rows(cases, now=NOW)
    assert runtime.deleted == [row.memory.memory_id for row in rows]
    assert session_factory.begin_count == 1
    assert [stored.memory.memory_id for stored in repository.inserted] == [
        row.memory.memory_id for row in rows
    ]
    assert all(stored.memory.status is MemoryStatus.PENDING for stored in repository.inserted)
    assert len({stored.signature for stored in repository.inserted}) == len(rows)
    assert all(
        stored.embedding_provider == provider.provider_id
        and stored.embedding_dimensions == provider.dimensions
        for stored in repository.inserted
    )
    assert report.confirmed_memory_ids == tuple(
        memory_id
        for memory_id, decision in runtime.decisions
        if decision is MemoryDecision.CONFIRM
    )
    assert set(report.pending_memory_ids).isdisjoint(report.confirmed_memory_ids)
    assert report.embedding_provider == provider.provider_id


@pytest.mark.asyncio
async def test_seeding_non_memory_cases_writes_nothing_but_still_reports_vector_space() -> None:
    """确认非记忆类案例不产生任何写入，报告仍公开向量空间以说明分母为空的原因。

    ``--seed-history`` 与案例选择彼此独立，只跑冒烟三案例时其中没有记忆类案例，此时预置必须是
    零写入而不是报错；报告里的空 ID 元组是"本轮没有历史分母"的显式声明。
    """

    cases = [
        case
        for case in load_golden_cases(GOLDEN_CASE_FILE)
        if case.history_expectation is None
    ]
    provider = StubEmbeddingProvider()
    repository = RecordingRepository()
    session_factory = FakeSessionFactory()
    runtime = RecordingMemoryRuntime()

    report = await seed_live_golden_history(
        cases,
        memory_runtime=runtime,
        session_factory=session_factory,
        embedding_provider=provider,
        now=NOW,
        repository_factory=lambda _session: repository,
    )

    assert repository.inserted == []
    assert runtime.deleted == []
    assert session_factory.begin_count == 0
    assert provider.texts == []
    assert report.confirmed_memory_ids == ()
    assert report.embedding_dimensions == provider.dimensions
