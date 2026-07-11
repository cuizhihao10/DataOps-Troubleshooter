"""实现 diagnosis session、agent run 和公开事件的 PostgreSQL 事务仓储。

仓储只负责 ORM/领域转换与行锁，不运行 GraphRAG、模型或 MCP。调用方为创建、完成和失败分别提供
事务边界，使 run 终态与整批事件原子提交，避免轮询看到 completed 却缺少时间线。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capabilities import DiagnosisIntent, HistoryTrigger
from app.domain.models import Component
from app.orchestration.diagnosis_models import DiagnosisRunResult
from app.orchestration.run_models import (
    AgentRunSnapshot,
    AgentRunStatus,
    DiagnosisMessage,
    DiagnosisSession,
    RunEventPhase,
    RunPublicEvent,
)
from app.persistence.models import AgentRunRecord, DiagnosisSessionRecord, RunEventRecord


class PostgresDiagnosisRunRepository:
    """封装会话创建、run 状态转换和连续事件读写。

    AsyncSession 由应用 runtime 拥有；所有写方法不自动 commit。终态更新使用 ``FOR UPDATE`` 并只
    允许 running 转为 completed/failed，防止重复 HTTP 重放覆盖已经完成的可审计结果。
    """

    def __init__(self, session: AsyncSession) -> None:
        """保存调用方提供的短生命周期 AsyncSession，不立即连接数据库。

        同一次状态转换及其事件共享该会话以获得事务原子性；仓储不关闭或缓存 session，避免跨
        FastAPI 请求复用非并发安全对象。
        """

        self._session = session

    async def create_session(
        self,
        *,
        session_id: str,
        title: str,
        now: datetime,
    ) -> DiagnosisSession:
        """插入一个空诊断会话并返回经过领域模型校验的快照。

        ID/title/time 由应用 runtime 预先生成；主键冲突或数据库约束失败由事务向上抛出，不静默
        返回既有会话。flush 后即可在同一事务读取服务端字段。
        """

        record = DiagnosisSessionRecord(
            session_id=session_id,
            title=title,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        return _session_from_record(record)

    async def get_session(self, session_id: str) -> DiagnosisSession | None:
        """按主键读取会话，未命中返回 None 且不修改活动时间。

        空 ID 由 API 路径/领域模型阻止；查询结果不加载 runs，保持 GET/存在性检查成本有界。数据库
        异常原样传播，不能把连接故障解释为 404。
        """

        record = await self._session.get(DiagnosisSessionRecord, session_id)
        return _session_from_record(record) if record is not None else None

    async def create_run(
        self,
        *,
        run_id: str,
        session_id: str,
        message: DiagnosisMessage,
        now: datetime,
    ) -> AgentRunSnapshot | None:
        """锁定会话、创建 running run，并刷新最后问题摘要与活动时间。

        会话不存在返回 None，使 API 可映射 404；存在时 run 与 session touch 在同一事务提交。摘要
        截断到 500 字符，不复制完整 Prompt 到会话列表字段，完整用户文本只保存在 run 行。
        """

        # 锁定 session 可把“刷新最后问题摘要”和“创建 run”绑定为同一活动更新，避免并发消息互相覆盖。
        session_record = await self._session.scalar(
            select(DiagnosisSessionRecord)
            .where(DiagnosisSessionRecord.session_id == session_id)
            .with_for_update()
        )
        if session_record is None:
            return None
        session_record.last_user_query_summary = message.content.strip()[:500]
        session_record.updated_at = now
        record = AgentRunRecord(
            run_id=run_id,
            session_id=session_id,
            status=AgentRunStatus.RUNNING.value,
            user_query=message.content.strip(),
            intent=message.intent.value,
            components=[component.value for component in message.components],
            history_trigger=message.history_trigger.value,
            created_at=now,
            started_at=now,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        return _run_from_record(record)

    async def complete_run(
        self,
        run_id: str,
        *,
        result: DiagnosisRunResult,
        events: tuple[RunPublicEvent, ...],
        now: datetime,
    ) -> AgentRunSnapshot:
        """把 running run 原子转换为 completed，并插入完整公开事件序列。

        ``result`` 的 run/session 身份会在 AgentRunSnapshot 转换时再次验证；events 必须属于同一 run
        且连续。非 running 或缺失 run 抛 LookupError，防止重复执行覆盖既有终态。
        """

        # 先锁定并验证 running，再批量加入事件；外层事务让终态与时间线一起 commit/rollback。
        record = await self._lock_running_run(run_id)
        _validate_events(run_id, events)
        record.status = AgentRunStatus.COMPLETED.value
        record.result = result.model_dump(mode="json")
        record.completed_at = now
        record.updated_at = now
        self._session.add_all([_event_record(event) for event in events])
        await self._session.flush()
        return _run_from_record(record)

    async def fail_run(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        event: RunPublicEvent,
        now: datetime,
    ) -> AgentRunSnapshot:
        """把 running run 原子转换为 failed，并保存一条净化 system 事件。

        只接受非空稳定错误码/公开摘要；原异常通过 Python exception chaining 留给调用日志，不进入
        数据库。事件必须为目标 run 的 sequence=1 system 事件，防止部分成功时间线被伪造成失败。
        """

        if not error_code.strip() or not error_message.strip():
            raise ValueError("failed run requires public error code and message")
        if event.run_id != run_id or event.sequence != 1 or event.phase is not RunEventPhase.SYSTEM:
            raise ValueError("failed run requires a first system event for the same run")
        record = await self._lock_running_run(run_id)
        record.status = AgentRunStatus.FAILED.value
        record.error_code = error_code
        record.error_message = error_message
        record.completed_at = now
        record.updated_at = now
        self._session.add(_event_record(event))
        await self._session.flush()
        return _run_from_record(record)

    async def get_run(self, run_id: str) -> AgentRunSnapshot | None:
        """按 run_id 读取运行快照并恢复版本化 DiagnosisRunResult。

        未命中返回 None；JSONB/Pydantic 不一致会显式失败，避免 API 静默返回数据库污染的部分结构。
        方法不加载事件，GET run 和 GET events 保持独立预算。
        """

        record = await self._session.get(AgentRunRecord, run_id)
        return _run_from_record(record) if record is not None else None

    async def list_events(self, run_id: str) -> tuple[RunPublicEvent, ...] | None:
        """确认 run 存在后按 sequence 返回全部公开事件，未知 run 返回 None。

        先做轻量主键存在性检查可区分“已知 run 暂无事件”和“未知 run”；排序在 SQL 层完成，领域
        ``RunEventList`` 仍会再次验证连续性。
        """

        exists = await self._session.get(AgentRunRecord, run_id)
        if exists is None:
            return None
        records = (
            await self._session.scalars(
                select(RunEventRecord)
                .where(RunEventRecord.run_id == run_id)
                .order_by(RunEventRecord.sequence)
            )
        ).all()
        return tuple(_event_from_record(record) for record in records)

    async def _lock_running_run(self, run_id: str) -> AgentRunRecord:
        """获取 run 行锁并要求当前状态仍为 running。

        行锁把完成/失败竞争串行化；缺失或已终态统一抛 LookupError，由应用 runtime 视为编程/重放
        冲突而不是重新创建事件。锁随外层事务 commit/rollback 释放。
        """

        record = await self._session.scalar(
            select(AgentRunRecord).where(AgentRunRecord.run_id == run_id).with_for_update()
        )
        if record is None:
            raise LookupError(f"agent run not found: {run_id}")
        if record.status != AgentRunStatus.RUNNING.value:
            raise LookupError(f"agent run is already terminal: {run_id}")
        return record


def _session_from_record(record: DiagnosisSessionRecord) -> DiagnosisSession:
    """把 ORM 会话投影为不含 session 对象的冻结领域模型。

    显式字段映射会在数据库枚举/时间漂移时触发 Pydantic 错误；返回对象可直接作为 FastAPI 响应，
    不暴露关联 run 或 SQLAlchemy 内部状态。
    """

    return DiagnosisSession(
        session_id=record.session_id,
        title=record.title,
        last_user_query_summary=record.last_user_query_summary,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _run_from_record(record: AgentRunRecord) -> AgentRunSnapshot:
    """把 ORM run 及可选 JSONB result 恢复为强类型终态快照。

    组件、意图、trigger 和 status 显式转枚举；completed JSON 必须重新通过 DiagnosisRunResult 校验，
    防止代码升级后旧或损坏 payload 未经检查直接进入 API。
    """

    result = DiagnosisRunResult.model_validate(record.result) if record.result is not None else None
    return AgentRunSnapshot(
        run_id=record.run_id,
        session_id=record.session_id,
        status=AgentRunStatus(record.status),
        user_query=record.user_query,
        intent=DiagnosisIntent(record.intent),
        components=tuple(Component(value) for value in record.components),
        history_trigger=HistoryTrigger(record.history_trigger),
        result=result,
        error_code=record.error_code,
        error_message=record.error_message,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        updated_at=record.updated_at,
    )


def _event_record(event: RunPublicEvent) -> RunEventRecord:
    """把公开事件转换为 JSONB payload ORM 行，不修改序号或时间。

    payload 复制为普通 dict，防止调用方后续修改共享引用；方法不 add/flush，事务批量写入由仓储
    完成/失败方法统一控制。
    """

    return RunEventRecord(
        event_id=event.event_id,
        run_id=event.run_id,
        sequence=event.sequence,
        phase=event.phase.value,
        event_type=event.event_type,
        summary=event.summary,
        payload=dict(event.payload),
        created_at=event.created_at,
    )


def _event_from_record(record: RunEventRecord) -> RunPublicEvent:
    """把事件 ORM 行恢复为冻结公开模型并校验 phase/时间。

    数据库未知 phase 或无时区时间会显式失败，不静默转成 system；payload 只做浅复制，因为 JSONB
    已由驱动返回普通可序列化对象。
    """

    return RunPublicEvent(
        event_id=record.event_id,
        run_id=record.run_id,
        sequence=record.sequence,
        phase=RunEventPhase(record.phase),
        event_type=record.event_type,
        summary=record.summary,
        payload=dict(record.payload),
        created_at=record.created_at,
    )


def _validate_events(run_id: str, events: tuple[RunPublicEvent, ...]) -> None:
    """要求 completed run 至少一条事件、同 run 且 sequence 严格连续。

    该校验在插入数据库前执行，避免依赖唯一约束逐条失败后才发现时间线缺口；不自动重排，因为
    顺序错误表示投影逻辑或调用链发生真实漂移。
    """

    if not events:
        raise ValueError("completed run requires public events")
    if any(event.run_id != run_id for event in events):
        raise ValueError("completed run events must share run_id")
    if [event.sequence for event in events] != list(range(1, len(events) + 1)):
        raise ValueError("completed run event sequence must be consecutive from one")
