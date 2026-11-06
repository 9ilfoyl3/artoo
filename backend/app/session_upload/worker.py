"""SessionUploadWorker - 会话上传建索引工作进程，消费 Redis Stream 任务

提供：
- SessionUploadWorker 类：从 ``SessionUploadQueue`` 消费会话上传任务，通过
  ``asyncio.Semaphore`` 控制并发，调用 ``SessionUploadService.process_task`` 执行
  建索引，支持失败重试、死信队列、毒消息回调、PEL 崩溃恢复。

设计见 session-upload-async-ws design C8：结构参照 ``PipelineWorker``，但**不重复实现
建索引逻辑**——建索引 + 状态/事件更新都在 ``SessionUploadService.process_task(task)``
内完成（任务 5.2）。本 worker 只负责「队列机械」：

- consume 循环 + ``asyncio.Semaphore(session_upload_max_concurrent)`` 控并发。
- 单任务处理总超时（``asyncio.wait_for``）：超时置 ``SessionFile.status=failed`` + ACK。
- 失败重试指数退避重新入队，超上限移入 DLQ。
- 毒消息（反复崩溃超投递上限）回调置 ``SessionFile.status=failed``（``claim_pending``）。
- ``claim_pending`` 周期性回收 PEL 孤儿任务（崩溃恢复）。
- 幂等跳过已 completed 的文件（``process_task`` 内亦有幂等检查，此处为轻量前置跳过）。
- 复用 ``PipelineWorker`` 的 embedding 健康检查 / 熔断等待逻辑：启动前及熔断打开时
  轮询等待 embedding 服务可用，连续失败超阈值触发熔断暂停消费，避免 embedding 挂掉
  时反复失败刷 DLQ（任务 6.2 / REQ-9）。

与文档入库 ``PipelineWorker`` / ``TaskQueue`` 物理隔离（独立 stream / group / DLQ）。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.config import get_settings
from app.schema.db import SessionFile
from app.session_upload.events import make_failed
from app.storage.database import async_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.session_upload.queue import SessionUploadQueue, SessionUploadTask
    from app.session_upload.service import SessionUploadService

logger = logging.getLogger("session_upload.worker")

# 不可重试的错误类型：入参/内容类错误重试也不会成功，直接进 DLQ。
# - FileNotFoundError：MinIO 原件丢失（object_key 无对象）。
# - ValueError / PermissionError：参数/权限错误。
# - EmptyDocumentContentError：缺少必要提取能力，或非显式空文档切出零 chunk
#   （内容/配置问题，重试无意义）。
# - UploadCapExceeded：容量闸门拒绝（配置未变前重试必再超限）。
# 均通过延迟导入在 ``_non_retryable_errors`` 中解析，避免模块导入期硬依赖。


def _non_retryable_errors() -> tuple[type[BaseException], ...]:
    """解析不可重试异常类型元组（延迟导入，避免循环依赖 / 导入期副作用）。"""
    from app.api.errors import EmptyDocumentContentError
    from app.pipeline.pipeline import UploadCapExceeded

    return (
        FileNotFoundError,
        ValueError,
        PermissionError,
        EmptyDocumentContentError,
        UploadCapExceeded,
    )


class SessionUploadWorker:
    """会话上传建索引工作进程，消费 Redis Stream 任务。

    启动后先 ``claim_pending()`` 回收上一实例遗留在 PEL 中的孤儿任务，再循环
    ``consume()`` 新任务。使用 ``asyncio.Semaphore`` 控制并发建索引数。建索引本体
    委托 ``SessionUploadService.process_task``；本类只做超时保护 / 重试 / DLQ /
    毒消息 / 崩溃恢复的队列机械。
    """

    def __init__(
        self,
        queue: "SessionUploadQueue",
        service: "SessionUploadService",
        *,
        db_session_factory: "async_sessionmaker[AsyncSession] | None" = None,
        max_concurrent: int | None = None,
        max_retries: int | None = None,
        task_timeout_seconds: float | None = None,
    ):
        self._queue = queue
        self._service = service
        # 幂等前置跳过需要读 SessionFile.status；默认工程内单例 async_session，
        # 测试可注入内存 sqlite 工厂。
        self._db_session_factory = db_session_factory or async_session

        # 配置项（session_upload_* 由任务 9.1 添加）——防御式 getattr 读取并带安全
        # 默认值，使本任务不硬依赖 9.1 落地。构造参数显式传入时优先。
        settings = get_settings()
        self._max_concurrent = (
            max_concurrent
            if max_concurrent is not None
            else getattr(settings, "session_upload_max_concurrent", 4)
        )
        self._max_retries = (
            max_retries
            if max_retries is not None
            else getattr(settings, "session_upload_max_retries", 3)
        )
        task_timeout_minutes = getattr(
            settings, "session_upload_task_timeout_minutes", 30
        )
        self._task_timeout = (
            task_timeout_seconds
            if task_timeout_seconds is not None
            else task_timeout_minutes * 60
        )

        self.semaphore = asyncio.Semaphore(self._max_concurrent)
        self._running = False
        self._consumer_name = f"session-upload-worker-{socket.gethostname()}"
        self._tasks: set[asyncio.Task] = set()

        # embedding 健康检查 / 熔断状态（任务 6.2）。会话上传与文档入库共享同一 embedding
        # 服务，故直接复用 pipeline_* 健康检查/熔断配置（防御式 getattr 带安全默认，
        # 避免硬依赖）。目的：embedding 挂掉时轮询等待而非反复失败刷 DLQ。
        self._health_check_interval = getattr(
            settings, "pipeline_health_check_interval", 30
        )
        self._circuit_breaker_threshold = getattr(
            settings, "pipeline_circuit_breaker_threshold", 5
        )
        self._consecutive_failures = 0
        self._circuit_open = False

        # PEL 孤儿任务周期性回收（崩溃恢复）
        self._claim_interval = getattr(
            settings, "session_upload_claim_interval_seconds", 60
        )
        # idle 阈值必须大于单任务最大处理时长，否则会抢走正在被合法处理的消息。
        # 强制下限：task_timeout + 5 分钟（复用 PipelineWorker 的 safe-floor 模式）。
        # task_timeout 以秒计算，safe_floor 直接从秒换算成毫秒。
        safe_floor_ms = int((self._task_timeout + 5 * 60) * 1000)
        configured_idle_minutes = getattr(
            settings, "session_upload_claim_min_idle_minutes", 0
        )
        configured_idle_ms = int(configured_idle_minutes) * 60 * 1000
        self._claim_min_idle_ms = max(configured_idle_ms, safe_floor_ms)
        # 毒消息投递次数上限，复用 max_retries
        self._claim_max_delivery = self._max_retries
        # 上次执行周期性 claim 的时间戳（monotonic）
        self._last_claim_at = 0.0

    async def start(self) -> None:
        """启动 Worker 循环：（embedding 就绪 hook）→ claim pending → consume new。"""
        self._running = True
        logger.info(
            "Session upload worker starting, max_concurrent=%d, task_timeout=%ds, "
            "max_retries=%d, circuit_breaker_threshold=%d",
            self._max_concurrent, self._task_timeout, self._max_retries,
            self._circuit_breaker_threshold,
        )

        # 启动前健康检查 embedding 服务，不可用时轮询等待（复用 PipelineWorker 逻辑）。
        await self._wait_for_embedding_service()

        logger.info("Session upload worker started, embedding service available")
        print("[SessionUploadWorker] initialized, embedding service healthy")

        # 启动时回收上一个实例遗留在 PEL 中的孤儿任务（崩溃恢复）。
        await self._reclaim_orphan_tasks()

        # 主消费循环
        while self._running:
            # 熔断检查：连续失败超阈值时暂停消费，轮询等待 embedding 恢复后再继续，
            # 避免 embedding 挂掉时反复失败把任务刷进 DLQ。
            if self._circuit_open:
                print("[SessionUploadWorker] ⚡ 熔断中，等待 Embedding 服务恢复...")
                logger.warning(
                    "Circuit breaker OPEN, waiting for embedding service recovery"
                )
                await self._wait_for_embedding_service()
                self._circuit_open = False
                self._consecutive_failures = 0
                print("[SessionUploadWorker] ✅ Embedding 服务恢复，继续消费")
                logger.info("Circuit breaker CLOSED, resuming consumption")

            # 周期性回收 PEL 孤儿任务（崩溃恢复）。放在 consume 之前，确保即使本进程
            # 一直存活，也能接管其他已死 Worker 的遗留任务，以及本进程上次崩溃后重启
            # 过快（idle 未达阈值）漏掉的任务。
            if time.monotonic() - self._last_claim_at >= self._claim_interval:
                await self._reclaim_orphan_tasks()

            try:
                messages = await self._queue.consume(
                    self._consumer_name, count=1, block_ms=5000
                )
                for message_id, task in messages:
                    worker_task = asyncio.create_task(
                        self._process_task(message_id, task)
                    )
                    self._tasks.add(worker_task)
                    worker_task.add_done_callback(self._tasks.discard)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error consuming session upload tasks: %s, retrying in 5s", e)
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """停止 Worker，等待所有正在处理的任务完成。"""
        self._running = False
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _wait_for_embedding_service(self) -> None:
        """轮询等待 embedding 服务可用（复用 PipelineWorker 逻辑）。

        会话上传同样依赖 embedding；embedding 挂掉时在此轮询等待而非反复失败刷 DLQ。
        """
        attempt = 0
        while self._running:
            attempt += 1
            if await self._ping_embedding():
                if attempt > 1:
                    print(
                        f"[SessionUploadWorker] ✅ Embedding 服务恢复"
                        f"（第 {attempt} 次检查）"
                    )
                    logger.info(
                        "Embedding service recovered after %d attempts", attempt
                    )
                return
            print(
                f"[SessionUploadWorker] ⏳ Embedding 服务不可用，第 {attempt} 次检查失败，"
                f"{self._health_check_interval}s 后重试..."
            )
            logger.warning(
                "Embedding health check #%d failed, retrying in %ds...",
                attempt, self._health_check_interval,
            )
            await asyncio.sleep(self._health_check_interval)

    async def _ping_embedding(self) -> bool:
        """检查 embedding 服务是否可用：发一个最小真实推理请求（复用 PipelineWorker）。

        以「能否成功 embed 一小段文本」作为可用性判据，直接复用实际生效的 embedder
        （manager.embedder，来自数据库 is_active 配置），与建索引时真正发请求的对象、
        地址、鉴权完全一致。embedder.embed() 直接 HTTP 调远程服务，不经 Redis 队列，
        因此探活不会消费/占用任务队列。
        """
        try:
            from app.models.manager import get_model_manager

            manager = get_model_manager()
            # 直接发最小推理请求；占位 Provider（未配置）会抛错 → 判定不可用
            await asyncio.wait_for(manager.embedder.embed(["ping"]), timeout=10.0)
            return True
        except Exception as e:
            logger.debug("Embedding health check failed: %s", e)
            return False

    def _record_failure(self) -> None:
        """记录连续失败次数，达阈值触发熔断（复用 PipelineWorker 逻辑）。"""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._circuit_breaker_threshold:
            self._circuit_open = True
            logger.error(
                "Circuit breaker OPEN: %d consecutive failures (threshold=%d)",
                self._consecutive_failures, self._circuit_breaker_threshold,
            )
            print(
                f"[SessionUploadWorker] ⚡ 熔断触发："
                f"连续 {self._consecutive_failures} 次失败"
            )

    async def _reclaim_orphan_tasks(self) -> None:
        """认领 PEL 中 idle 超时的孤儿消息并重新处理（崩溃恢复）。

        使用 ``self._claim_min_idle_ms``（已强制 > task_timeout）作为阈值，确保不会
        抢走正在被合法处理的消息。毒消息（投递次数超 max_retries）由 claim_pending
        内部直接移入 DLQ 并回调 ``_on_poison_pill`` 置 failed，不会返回到这里。
        """
        self._last_claim_at = time.monotonic()
        try:
            pending = await self._queue.claim_pending(
                self._consumer_name,
                min_idle_ms=self._claim_min_idle_ms,
                max_delivery_count=self._claim_max_delivery,
                on_poison_pill=self._on_poison_pill,
            )
        except Exception as e:
            logger.warning("Failed to claim pending session upload tasks: %s", e)
            return

        if not pending:
            return

        print(f"[SessionUploadWorker] ♻️ 回收 {len(pending)} 个 PEL 孤儿任务重新处理")
        logger.info("Reclaimed %d orphan session upload task(s) from PEL", len(pending))
        for message_id, task in pending:
            worker_task = asyncio.create_task(self._process_task(message_id, task))
            self._tasks.add(worker_task)
            worker_task.add_done_callback(self._tasks.discard)

    async def _on_poison_pill(self, task: "SessionUploadTask", reason: str) -> None:
        """毒消息被移入 DLQ 后的回调：将对应会话文件标记为 failed。

        避免反复崩溃的毒任务在消息进 DLQ 后，DB 状态仍停留在 processing/queued，
        导致前端看到文件永久"处理中"（REQ-3：不永久停留 processing）。
        """
        print(f"[SessionUploadWorker] 💀 会话文件 {task.file_id} 判定为毒消息进入 DLQ: {reason}")
        logger.error(
            "Poison-pill session file marked as failed: file_id=%s (trace_id=%s), reason=%s",
            task.file_id, task.trace_id, reason,
        )
        await self._mark_failed(task, f"重复崩溃，已停止重试: {reason}")

    async def _process_task(
        self, message_id: str, task: "SessionUploadTask"
    ) -> None:
        """处理单个任务（幂等前置跳过 + 并发信号量 + 总超时保护）。

        建索引本体委托 ``service.process_task``：
        - 成功 → ACK。
        - 超时（``asyncio.wait_for`` 取消 process_task）→ 置 failed + publish failed + ACK。
          （process_task 被取消不会走到它自身的 except，故超时终态由 worker 补齐。）
        - 其他异常（process_task 已置 failed 并重新抛出）→ ``_handle_failure``
          做重试 / DLQ 机械（failed 幂等；重试成功时 process_task 会再更新为 completed）。
        """
        # 幂等前置跳过：已 completed / 已删除的文件不进入建索引计算。
        # （process_task 内部亦有幂等检查，此处提前跳过省去信号量占用与下载开销。）
        if await self._is_file_completed(task.file_id):
            print(f"[SessionUploadWorker] 会话文件 {task.file_id} 已完成/删除，跳过")
            logger.info(
                "Session file %s already completed/removed, skipping (trace_id=%s)",
                task.file_id, task.trace_id,
            )
            await self._queue.ack(message_id)
            return

        print(
            f"[SessionUploadWorker] 📄 开始处理会话文件 {task.file_id} "
            f"(retry={task.retry_count}, trace_id={task.trace_id})"
        )
        logger.info(
            "Processing session file file_id=%s, session=%s, retry=%d, trace_id=%s",
            task.file_id, task.session_id, task.retry_count, task.trace_id,
        )

        async with self.semaphore:
            try:
                await asyncio.wait_for(
                    self._service.process_task(task),
                    timeout=self._task_timeout,
                )
                # 处理成功，ACK 消息，重置熔断计数
                await self._queue.ack(message_id)
                self._consecutive_failures = 0
                print(f"[SessionUploadWorker] ✅ 会话文件 {task.file_id} 处理完成")
                logger.info(
                    "Session upload task completed: file_id=%s (trace_id=%s)",
                    task.file_id, task.trace_id,
                )
            except asyncio.TimeoutError:
                error_msg = f"处理超时（超过 {int(self._task_timeout // 60)} 分钟）"
                print(f"[SessionUploadWorker] ⏰ 会话文件 {task.file_id} {error_msg}")
                logger.error(
                    "Session upload task timeout for file_id=%s (trace_id=%s): %s",
                    task.file_id, task.trace_id, error_msg,
                )
                # 超时：process_task 被取消未落终态，worker 补齐 failed + 事件，不重试。
                await self._mark_failed(task, error_msg)
                await self._queue.ack(message_id)
                self._record_failure()
            except asyncio.CancelledError:
                # worker 停机/任务取消：不吞掉，向上传播以便 gather 收尾。
                raise
            except Exception as e:
                print(
                    f"[SessionUploadWorker] ❌ 会话文件 {task.file_id} 处理失败: "
                    f"{type(e).__name__}: {e}"
                )
                logger.error(
                    "Session upload task failed: file_id=%s, error=%s (trace_id=%s)",
                    task.file_id, e, task.trace_id,
                )
                self._record_failure()
                await self._handle_failure(message_id, task, e)

    async def _handle_failure(
        self, message_id: str, task: "SessionUploadTask", error: Exception
    ) -> None:
        """失败处理。

        - 不可重试错误直接进 DLQ。
        - retry_count < max_retries 时指数退避重新入队。
        - 否则移入 DLQ。

        注意：``process_task`` 在抛出前已把 ``SessionFile`` 置 failed + publish failed，
        故此处无需重复置 failed（重试成功时 process_task 会把行更新回 completed）。
        """
        error_str = f"{type(error).__name__}: {error}"

        # 不可重试错误直接进 DLQ
        if isinstance(error, _non_retryable_errors()):
            logger.error(
                "Non-retryable error for session file file_id=%s, moving to DLQ: "
                "%s (trace_id=%s)",
                task.file_id, error_str, task.trace_id,
            )
            await self._queue.move_to_dlq(message_id, task, error_str)
            return

        # 检查重试次数
        next_retry = task.retry_count + 1
        if next_retry <= self._max_retries:
            delay = 2 ** (next_retry - 1)  # 1s, 2s, 4s
            print(
                f"[SessionUploadWorker] 🔄 会话文件 {task.file_id} "
                f"第 {next_retry}/{self._max_retries} 次重试，{delay}s 后重新入队"
            )
            logger.warning(
                "Session upload task failed for file_id=%s (retry %d/%d), "
                "retrying in %ds: %s (trace_id=%s)",
                task.file_id, next_retry, self._max_retries,
                delay, error_str, task.trace_id,
            )
            await self._queue.ack(message_id)
            await asyncio.sleep(delay)
            retry_task = self._build_retry_task(task, next_retry)
            await self._queue.enqueue(retry_task)
        else:
            print(
                f"[SessionUploadWorker] 💀 会话文件 {task.file_id} "
                f"重试 {self._max_retries} 次后放弃，进入死信队列"
            )
            logger.error(
                "Max retries exceeded for session file file_id=%s, moving to DLQ: "
                "%s (trace_id=%s)",
                task.file_id, error_str, task.trace_id,
            )
            await self._mark_failed(
                task, f"重试 {self._max_retries} 次后失败: {error_str}"
            )
            await self._queue.move_to_dlq(message_id, task, error_str)

    @staticmethod
    def _build_retry_task(
        task: "SessionUploadTask", next_retry: int
    ) -> "SessionUploadTask":
        """构造重试任务：仅递增 retry_count，其余字段（含 trace_id/created_at）沿用。"""
        from app.session_upload.queue import SessionUploadTask

        return SessionUploadTask(
            file_id=task.file_id,
            session_id=task.session_id,
            tenant_id=task.tenant_id,
            owner_user_id=task.owner_user_id,
            object_key=task.object_key,
            ext=task.ext,
            filename=task.filename,
            retry_count=next_retry,
            created_at=task.created_at,
            trace_id=task.trace_id,
        )

    async def _mark_failed(self, task: "SessionUploadTask", error_message: str) -> None:
        """将会话文件置 failed 并 publish failed 事件（终态一致）。

        复用 ``service._safe_mark_failed``（幂等：已 completed 的行不被覆盖，失败仅
        WARNING）。随后 best-effort publish failed 事件，让订阅 WS 的客户端看到终态。
        """
        await self._service._safe_mark_failed(task.file_id, error_message)
        await self._service._publish_event(
            make_failed(
                task.session_id, task.file_id, error_message,
                filename=task.filename, message="建索引失败",
            )
        )

    async def _is_file_completed(self, file_id: str) -> bool:
        """检查会话文件是否应跳过处理（已完成或已删除）。

        - 行不存在（已删除）→ True（跳过）。
        - status == "completed" → True（跳过，幂等）。
        - 其余 → False。
        - 查询异常 → False（不跳过，交由 process_task 内部幂等兜底）。
        """
        try:
            async with self._db_session_factory() as session:
                result = await session.execute(
                    select(SessionFile.status).where(SessionFile.id == file_id)
                )
                status = result.scalar_one_or_none()
                if status is None:
                    return True
                return status == "completed"
        except Exception as e:
            logger.warning(
                "Failed to check session file status for %s: %s", file_id, e
            )
            return False
