"""Pipeline Worker 独立进程入口

独立于 API 服务运行，消费 Redis Stream 中的文档处理任务。
与 API 不共享事件循环，避免大文件 Embedding 阻塞 API 响应。

启动方式：
    python -m app.worker_main
"""

import asyncio
import logging
import signal
import sys

from app.logging_config import setup_logging

# 在所有业务模块 import 之前配置日志
setup_logging(service_name="worker")

from app.config import get_settings
from app.pipeline.factory import create_pipeline
from app.pipeline.queue import TaskQueue
from app.pipeline.worker import PipelineWorker
from app.startup import configure_thread_pool, load_embed_configs
from app.storage.database import async_session, init_db

logger = logging.getLogger("worker_main")

# 优雅关闭超时（秒），超时后强制退出
_SHUTDOWN_TIMEOUT = 60


async def main():
    """Worker 主函数"""
    settings = get_settings()

    # 设置线程池上限（在任何 asyncio.to_thread 调用之前）
    configure_thread_pool()

    print("=" * 50)
    print("[Worker] Pipeline Worker 独立进程启动")
    print(f"[Worker] max_concurrent={settings.pipeline_max_concurrent}, "
          f"max_retries={settings.pipeline_max_retries}")
    print(f"[Worker] embed_batch_size={settings.pipeline_embed_batch_size}, "
          f"embed_concurrency={settings.pipeline_embed_concurrency}")
    print(f"[Worker] task_timeout={settings.pipeline_task_timeout_minutes}min, "
          f"circuit_breaker={settings.pipeline_circuit_breaker_threshold}")
    print(f"[Worker] slow_lane_min_mb={settings.pipeline_slow_lane_min_mb}, "
          f"slow_max_concurrent={settings.pipeline_slow_max_concurrent}")
    print(f"[Worker] db_pool={settings.db_pool_size}+{settings.db_max_overflow}, "
          f"thread_pool_max_workers={settings.thread_pool_max_workers or 'default'}")
    print("=" * 50)

    # 初始化数据库（确保表存在 + migration）
    await init_db()

    # 会话文件异步上传新列迁移（progress / progress_message / error_message）
    from app.startup import _auto_migrate_session_file_columns
    await _auto_migrate_session_file_columns()

    # 幂等建好 Milvus 的两个物理 collection（Worker 可能先于 API 起来，各自幂等）
    from app.startup import init_milvus_collections
    await init_milvus_collections()

    # 加载 Embedding/Rerank 配置（与 API 共用逻辑）
    await load_embed_configs()

    # 连接 Redis（快道）
    task_queue = await TaskQueue.create(settings.redis_url)
    if task_queue is None:
        print("[Worker] ❌ Redis 不可用，Worker 无法启动")
        sys.exit(1)

    # 慢道队列（大文件）：独立 stream + consumer group，与快道物理隔离，
    # 由同一个 Worker 进程消费，但受 slow_max_concurrent 限制在途数。
    slow_queue = await TaskQueue.create(
        settings.redis_url,
        stream_key="pipeline:tasks:slow",
        dlq_key="pipeline:dlq",
        group_name="pipeline-workers",
    )

    # Durable document-summary lane: independent stream/group/worker so model
    # latency cannot block indexing.
    from app.summary.service import initialize_document_summary_queue

    summary_queue = await initialize_document_summary_queue(settings.redis_url)

    # 创建 Pipeline（通过工厂函数统一组装依赖）
    pipeline = await create_pipeline()

    # 创建 Worker
    worker = PipelineWorker(
        queue=task_queue,
        pipeline=pipeline,
        db_session_factory=async_session,
        max_concurrent=settings.pipeline_max_concurrent,
        max_retries=settings.pipeline_max_retries,
        slow_queue=slow_queue,
        slow_max_concurrent=settings.pipeline_slow_max_concurrent,
    )

    # 启动跨进程失效广播（InvalidationBus）—— M1/M2/M7 多进程热生效
    from app.startup import start_invalidation_bus
    from app.retrieval.cache import get_retrieval_cache
    from app.storage.milvus import get_milvus_client
    from sqlalchemy import select

    async def _handle_kb_data(kb_id: str):
        """收到 kb_data 失效信号：清除对应知识库的 Milvus 加载缓存 + 检索结果缓存"""
        # 单 collection + Partition Key 拓扑下会清掉承载该知识库的物理 collection 的
        # 全局加载标记——宁可多 load 不可漏 load（漏 load 会读到旧快照）。
        get_milvus_client().invalidate_load_cache(kb_id)
        cache = await get_retrieval_cache()
        if cache:
            await cache.invalidate_kb(kb_id)
        logger.info("InvalidationBus: kb_data 失效完成 kb_id=%s", kb_id)

    async def _handle_tenant_config(tenant_id: str):
        """收到 tenant_config 失效信号（M1 + M7）：
        1. 失效该租户的检索配置缓存（M1 多进程热生效）
        2. 失效该租户名下所有 KB 的检索结果缓存（M7 配置变更失效结果缓存）
        """
        from app.retrieval.config import get_retrieval_config_store
        store = get_retrieval_config_store()
        store.invalidate(tenant_id)

        # M7: 额外失效该租户名下 KB 的检索结果缓存
        cache = await get_retrieval_cache()
        kb_ids: list[str] = []
        if cache:
            kb_ids = await _get_tenant_kb_ids(tenant_id)
            for kb_id in kb_ids:
                await cache.invalidate_kb(kb_id)
        logger.info(
            "InvalidationBus: tenant_config 失效完成 tenant_id=%s, 失效 %d 个 KB 缓存",
            tenant_id, len(kb_ids),
        )

    async def _get_tenant_kb_ids(tenant_id: str) -> list[str]:
        """查询数据库获取该租户名下的所有 kb_id（轻量 select 仅取 id 列）"""
        try:
            from app.schema.db import KnowledgeBase
            async with async_session() as session:
                result = await session.execute(
                    select(KnowledgeBase.id).where(KnowledgeBase.tenant_id == tenant_id)
                )
                return [row[0] for row in result.all()]
        except Exception as e:
            logger.warning("查询租户 %s KB 列表失败（跳过结果缓存失效）: %s", tenant_id, e)
            return []

    async def _handle_capability_config(capability: str):
        """收到 capability_config 失效信号：重载 Worker 持有的能力运行时对象

        - embedding / rerank：重载 ModelManager 单例（走 load_embed_configs）。
        - ocr / asr：对 pipeline 持有的 Manager 调 reload_from_configs 原子替换内部
          Provider 集合。Manager 实例对象不变，故无需重建 pipeline；正在处理中的
          文档继续用旧 Provider 跑完，新任务用新配置。
        """
        from app.api.capability_reload import (
            CAPABILITY_ASR,
            CAPABILITY_OCR,
            reload_capability_locally,
        )
        from app.startup import load_asr_configs, load_ocr_configs

        try:
            if capability == CAPABILITY_OCR:
                if pipeline.ocr_manager is None:
                    logger.warning("capability_config: pipeline 无 OCR Manager，跳过重载")
                else:
                    pipeline.ocr_manager.reload_from_configs(await load_ocr_configs())
            elif capability == CAPABILITY_ASR:
                if pipeline.asr_manager is None:
                    logger.warning("capability_config: pipeline 无 ASR Manager，跳过重载")
                else:
                    pipeline.asr_manager.reload_from_configs(await load_asr_configs())
            else:
                # embedding / rerank 走共用的本地重载逻辑
                await reload_capability_locally(capability)
        except Exception as e:  # noqa: BLE001 — 重载失败不能打断 worker 消费
            logger.warning("capability_config 重载失败 capability=%s: %s", capability, e)
            return

        logger.info("InvalidationBus: capability_config 处理完成 capability=%s", capability)

    await start_invalidation_bus({
        "kb_data": _handle_kb_data,
        "tenant_config": _handle_tenant_config,
        "capability_config": _handle_capability_config,
    })

    # 会话文件异步上传 worker（REQ-2 / REQ-4 / REQ-9）：与文档入库快/慢道队列物理隔离，
    # 消费独立 stream ``session_upload:tasks``，后台建索引并 publish 状态事件。
    # 与文档 worker 一样在独立事件循环任务中运行，互不阻塞。
    session_worker = None
    session_worker_task = None

    from app.session_upload.events import init_session_upload_event_bus
    from app.session_upload.queue import SessionUploadQueue, set_session_upload_queue
    from app.session_upload.service import get_session_upload_service
    from app.session_upload.worker import SessionUploadWorker

    session_upload_queue = await SessionUploadQueue.create(settings.redis_url)
    if session_upload_queue is None:
        # Redis 前面已校验可用（task_queue is None → sys.exit(1)），理论上不会到这；
        # 仍防御式跳过，避免 worker 进程因会话上传队列缺失而无法启动。
        print("[Worker] ⚠️ 会话上传队列不可用（Redis），跳过会话上传 worker")
        logger.warning("SessionUploadQueue unavailable, skipping session upload worker")
    else:
        # 注入进程内单例，使 SessionUploadService 惰性解析到同一队列实例。
        set_session_upload_queue(session_upload_queue)
        # worker 进程只 publish 事件（不订阅、不持有 EventHub）→ local_hub=None。
        # Redis 可用则跨进程广播；不可用则 publish 退化为记 WARNING 的 no-op。
        await init_session_upload_event_bus(local_hub=None)
        # 服务单例的 queue/event_bus 属性此时解析到上面刚注入的进程单例，
        # process_task 的 publish 走 worker 侧事件总线。
        session_service = get_session_upload_service()
        # 并发/重试/超时等配置由 worker 内部防御式 getattr 读取（任务 9.1 补齐配置字段）。
        session_worker = SessionUploadWorker(
            queue=session_upload_queue,
            service=session_service,
            db_session_factory=async_session,
        )
        session_worker_task = asyncio.create_task(session_worker.start())
        print("[Worker] 📤 会话文件异步上传 worker 已启动")

    summary_worker = None
    summary_worker_task = None
    if summary_queue is not None:
        from app.api.chat import _get_llm_for_request
        from app.summary.worker import DocumentSummaryWorker

        summary_llm, _stream, _max_ctx = await _get_llm_for_request(None)
        summary_worker = DocumentSummaryWorker(
            queue=summary_queue,
            llm=summary_llm,
            max_concurrent=2,
        )
        summary_worker_task = asyncio.create_task(summary_worker.start())
        print("[Worker] 📝 文档摘要制品 worker 已启动")
    else:
        print("[Worker] ⚠️ 文档摘要队列不可用，跳过摘要 worker")

    # 知识图谱抽取慢道 worker（仅 graph_enable 开启时启动，避免未启用成本 —— Req 9.3）。
    # 独立队列 + 独立并发信号量，与文档入库 worker 物理隔离，绝不挤占主链路（Req 1.2）。
    graph_worker = None
    graph_worker_task = None
    graph_housekeeping_task = None
    graph_housekeeping_stop = None
    if settings.graph_enable:
        from app.pipeline.graph.cleanup import run_graph_housekeeping_loop
        from app.pipeline.graph.trigger import create_graph_queue
        from app.pipeline.graph.worker import GraphExtractWorker
        from app.storage.graph_store import get_graph_store

        graph_queue = await create_graph_queue(settings.redis_url)
        graph_store = await get_graph_store()
        if graph_queue is None:
            print("[Worker] ⚠️ GRAPH_ENABLE=true 但 Redis 慢道队列不可用，跳过图谱 worker")
        elif graph_store is None:
            print("[Worker] ⚠️ GRAPH_ENABLE=true 但 Neo4j 不可用，跳过图谱 worker")
        else:
            # 事件中心图谱：注入 Milvus 事件向量集合存储（双写 Neo4j + Milvus）。
            # 取单例失败时传 None，worker 内事件向量写入降级（仅写 Neo4j 事件节点）。
            try:
                from app.storage.milvus_event_store import get_milvus_event_store
                graph_event_store = get_milvus_event_store()
            except Exception as e:  # noqa: BLE001
                print(f"[Worker] ⚠️ 事件向量集合存储不可用，事件向量写入将降级：{e}")
                graph_event_store = None
            graph_worker = GraphExtractWorker(
                queue=graph_queue,
                store=graph_store,
                db_session_factory=async_session,
                event_store=graph_event_store,
                max_concurrent=settings.graph_extract_concurrency,
                max_retries=settings.graph_extract_max_retries,
            )
            # 与主入库 worker 并行运行（独立事件循环任务），互不阻塞。
            graph_worker_task = asyncio.create_task(graph_worker.start())
            print(f"[Worker] 🕸️ 知识图谱抽取 worker 已启动（并发={settings.graph_extract_concurrency}）")

        # housekeeping 巡检：周期性把卡死（worker 硬崩溃 pending_subtasks 不归零）的
        # GraphExtractJob 置 failed 并零化计数器（Req 4.4）。即使 Neo4j 不可用也启动——
        # 它只读写 PG 台账，不依赖 Neo4j，保证卡死 job 总能到达终态。
        if graph_queue is not None:
            graph_housekeeping_stop = asyncio.Event()
            graph_housekeeping_task = asyncio.create_task(
                run_graph_housekeeping_loop(async_session, stop_event=graph_housekeeping_stop)
            )
            print(f"[Worker] 🧹 知识图谱 housekeeping 巡检已启动"
                  f"（间隔={settings.graph_housekeeping_interval_seconds}s, "
                  f"超时阈值={settings.graph_job_timeout_minutes}min）")

    # 优雅关闭（仅 Unix 支持 signal handler）
    if sys.platform != "win32":
        loop = asyncio.get_event_loop()

        def _signal_handler():
            print("\n[Worker] 收到停止信号，正在优雅关闭...")

            async def _graceful_shutdown():
                try:
                    await asyncio.wait_for(worker.stop(), timeout=_SHUTDOWN_TIMEOUT)
                    if summary_worker is not None:
                        await asyncio.wait_for(summary_worker.stop(), timeout=_SHUTDOWN_TIMEOUT)
                    if session_worker is not None:
                        await asyncio.wait_for(session_worker.stop(), timeout=_SHUTDOWN_TIMEOUT)
                    if graph_worker is not None:
                        await asyncio.wait_for(graph_worker.stop(), timeout=_SHUTDOWN_TIMEOUT)
                    if graph_housekeeping_stop is not None:
                        graph_housekeeping_stop.set()
                except asyncio.TimeoutError:
                    print(f"[Worker] ⚠️ 优雅关闭超时（{_SHUTDOWN_TIMEOUT}s），强制退出")
                    logger.warning("Graceful shutdown timed out after %ds", _SHUTDOWN_TIMEOUT)

            asyncio.create_task(_graceful_shutdown())

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

    # 启动 Worker
    await worker.start()

    # 主 worker 循环退出后，停止会话上传 worker 并回收其后台任务。
    if summary_worker is not None:
        await summary_worker.stop()
    if summary_worker_task is not None:
        await asyncio.gather(summary_worker_task, return_exceptions=True)
    if session_worker is not None:
        await session_worker.stop()
    if session_worker_task is not None:
        await asyncio.gather(session_worker_task, return_exceptions=True)

    # 主 worker 循环退出后，停止图谱 worker 并回收其后台任务。
    if graph_worker is not None:
        await graph_worker.stop()
    if graph_worker_task is not None:
        await asyncio.gather(graph_worker_task, return_exceptions=True)
    # 停止 housekeeping 巡检循环并回收。
    if graph_housekeeping_stop is not None:
        graph_housekeeping_stop.set()
    if graph_housekeeping_task is not None:
        await asyncio.gather(graph_housekeeping_task, return_exceptions=True)

    print("[Worker] Worker 已停止")


if __name__ == "__main__":
    asyncio.run(main())
