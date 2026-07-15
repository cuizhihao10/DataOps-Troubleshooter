"""实现 diagnosis session、agent run 和公开事件的 PostgreSQL 事务仓储。

仓储只负责 ORM/领域转换与行锁，不运行 GraphRAG、模型或 MCP。调用方为创建、完成和失败分别提供
事务边界，使 run 终态与整批事件原子提交，避免轮询看到 completed 却缺少时间线。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.capabilities import DiagnosisIntent, HistoryTrigger
from app.domain.models import Component
from app.memory.checkpoint import SessionCheckpoint
from app.orchestration.diagnosis_models import DiagnosisRunResult
from app.orchestration.run_models import (
    ActiveRunConflictError,
    AgentRunSnapshot,
    AgentRunStatus,
    ClaimedDiagnosisRun,
    DiagnosisMessage,
    DiagnosisSession,
    RunEventPhase,
    RunPublicEvent,
)
from app.persistence.models import (
    AgentRunRecord,
    DiagnosisSessionRecord,
    RunEventRecord,
    SessionCheckpointRecord,
)


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
        """锁定会话、创建 queued run，并刷新最后问题摘要与活动时间。

        会话不存在返回 None，使 API 可映射 404；已有 queued/running run 则抛安全冲突，避免多个
        Worker 竞争同一 session checkpoint。摘要截断到 500 字符，完整用户文本只保存在 run 行；
        入队事务不执行 GraphRAG、模型或 MCP，因此 HTTP 响应不会持有长事务。
        """

        # 锁定 session 可把“刷新最后问题摘要”和“创建 run”绑定为同一活动更新，避免并发消息互相覆盖。
        session_record = await self._session.scalar(
            select(DiagnosisSessionRecord)
            .where(DiagnosisSessionRecord.session_id == session_id)
            .with_for_update()
        )
        if session_record is None:
            return None
        active = await self._session.scalar(
            select(AgentRunRecord)
            .where(
                AgentRunRecord.session_id == session_id,
                AgentRunRecord.status.in_(
                    (AgentRunStatus.QUEUED.value, AgentRunStatus.RUNNING.value)
                ),
            )
            .order_by(AgentRunRecord.created_at)
        )
        if active is not None:
            raise ActiveRunConflictError(active.run_id)
        session_record.last_user_query_summary = message.content.strip()[:500]
        session_record.updated_at = now
        record = AgentRunRecord(
            run_id=run_id,
            session_id=session_id,
            status=AgentRunStatus.QUEUED.value,
            user_query=message.content.strip(),
            intent=message.intent.value,
            components=[component.value for component in message.components],
            history_trigger=message.history_trigger.value,
            created_at=now,
            started_at=None,
            attempt_count=0,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        return _run_from_record(record)

    async def claim_next_run(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: float,
        max_attempts: int,
    ) -> ClaimedDiagnosisRun | None:
        """使用 ``FOR UPDATE SKIP LOCKED`` 原子领取最早 queued 或已过期 running run。

        领取事务只更新状态、attempt_count 和短租约，随后立即提交并释放行锁；真正 GraphRAG/模型/MCP
        执行发生在事务外。已过期 running 允许有限重试，进程崩溃后新 Worker 可接管，
        不会永久卡住队列。
        """

        if now.tzinfo is None or lease_seconds <= 0 or max_attempts < 1:
            raise ValueError("worker claim requires timezone, positive lease, and attempts")
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        eligible = or_(
            AgentRunRecord.status == AgentRunStatus.QUEUED.value,
            and_(
                AgentRunRecord.status == AgentRunStatus.RUNNING.value,
                AgentRunRecord.lease_expires_at.is_not(None),
                AgentRunRecord.lease_expires_at <= now,
                AgentRunRecord.attempt_count < max_attempts,
            ),
        )
        record = await self._session.scalar(
            select(AgentRunRecord)
            .where(eligible)
            .order_by(AgentRunRecord.created_at, AgentRunRecord.run_id)
            .with_for_update(skip_locked=True)
        )
        if record is None:
            return None
        if record.status == AgentRunStatus.QUEUED.value:
            record.started_at = now
        record.status = AgentRunStatus.RUNNING.value
        record.attempt_count += 1
        record.lease_owner = worker_id
        record.lease_expires_at = lease_expires_at
        record.updated_at = now
        await self._session.flush()
        return ClaimedDiagnosisRun(
            run=_run_from_record(record),
            worker_id=worker_id,
            lease_expires_at=lease_expires_at,
        )

    async def fail_exhausted_runs(
        self,
        *,
        now: datetime,
        max_attempts: int,
    ) -> int:
        """把已过期且达到最大领取次数的 run 标为安全 failed，并写入 system 事件。

        该清理与领取使用同一 ``SKIP LOCKED`` 思路，避免多个 Worker 重复处理；只有没有任何 Worker
        能再合法接管的任务才会被终结。事件不包含租约、异常或模型内容，客户端可据此停止轮询。
        """

        if now.tzinfo is None or max_attempts < 1:
            raise ValueError("worker reaper requires timezone and positive attempts")
        records = (
            await self._session.scalars(
                select(AgentRunRecord)
                .where(
                    AgentRunRecord.status == AgentRunStatus.RUNNING.value,
                    AgentRunRecord.lease_expires_at.is_not(None),
                    AgentRunRecord.lease_expires_at <= now,
                    AgentRunRecord.attempt_count >= max_attempts,
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
        for record in records:
            record.status = AgentRunStatus.FAILED.value
            record.error_code = "worker_attempts_exhausted"
            record.error_message = "诊断 Worker 重试次数已耗尽；请重新提交问题。"
            record.completed_at = now
            record.updated_at = now
            record.lease_owner = None
            record.lease_expires_at = None
            last_sequence = await self._session.scalar(
                select(func.max(RunEventRecord.sequence)).where(
                    RunEventRecord.run_id == record.run_id
                )
            )
            sequence = (last_sequence or 0) + 1
            self._session.add(
                _event_record(
                    RunPublicEvent(
                        event_id=f"run_evt_{uuid4().hex[:16]}",
                        run_id=record.run_id,
                        sequence=sequence,
                        phase=RunEventPhase.SYSTEM,
                        event_type="worker_attempts_exhausted",
                        summary="诊断 Worker 重试次数已耗尽；请重新提交问题。",
                        payload={"error_code": "worker_attempts_exhausted"},
                        created_at=now,
                    )
                )
            )
        await self._session.flush()
        return len(records)

    async def renew_lease(
        self,
        run_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: float,
    ) -> bool:
        """在 workflow 仍运行且 owner 未过期时延长租约，并返回是否仍拥有该 run。

        条件更新而非先读后写避免两个 Worker 之间的竞态；rowcount 为零表示任务已被别的 Worker 接管
        或已终态，执行方必须停止继续提交结果。该方法不保存 heartbeat 事件，避免时间线噪声。
        """

        if now.tzinfo is None or lease_seconds <= 0:
            raise ValueError("worker lease renewal requires timezone and positive lease")
        result = await self._session.execute(
            update(AgentRunRecord)
            .where(
                AgentRunRecord.run_id == run_id,
                AgentRunRecord.status == AgentRunStatus.RUNNING.value,
                AgentRunRecord.lease_owner == worker_id,
                AgentRunRecord.lease_expires_at > now,
            )
            .values(
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
        )
        return result.rowcount == 1

    async def complete_run(
        self,
        run_id: str,
        *,
        result: DiagnosisRunResult,
        events: tuple[RunPublicEvent, ...],
        checkpoint: SessionCheckpoint,
        worker_id: str,
        now: datetime,
    ) -> AgentRunSnapshot:
        """把 running run 原子转换为 completed，并写事件与最新会话 checkpoint。

        ``result``、events 与 checkpoint 必须属于同一 run/session；三者和 run 终态共用外层事务，
        因而轮询者不会看到 completed 但追问仍读取旧快照。非 running、版本跳跃或身份不一致显式
        失败，防止并发/重放覆盖更新的会话上下文。
        """

        # 先锁定并验证 running，再批量加入事件；外层事务让终态与时间线一起 commit/rollback。
        record = await self._lock_owned_running_run(run_id, worker_id=worker_id, now=now)
        _validate_events(run_id, events)
        if checkpoint.source_run_id != run_id or checkpoint.session_id != record.session_id:
            raise ValueError("checkpoint must belong to the completed run and session")
        record.status = AgentRunStatus.COMPLETED.value
        record.result = result.model_dump(mode="json")
        record.completed_at = now
        record.updated_at = now
        record.lease_owner = None
        record.lease_expires_at = None
        self._session.add_all([_event_record(event) for event in events])
        await self._save_checkpoint(checkpoint)
        await self._session.flush()
        return _run_from_record(record)

    async def get_checkpoint(self, session_id: str) -> SessionCheckpoint | None:
        """读取一个 session 最新的版本化 checkpoint，未生成时返回 None。

        JSONB 必须重新通过 ``SessionCheckpoint`` 校验；损坏或旧版本数据不能静默当作无上下文。
        方法不锁行，调用方只把返回值作为一次 run 的不可变输入快照，后续写入使用独立事务。
        """

        record = await self._session.get(SessionCheckpointRecord, session_id)
        if record is None:
            return None
        return _checkpoint_from_record(record)

    async def fail_run(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        event: RunPublicEvent,
        worker_id: str,
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
        record = await self._lock_owned_running_run(run_id, worker_id=worker_id, now=now)
        record.status = AgentRunStatus.FAILED.value
        record.error_code = error_code
        record.error_message = error_message
        record.completed_at = now
        record.updated_at = now
        record.lease_owner = None
        record.lease_expires_at = None
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

    async def _lock_owned_running_run(
        self,
        run_id: str,
        *,
        worker_id: str,
        now: datetime,
    ) -> AgentRunRecord:
        """获取 run 行锁并要求当前状态、owner 和租约仍然有效。

        行锁把完成/失败竞争串行化；租约过期后旧 Worker 即使完成模型调用也不能覆盖新 Worker 的
        结果。缺失、已终态或 owner 不匹配统一抛 LookupError，锁随外层事务释放。
        """

        record = await self._session.scalar(
            select(AgentRunRecord).where(AgentRunRecord.run_id == run_id).with_for_update()
        )
        if record is None:
            raise LookupError(f"agent run not found: {run_id}")
        if (
            record.status != AgentRunStatus.RUNNING.value
            or record.lease_owner != worker_id
            or record.lease_expires_at is None
            or record.lease_expires_at <= now
        ):
            raise LookupError(f"agent run lease is no longer owned: {run_id}")
        return record

    async def _save_checkpoint(self, checkpoint: SessionCheckpoint) -> None:
        """在当前完成事务内插入首版快照或单调覆盖同 session 的下一版本。

        现有行使用 ``FOR UPDATE`` 串行化并发完成；新版本必须恰好加一，created_at 必须保持不变。
        这会让基于旧快照完成的并发 run 明确失败，而不是倒退 source_run 或覆盖更新的上下文。
        """

        current = await self._session.scalar(
            select(SessionCheckpointRecord)
            .where(SessionCheckpointRecord.session_id == checkpoint.session_id)
            .with_for_update()
        )
        if current is None:
            if checkpoint.checkpoint_version != 1:
                raise ValueError("first session checkpoint must use version one")
            self._session.add(
                SessionCheckpointRecord(
                    session_id=checkpoint.session_id,
                    source_run_id=checkpoint.source_run_id,
                    checkpoint_version=checkpoint.checkpoint_version,
                    snapshot=checkpoint.model_dump(mode="json"),
                    created_at=checkpoint.created_at,
                    updated_at=checkpoint.updated_at,
                )
            )
            return
        if checkpoint.checkpoint_version != current.checkpoint_version + 1:
            raise ValueError("session checkpoint version must increase by exactly one")
        if checkpoint.created_at != current.created_at:
            raise ValueError("session checkpoint created_at must remain stable across updates")

        current.source_run_id = checkpoint.source_run_id
        current.checkpoint_version = checkpoint.checkpoint_version
        current.snapshot = checkpoint.model_dump(mode="json")
        current.updated_at = checkpoint.updated_at


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
        attempt_count=record.attempt_count,
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


def _checkpoint_from_record(record: SessionCheckpointRecord) -> SessionCheckpoint:
    """把 checkpoint JSONB 与关系列交叉校验后恢复为冻结领域模型。

    snapshot 自带身份、版本和时间；关系列是数据库索引/外键事实。两者任何漂移都抛 ValueError，
    防止只修改 JSONB 或只修改列后把错误上下文恢复到下一条用户消息。
    """

    checkpoint = SessionCheckpoint.model_validate(record.snapshot)
    if checkpoint.session_id != record.session_id:
        raise ValueError("checkpoint snapshot session_id does not match its row")
    if checkpoint.source_run_id != record.source_run_id:
        raise ValueError("checkpoint snapshot source_run_id does not match its row")
    if checkpoint.checkpoint_version != record.checkpoint_version:
        raise ValueError("checkpoint snapshot version does not match its row")
    if checkpoint.created_at != record.created_at or checkpoint.updated_at != record.updated_at:
        raise ValueError("checkpoint snapshot timestamps do not match its row")
    return checkpoint


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
