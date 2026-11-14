"""PipelineWorker - 管道工作进程，消费 Redis Stream 任务

提供：
- PipelineWorker 类：从 TaskQueue 消费任务，通过 Semaphore 控制并发，
  执行 DocumentPipeline 处理，支持失败重试、死信队列、熔断机制。

特性：
- 启动前健康检查 Embedding 服务，不可用时轮询等待
- 单个文档处理总超时（默认 30 分钟）
- 熔断机制：连续 N 次失败后暂停消费，轮询恢复
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.pipeline.queue import TaskMessage, TaskQueue
from app.pipeline.pipeline import CancelledError
from app.schema.db import Document

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.pipeline.pipeline import DocumentPipeline

logger = logging.getLogger("pipeline.worker")

# 不可重试的错误类型：文件不存在、参数错误、权限不足、任务取消
NON_RETRYABLE_ERRORS = (FileNotFoundError, ValueError, PermissionError, CancelledError)


class PipelineWorker:
    """管道工作进程，消费 Redis Stream 任务

    启动时先健康检查 Embedding 服务，通过后 claim_pending() 恢复中断任务，
    再循环 consume() 新任务。使用 asyncio.Semaphore 控制并发处理文件数。
    内置熔断机制，连续失败超过阈值时暂停消费。
    """

    def __init__(
        self,
        queue: TaskQueue,
        pipeline: "DocumentPipeline",
        db_session_factory: "async_sessionmaker[AsyncSession]",
        max_concurrent: int = 3,
        max_retries: int = 3,
        slow_queue: "TaskQueue | None" = None,
        slow_max_concurrent: int = 1,
    ):
        self._queue = queue
        # 慢道队列（大文件）。为 None 时退化为单队列行为，与历史完全一致。
        self._slow_queue = slow_queue
        self._pipeline = pipeline
        self._db_session_factory = db_session_factory
        self._max_concurrent = max_concurrent
        self._max_retries = max_retries
        self.semaphore = asyncio.Semaphore(max_concurrent)
        # 慢道额外信号量：限制慢道在途文档数，保证快道始终有
        # (max_concurrent - slow_max_concurrent) 个文档准入额度，永不被大文件占满。
        self._slow_max_concurrent = slow_max_concurrent
        self._slow_semaphore = asyncio.Semaphore(slow_max_concurrent)
        self._running = False
        self._consumer_name = f"worker-{socket.gethostname()}"
        self._tasks: set[asyncio.Task] = set()

        # 熔断状态
        settings = get_settings()
        self._circuit_breaker_threshold = settings.pipeline_circuit_breaker_threshold
        self._health_check_interval = settings.pipeline_health_check_interval
        self._task_timeout = settings.pipeline_task_timeout_minutes * 60  # 转为秒
        self._consecutive_failures = 0
        self._circuit_open = False

        # PEL 孤儿任务周期性回收（崩溃恢复）
        self._claim_interval = settings.pipeline_claim_interval_seconds
        # idle 阈值必须大于单文档最大处理时长，否则会抢走正在被合法处理的消息。
        # 强制下限：task_timeout + 5 分钟，防止配置写小导致重复处理。
        configured_idle_ms = settings.pipeline_claim_min_idle_minutes * 60 * 1000
        safe_floor_ms = (settings.pipeline_task_timeout_minutes + 5) * 60 * 1000
        self._claim_min_idle_ms = max(configured_idle_ms, safe_floor_ms)
        # 毒消息投递次数上限，复用 max_retries
        self._claim_max_delivery = self._max_retries
        # 上次执行周期性 claim 的时间戳（monotonic）
        self._last_claim_at = 0.0

    async def start(self) -> None:
        """启动 Worker 循环：健康检查 → claim pending → consume new"""
        self._running = True
        logger.info(
            "Pipeline worker starting, max_concurrent=%d, task_timeout=%ds, "
            "circuit_breaker_threshold=%d",
            self._max_concurrent, self._task_timeout, self._circuit_breaker_threshold,
        )

        # 启动前健康检查 Embedding 服务
        await self._wait_for_embedding_service()

        logger.info("Pipeline worker started, embedding service available")
        print("[Worker] Pipeline worker initialized, embedding service healthy")

        # 启动时回收上一个实例遗留在 PEL 中的孤儿任务
        await self._reclaim_orphan_tasks()

        # 主消费循环
        while self._running:
            # 熔断检查
            if self._circuit_open:
                print("[Worker] ⚡ 熔断中，等待 Embedding 服务恢复...")
                logger.warning("Circuit breaker OPEN, waiting for embedding service recovery")
                await self._wait_for_embedding_service()
                self._circuit_open = False
                self._consecutive_failures = 0
                print("[Worker] ✅ Embedding 服务恢复，继续消费")
                logger.info("Circuit breaker CLOSED, resuming consumption")

            # 周期性回收 PEL 孤儿任务（崩溃恢复）。放在 consume 之前，
            # 确保即使本进程一直存活，也能接管其他已死 Worker 的遗留任务，
            # 以及本进程上次崩溃后重启过快（idle 未达阈值）漏掉的任务。
            if time.monotonic() - self._last_claim_at >= self._claim_interval:
                await self._reclaim_orphan_tasks()

            try:
                # Never accumulate an unbounded number of tasks that would each
                # perform a DB status lookup before reaching the processing
                # semaphore. Keep dispatch itself bounded.
                await self._wait_for_task_capacity()
                if not self._running:
                    break
                # 快道：常规/小文件，吃满 max_concurrent
                messages = await self._queue.consume(
                    self._consumer_name, count=1, block_ms=5000
                )
                for message_id, msg in messages:
                    task = asyncio.create_task(
                        self._process_task(message_id, msg, queue=self._queue)
                    )
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)

                # 慢道：大文件，受 _slow_semaphore 限制在途数。短轮询拉取
                # （block_ms=100）——注意 XREADGROUP 中 block=0 是“无限阻塞直到有消息”，
                # 并非不阻塞；队列空时会一直阻塞到 socket_timeout 触发
                # "Timeout reading from redis"。用 100ms 小正值做近似非阻塞拉取，
                # 快道的 5s 长轮询已提供主循环节流，这里只需快速探一下慢道。
                if (
                    self._slow_queue is not None
                    and not self._slow_semaphore.locked()
                    and len(self._tasks) < self._max_concurrent
                ):
                    slow_messages = await self._slow_queue.consume(
                        self._consumer_name, count=1, block_ms=100
                    )
                    for message_id, msg in slow_messages:
                        task = asyncio.create_task(
                            self._process_task(message_id, msg, queue=self._slow_queue)
                        )
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error consuming tasks: %s, retrying in 5s", e)
                await asyncio.sleep(5)

    async def _reclaim_orphan_tasks(self) -> None:
        """认领 PEL 中 idle 超时的孤儿消息并重新处理（快道 + 慢道）

        使用 self._claim_min_idle_ms 作为阈值（已强制 > task_timeout），
        确保不会抢走正在被合法处理的消息。毒消息（投递次数超 max_retries）
        由 claim_pending 内部直接移入 DLQ，不会返回到这里。
        """
        self._last_claim_at = time.monotonic()
        await self._reclaim_from_queue(self._queue)
        if self._slow_queue is not None:
            await self._reclaim_from_queue(self._slow_queue)

    async def _reclaim_from_queue(self, queue: TaskQueue) -> None:
        """从指定队列认领孤儿消息并按所属队列重新派发处理。"""
        try:
            pending = await queue.claim_pending(
                self._consumer_name,
                min_idle_ms=self._claim_min_idle_ms,
                max_delivery_count=self._claim_max_delivery,
                on_poison_pill=self._on_poison_pill,
            )
        except Exception as e:
            logger.warning("Failed to claim pending tasks: %s", e)
            return

        if not pending:
            return

        print(f"[Worker] ♻️ 回收 {len(pending)} 个 PEL 孤儿任务重新处理")
        logger.info("Reclaimed %d orphan task(s) from PEL", len(pending))
        for message_id, msg in pending:
            await self._wait_for_task_capacity()
            task = asyncio.create_task(self._process_task(message_id, msg, queue=queue))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _wait_for_task_capacity(self) -> None:
        """Backpressure the queue consumer before creating another DB-touching task."""
        while self._running and len(self._tasks) >= self._max_concurrent:
            await asyncio.sleep(0.05)

    async def _on_poison_pill(self, msg: TaskMessage, reason: str) -> None:
        """毒消息被移入 DLQ 后的回调：将对应文档标记为 failed

        避免反复崩溃的毒文档在消息进 DLQ 后，DB 状态仍停留在 processing，
        导致前端看到文档永久"处理中"。
        """
        print(f"[Worker] 💀 文档 {msg.doc_id} 判定为毒消息进入 DLQ: {reason}")
        logger.error(
            "Poison-pill document marked as failed: doc_id=%s (trace_id=%s), reason=%s",
            msg.doc_id, msg.trace_id, reason,
        )
        await self._mark_failed(msg.doc_id, f"重复崩溃，已停止重试: {reason}")

    async def stop(self) -> None:
        """停止 Worker，等待所有正在处理的任务完成。"""
        self._running = False
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _wait_for_embedding_service(self) -> None:
        """轮询等待 Embedding 服务可用"""
        attempt = 0
        while self._running:
            attempt += 1
            if await self._ping_embedding():
                if attempt > 1:
                    print(f"[Worker] ✅ Embedding 服务恢复（第 {attempt} 次检查）")
                    logger.info("Embedding service recovered after %d attempts", attempt)
                return
            print(
                f"[Worker] ⏳ Embedding 服务不可用，第 {attempt} 次检查失败，"
                f"{self._health_check_interval}s 后重试..."
            )
            logger.warning(
                "Embedding health check #%d failed, retrying in %ds...",
                attempt, self._health_check_interval,
            )
            await asyncio.sleep(self._health_check_interval)

    async def _ping_embedding(self) -> bool:
        """检查 Embedding 服务是否可用：发一个最小真实推理请求。

        这是主流知识库（WeKnora / Dify / Open-WebUI）的统一做法——以「能否成功 embed
        一小段文本」作为可用性判据，而非探 /health 或 /models：
        - /health 不是所有服务都有（云端 OpenAI 兼容网关、DashScope 等没有）。
        - /v1 路径前缀也因服务而异（TEI 裸 host、Infinity 根路径、DashScope /compatible-mode/v1、
          Azure 完全不同），靠推导 health 路径必然在某类服务上判错。
        - 最小推理请求直接复用「实际生效的 embedder」（manager.embedder，来自数据库 is_active
          配置），与入库时真正发请求的对象、地址、鉴权完全一致，判定结果最可信。

        注意：embedder.embed() 是直接 HTTP 调远程服务，不经过 Redis 入库队列，
        因此这里探活不会消费/占用任务队列。
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

    async def _process_task(
        self, message_id: str, msg: TaskMessage, queue: "TaskQueue | None" = None
    ) -> None:
        """处理单个任务（带总超时）

        Args:
            message_id: 消息 ID
            msg: 任务消息
            queue: 该消息所属队列（快道或慢道）。None 时默认为快道 self._queue，
                   保证旧调用方（及测试）行为不变。
        """
        queue = queue or self._queue
        is_slow = self._slow_queue is not None and queue is self._slow_queue

        # Slow tasks take their dedicated lane slot first, then both lanes compete
        # for the global processing semaphore. All DB work stays inside the slot.
        if is_slow:
            async with self._slow_semaphore:
                await self._process_task_admitted(message_id, msg, queue, is_slow)
        else:
            await self._process_task_admitted(message_id, msg, queue, is_slow)

    async def _process_task_admitted(
        self,
        message_id: str,
        msg: TaskMessage,
        queue: TaskQueue,
        is_slow: bool,
    ) -> None:
        """Run one task only after acquiring a processing slot."""
        async with self.semaphore:
            await self._process_task_in_slot(message_id, msg, queue, is_slow)

    async def _process_task_in_slot(
        self,
        message_id: str,
        msg: TaskMessage,
        queue: TaskQueue,
        is_slow: bool,
    ) -> None:
        """Perform the idempotency lookup and pipeline execution in a bounded slot."""
        # 幂等检查：文档已完成则跳过
        if await self._is_document_completed(msg.doc_id):
            print(f"[Worker] 文档 {msg.doc_id} 已完成/取消/删除，跳过")
            logger.info(
                "Document %s already completed, skipping (trace_id=%s)",
                msg.doc_id, msg.trace_id,
            )
            await queue.ack(message_id)
            return

        print(f"[Worker] 📄 开始处理文档 {msg.doc_id} (lane={'slow' if is_slow else 'fast'}, retry={msg.retry_count}, trace_id={msg.trace_id})")
        logger.info(
            "Processing doc_id=%s, lane=%s, retry=%d, trace_id=%s",
            msg.doc_id, "slow" if is_slow else "fast", msg.retry_count, msg.trace_id,
        )

        await self._run_pipeline_guarded(message_id, msg, queue)

    async def _run_pipeline_guarded(
        self, message_id: str, msg: TaskMessage, queue: "TaskQueue"
    ) -> None:
        """在文档准入信号量内执行 pipeline，处理成功/超时/失败。

        源文件权威存储在 MinIO（object_key）。处理前下载到临时文件，处理完毕
        （成功/失败/超时）删除临时文件。object_key 为空时回退用 msg.file_path
        （兼容历史消息与对象存储不可用的降级路径）。
        """
        try:
            async with self._resolve_file(msg) as file_path:
                # 单个文档处理总超时
                await asyncio.wait_for(
                    self._pipeline.process(
                        file_path=file_path,
                        doc_id=msg.doc_id,
                        kb_id=msg.kb_id,
                    ),
                    timeout=self._task_timeout,
                )
            # 处理成功，ACK 消息，重置熔断计数
            await queue.ack(message_id)
            self._consecutive_failures = 0
            print(f"[Worker] ✅ 文档 {msg.doc_id} 处理完成")
            logger.info(
                "Task completed: doc_id=%s (trace_id=%s)",
                msg.doc_id, msg.trace_id,
            )
        except asyncio.TimeoutError:
            error_msg = f"处理超时（超过 {self._task_timeout // 60} 分钟）"
            print(f"[Worker] ⏰ 文档 {msg.doc_id} {error_msg}")
            logger.error(
                "Task timeout for doc_id=%s (trace_id=%s): %s",
                msg.doc_id, msg.trace_id, error_msg,
            )
            # 超时直接标记失败，不重试
            await self._mark_failed(msg.doc_id, error_msg)
            await queue.ack(message_id)
            self._record_failure()
        except Exception as e:
            print(f"[Worker] ❌ 文档 {msg.doc_id} 处理失败: {type(e).__name__}: {e}")
            logger.error(
                "Task failed: doc_id=%s, error=%s (trace_id=%s)",
                msg.doc_id, e, msg.trace_id,
            )
            self._record_failure()
            await self._handle_failure(message_id, msg, e, queue=queue)

    def _resolve_file(self, msg: TaskMessage):
        """返回一个 async context manager，产出可供 pipeline 处理的本地文件路径。

        - object_key 存在：从 MinIO 下载到临时文件，退出时删除（权威路径）。
        - object_key 为空：回退使用 msg.file_path（历史消息 / 降级路径），不做清理。
        """
        import contextlib
        import os

        object_key = msg.object_key
        if object_key:
            from app.storage.object_store import materialized_file

            suffix = os.path.splitext(object_key)[1]
            return materialized_file(object_key, suffix)

        @contextlib.asynccontextmanager
        async def _local_path():
            yield msg.file_path

        return _local_path()

    def _record_failure(self) -> None:
        """记录失败次数，触发熔断"""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._circuit_breaker_threshold:
            self._circuit_open = True
            logger.error(
                "Circuit breaker OPEN: %d consecutive failures (threshold=%d)",
                self._consecutive_failures, self._circuit_breaker_threshold,
            )
            print(
                f"[Worker] ⚡ 熔断触发：连续 {self._consecutive_failures} 次失败"
            )

    async def _mark_failed(self, doc_id: str, error_message: str) -> None:
        """将文档标记为失败"""
        try:
            async with self._db_session_factory() as session:
                result = await session.execute(
                    select(Document).where(Document.id == doc_id)
                )
                doc = result.scalar_one_or_none()
                if doc and doc.status != "completed":
                    doc.status = "failed"
                    doc.error_message = error_message
                    await session.commit()
        except Exception as e:
            logger.warning("Failed to mark document %s as failed: %s", doc_id, e)

    async def _handle_failure(
        self, message_id: str, msg: TaskMessage, error: Exception,
        queue: "TaskQueue | None" = None,
    ) -> None:
        """失败处理

        - 不可重试错误直接进 DLQ
        - retry_count < max_retries 时指数退避重新入队（回到原所属队列/lane）
        - 否则移入 DLQ

        Args:
            queue: 消息所属队列。None 时默认快道 self._queue，保证旧调用方/测试不变。
        """
        queue = queue or self._queue
        error_str = f"{type(error).__name__}: {error}"

        # 不可重试错误直接进 DLQ
        if isinstance(error, NON_RETRYABLE_ERRORS):
            logger.error(
                "Non-retryable error for doc_id=%s, moving to DLQ: %s (trace_id=%s)",
                msg.doc_id, error_str, msg.trace_id,
            )
            await self._mark_failed(msg.doc_id, error_str)
            await queue.move_to_dlq(message_id, msg, error_str)
            return

        # 检查重试次数
        next_retry = msg.retry_count + 1
        if next_retry <= self._max_retries:
            delay = 2 ** (next_retry - 1)  # 1s, 2s, 4s
            print(
                f"[Worker] 🔄 文档 {msg.doc_id} 第 {next_retry}/{self._max_retries} 次重试，"
                f"{delay}s 后重新入队"
            )
            logger.warning(
                "Task failed for doc_id=%s (retry %d/%d), "
                "retrying in %ds: %s (trace_id=%s)",
                msg.doc_id, next_retry, self._max_retries,
                delay, error_str, msg.trace_id,
            )
            await queue.ack(message_id)
            await asyncio.sleep(delay)
            retry_msg = TaskMessage(
                doc_id=msg.doc_id,
                kb_id=msg.kb_id,
                file_path=msg.file_path,
                retry_count=next_retry,
                created_at=msg.created_at,
                trace_id=msg.trace_id,
                tenant_id=msg.tenant_id,
                object_key=msg.object_key,
            )
            await queue.enqueue(retry_msg)
        else:
            print(f"[Worker] 💀 文档 {msg.doc_id} 重试 {self._max_retries} 次后放弃，进入死信队列")
            logger.error(
                "Max retries exceeded for doc_id=%s, moving to DLQ: %s (trace_id=%s)",
                msg.doc_id, error_str, msg.trace_id,
            )
            await self._mark_failed(msg.doc_id, f"重试 {self._max_retries} 次后失败: {error_str}")
            await queue.move_to_dlq(message_id, msg, error_str)

    async def _is_document_completed(self, doc_id: str) -> bool:
        """检查文档是否应跳过处理"""
        try:
            async with self._db_session_factory() as session:
                result = await session.execute(
                    select(Document.status).where(Document.id == doc_id)
                )
                status = result.scalar_one_or_none()
                if status is None:
                    return True
                return status in ("completed", "cancelled")
        except Exception as e:
            logger.warning(
                "Failed to check document status for %s: %s", doc_id, e
            )
            return False
