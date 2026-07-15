"""实现基于 PostgreSQL 租约的诊断后台 Worker。

Worker 不创建 Agent，也不复制诊断 workflow；它只轮询 agent_runs、用 ``SKIP LOCKED`` 领取一条任务、
在事务外调用既有 DiagnosisApplicationRuntime，并通过心跳延长租约。进程崩溃或取消后，过期租约可被
其他进程重新领取；达到最大尝试次数则写安全 failed 事件，避免队列永久悬挂。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.orchestration.diagnosis_runtime import DiagnosisApplicationRuntime
from app.orchestration.run_models import ClaimedDiagnosisRun
from app.persistence.run_repository import PostgresDiagnosisRunRepository


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间，作为 Worker 生产默认时钟。

    把时钟包装成有名字的 callable 便于测试注入单调合成时钟，也避免在数据库 lease 比较中
    混入 naive datetime。调用方可以替换它来验证过期接管，而不需要 sleep 或修改系统时钟。
    """

    return datetime.now(UTC)


class DiagnosisRunWorker:
    """在当前 API 进程内运行一个可重启、数据库持久化的诊断消费循环。

    进程内 task 只是唤醒器，队列事实、租约和终态都在 PostgreSQL；因此容器重启不丢任务，多个 API
    副本也能依靠行锁分工。Worker 复用已构造的 runtime，不持有模型/MCP/数据库长事务。
    """

    def __init__(
        self,
        runtime: DiagnosisApplicationRuntime,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        poll_interval_seconds: float = 0.25,
        lease_seconds: float = 180.0,
        heartbeat_seconds: float = 30.0,
        max_attempts: int = 2,
        now_factory: Callable[[], datetime] | None = None,
        worker_id: str | None = None,
    ) -> None:
        """注入 runtime、数据库会话工厂和租约/轮询预算，并校验心跳不会晚于租约。

        ``worker_id`` 可由测试固定；生产默认使用随机 16 位十六进制身份。构造只保存配置，不启动
        task 或连接数据库，生命周期由 FastAPI lifespan 的 start/stop 显式控制。
        """

        if poll_interval_seconds <= 0:
            raise ValueError("worker poll interval must be positive")
        if lease_seconds <= 0 or heartbeat_seconds <= 0:
            raise ValueError("worker lease and heartbeat must be positive")
        if heartbeat_seconds >= lease_seconds / 2:
            raise ValueError("worker heartbeat must be less than half the lease")
        if max_attempts < 1:
            raise ValueError("worker max_attempts must be positive")
        self._runtime = runtime
        self._session_factory = session_factory
        self._poll_interval_seconds = poll_interval_seconds
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._max_attempts = max_attempts
        self._now_factory = now_factory or _utc_now
        self._worker_id = worker_id or f"worker_{uuid4().hex[:16]}"
        if re.fullmatch(r"worker_[a-f0-9]{16}", self._worker_id) is None:
            raise ValueError("worker_id must contain worker_ plus 16 hexadecimal characters")
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """启动唯一的轮询 task；重复启动显式失败而不创建竞争消费循环。

        task 只负责等待数据库任务和捕获公开可处理异常；每次具体执行由 ``run_once`` 创建独立
        heartbeat 子任务，便于测试单步运行也便于优雅停机取消轮询。
        """

        if self._task is not None and not self._task.done():
            raise RuntimeError("diagnosis worker is already running")
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_loop(), name=f"diagnosis-worker-{self._worker_id}"
        )

    async def stop(self) -> None:
        """停止轮询 task 并等待其释放会话；正在执行的 run 保留租约，稍后可恢复。

        取消只作用于 Worker task，不把浏览器或进程停机伪装成业务 cancelled；未完成 run 的租约会在
        lease_expires_at 后由新 Worker 接管。``suppress`` 仅吞掉预期的 asyncio.CancelledError。
        """

        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def run_once(self) -> bool:
        """领取并执行最多一条任务，返回是否确实找到任务。

        领取、过期重试清理和租约更新各自使用短事务；workflow 期间不持有数据库行锁。返回 False
        只表示当前队列为空，不代表诊断失败，轮询循环会按固定间隔再次检查。
        """

        now = self._now_factory()
        async with self._session_factory.begin() as session:
            repository = PostgresDiagnosisRunRepository(session)
            # 先终结无法再接管的任务，避免它们长期占用 running 状态而阻塞 session 新消息。
            await repository.fail_exhausted_runs(now=now, max_attempts=self._max_attempts)
            claim = await repository.claim_next_run(
                worker_id=self._worker_id,
                now=now,
                lease_seconds=self._lease_seconds,
                max_attempts=self._max_attempts,
            )
        if claim is None:
            return False
        await self._execute_with_heartbeat(claim)
        return True

    async def _execute_with_heartbeat(self, claim: ClaimedDiagnosisRun) -> None:
        """在子 task 中运行 workflow，并以心跳保证长模型调用不会失去租约。

        ``asyncio.wait`` 只等待而不取消 workflow；每次超时先以条件 UPDATE 续租。若续租失败，说明
        另一个 Worker 已接管，当前执行被取消且不能提交结果，防止两个 Worker 对同一 run 双写。
        调用方取消本方法时也会清理子 task，未完成租约交给数据库恢复机制。
        """

        execution = asyncio.create_task(
            self._runtime.execute_claimed_run(claim),
            name=f"diagnosis-run-{claim.run.run_id}",
        )
        try:
            while True:
                done, _ = await asyncio.wait(
                    {execution},
                    timeout=self._heartbeat_seconds,
                )
                if done:
                    # 传播 DiagnosisExecutionFailed，让 worker loop 记录为已处理而继续消费下一条。
                    execution.result()
                    return
                renewed = await self._renew_claim(claim.run.run_id)
                if not renewed:
                    execution.cancel()
                    with suppress(asyncio.CancelledError):
                        await execution
                    return
        except asyncio.CancelledError:
            execution.cancel()
            with suppress(asyncio.CancelledError):
                await execution
            raise

    async def _renew_claim(self, run_id: str) -> bool:
        """在单独短事务中延长当前 Worker 的 run 租约，并返回 owner 条件是否命中。

        续租时间从当前 UTC 时钟计算；若注入时钟产生 naive 值，仓储显式拒绝而不会写入无时区时间。
        """

        now = self._now_factory()
        async with self._session_factory.begin() as session:
            return await PostgresDiagnosisRunRepository(session).renew_lease(
                run_id,
                worker_id=self._worker_id,
                now=now,
                lease_seconds=self._lease_seconds,
            )

    async def _run_loop(self) -> None:
        """持续消费队列，空队列等待，单条异常不杀死整个 Worker。

        数据库连接故障、workflow 编程异常或租约竞争都会在本轮向上结束；循环将短暂退避后继续，
        使 transient 数据库重启不丢失 API 进程。停止事件只在空闲等待时自然退出，
        停机时 cancel 立即打断。
        """

        while not self._stop_event.is_set():
            try:
                claimed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 详细异常留在受控进程日志；公共 run 状态由 runtime/租约机制保证，
                # 不把 traceback 写入 API。
                # 进入一次短暂 poll 退避，避免数据库故障或租约竞争造成忙等；持久化状态由
                # claim/lease 事务决定，下一轮仍会安全重试或接管。
                claimed = False
            if claimed:
                continue
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                continue
