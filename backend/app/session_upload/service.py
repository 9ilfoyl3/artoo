"""会话级文件上传核心服务（design C4 / Task 7）

职责：
- ``upload``: 同步建会话文件索引（文件大小校验 → 累计文件数校验 → pipeline 解耦单元
  Load/OCR/Clean/Chunk → Pre_Embed_Gate → Embed → 写共享 collection ``kb_session_files``
  + ``session_files`` / ``session_chunks`` 关系表）。失败清理已部分写入向量与回滚 DB
  事务，不留孤儿（Req 1.10）。
- ``list_files`` / ``has_files`` / ``used_chunks`` / ``used_files``: 会话维度查询
  （配额聚合 / 检索接入判断 / 文件列表展示）。
- ``remove_file``: 单文件移除——按 ``doc_id == file_id`` 删向量 + 删 ``session_files``
  / ``session_chunks`` 行，释放配额（Req 1.8 / 6.7）。
- ``cleanup_session_files``: 删会话级联清理——按 ``session_id`` 删共享库向量 +
  ``session_chunks`` 行（``session_files`` 由 FK CASCADE 删）。失败仅记 WARNING、
  不阻塞会话删除主流程（Req 1.6）。

向量入共享 collection ``kb_session_files``，每条带 ``session_id`` 标量字段做会话级隔离
（参考 WeKnora 的"共享 collection + 标量过滤"模式）；``file_id`` 复用为 Milvus
``doc_id``，使 ``delete_by_doc_id`` 能精准移除单文件向量。删向量后 publish
``InvalidationBus`` 失效 ``kb_session_files`` 的 ``_loaded_at``（复用 bugfix
``retrieval-pipeline-hardening`` 的失效广播基础设施），避免其他 API 进程在
``load_cache_ttl`` 内仍读到已删向量。

embedding 走 ``DocumentPipeline`` 的全局信号量；上传文件同时落盘到
``data/uploads/sessions/`` 临时区（仅供同步建索引使用，处理完即删）并持久化到 MinIO
（``sessions/{session_id}/{file_id}.{ext}``）以支持后续原件预览/下载。删单文件 / 删会话
时同步清理 MinIO 对象。
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import delete as sa_delete, func, select

from app.api.errors import EmptyDocumentContentError, FileTooLargeError
from app.pipeline.factory import create_pipeline
from app.schema.db import SessionChunk, SessionFile
from app.session_upload.events import (
    get_session_upload_event_bus,
    make_completed,
    make_failed,
    make_processing,
    make_progress,
    make_queued,
    make_removed,
)
from app.session_upload.queue import (
    SessionUploadTask,
    get_session_upload_queue,
)
from app.storage.database import async_session
from app.storage.invalidation import get_invalidation_bus
from app.storage.milvus import SESSION_FILES_KB_ID, get_milvus_client
from app.storage.object_store import (
    get_object_store,
    materialized_file,
    session_file_object_key,
    session_prefix,
)

if TYPE_CHECKING:
    from app.session_upload.limits import UploadLimits

logger = logging.getLogger(__name__)


# ============================================================
# 模块常量（避免魔法值）
# ============================================================

# 会话上传文件落盘目录：与 KB 上传 ``data/uploads`` 同根、子目录隔离。文件仅在同步建索引
# 期间需要（向量与 chunk 文本已落库），处理完毕（成功 / 失败）即删除。
_SESSION_UPLOAD_DIR = Path("data/uploads/sessions")

# Milvus 单批写入上限：与正式 KB 路径（pipeline.py）保持一致，避免一次性 RPC 过大。
_MILVUS_INSERT_BATCH_SIZE = 1000

# 失败时写入 SessionFile.error_message 的最大长度（避免超长异常文本撑爆列 / 前端展示）。
_ERROR_MESSAGE_MAX_LEN = 2000


# ============================================================
# 视图对象
# ============================================================


@dataclass(frozen=True)
class SessionFileVO:
    """会话文件视图对象（VO，对外暴露的会话文件元数据快照）。"""

    id: str
    session_id: str
    filename: str
    file_type: str | None
    file_size: int | None
    chunk_count: int
    status: str
    created_at: datetime
    # REQ-8：轮询兜底 / WS 断线重连对账需要的最新建索引进展字段。
    progress: int = 0
    progress_message: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class SessionFileWithContentVO:
    """会话文件元数据 + 解析后原文文本的组合 VO。

    供「带原文内容的附件列表」使用：在 ``SessionFileVO`` 全量元数据之外，附带该文件的
    解析原文（``content``，父块按 ``chunk_index`` 有序拼接）与父块粒度文本列表
    （``chunks``）。与仅返回元数据的 ``list_files`` 区分。
    """

    file: SessionFileVO
    content: str
    chunks: list[str]


def _row_to_vo(row: SessionFile) -> SessionFileVO:
    """ORM ``SessionFile`` -> ``SessionFileVO`` 的轻量映射（避免散落字段拷贝）。"""
    return SessionFileVO(
        id=row.id,
        session_id=row.session_id,
        filename=row.filename,
        file_type=row.file_type,
        file_size=row.file_size,
        chunk_count=row.chunk_count,
        status=row.status,
        created_at=row.created_at,
        progress=row.progress,
        progress_message=row.progress_message,
        error_message=row.error_message,
    )


# ============================================================
# 服务核心
# ============================================================


class SessionUploadService:
    """会话级文件上传：建索引 / 列出 / 移除 / 删会话级联清理。

    无状态（不持额外缓存），依赖项均通过构造器注入便于测试；默认依赖经默认参数解析为
    工程内单例（``async_session`` / ``get_milvus_client`` / ``create_pipeline``）。
    """

    def __init__(
        self,
        *,
        milvus_client=None,
        db_session_factory=None,
        pipeline_factory=None,
        queue=None,
        event_bus=None,
    ):
        # 延迟解析 Milvus 单例：测试场景未配置 Milvus 时不阻塞导入。
        self._milvus = milvus_client
        self._db_session_factory = db_session_factory or async_session
        self._pipeline_factory = pipeline_factory or create_pipeline
        # 队列 / 事件总线均惰性解析为进程单例（测试可显式注入）。
        self._queue = queue
        self._event_bus = event_bus

    @property
    def milvus(self):
        """惰性获取 Milvus 客户端（避免模块导入期触达 Milvus 配置）。"""
        if self._milvus is None:
            self._milvus = get_milvus_client()
        return self._milvus

    @property
    def queue(self):
        """惰性获取会话上传队列单例（未初始化 / Redis 不可用时为 None）。

        每次读取都回落到 ``get_session_upload_queue()``，以便启动期（任务 8）
        注入队列后，先前构造的服务实例也能拿到最新单例。
        """
        if self._queue is not None:
            return self._queue
        return get_session_upload_queue()

    @property
    def event_bus(self):
        """惰性获取会话上传事件总线单例（未初始化时为 None）。"""
        if self._event_bus is not None:
            return self._event_bus
        return get_session_upload_event_bus()

    # ------------------------------------------------------------------
    # 查询接口（轻量 SELECT，供配额校验 / 检索接入 / 文件列表展示）
    # ------------------------------------------------------------------

    async def has_files(self, session_id: str) -> bool:
        """该会话是否已上传文件（供检索决定是否追加会话源 cfg，design C7）。"""
        async with self._db_session_factory() as session:
            count = await session.scalar(
                select(func.count(SessionFile.id)).where(
                    SessionFile.session_id == session_id
                )
            )
            return bool(count or 0)

    async def used_chunks(self, session_id: str) -> int:
        """该会话累计 child chunk 数 = ``session_files.chunk_count`` 之和（Req 6.4）。

        移除文件即删 ``session_files`` 行，配额自动释放（Req 6.7）。
        """
        async with self._db_session_factory() as session:
            result = await session.execute(
                select(func.coalesce(func.sum(SessionFile.chunk_count), 0)).where(
                    SessionFile.session_id == session_id
                )
            )
            return int(result.scalar_one() or 0)

    async def used_files(self, session_id: str) -> int:
        """该会话已用文件数（查询接口，供文件列表展示/调试用）。"""
        async with self._db_session_factory() as session:
            count = await session.scalar(
                select(func.count(SessionFile.id)).where(
                    SessionFile.session_id == session_id
                )
            )
            return int(count or 0)

    async def list_files(self, session_id: str) -> list[SessionFileVO]:
        """列出会话已上传文件（Req 1.8），按上传时间倒序（最新在前）。"""
        async with self._db_session_factory() as session:
            result = await session.execute(
                select(SessionFile)
                .where(SessionFile.session_id == session_id)
                .order_by(SessionFile.created_at.desc())
            )
            return [_row_to_vo(row) for row in result.scalars().all()]

    async def list_files_with_content(
        self, session_id: str
    ) -> list[SessionFileWithContentVO]:
        """列出会话已上传文件，**每个文件附带解析后原文文本**（按上传时间倒序）。

        与 ``list_files``（仅元数据）区分：额外返回每个文件的解析原文（父块按
        ``chunk_index`` 有序拼接）与父块文本列表。采用「一次查文件 + 一次批量查父块 +
        内存分组」避免 N+1；父块按 ``chunk_index`` 排序，分组时各文件内部相对顺序稳定，
        故各文件 ``chunks`` 有序。

        文件尚未建索引完成（``status != completed``）时其父块可能为空，对应 ``content``
        为空串、``chunks`` 为空列表。
        """
        async with self._db_session_factory() as session:
            result = await session.execute(
                select(SessionFile)
                .where(SessionFile.session_id == session_id)
                .order_by(SessionFile.created_at.desc())
            )
            files = result.scalars().all()
            if not files:
                return []

            file_ids = [f.id for f in files]
            chunk_result = await session.execute(
                select(SessionChunk)
                .where(
                    SessionChunk.file_id.in_(file_ids),
                    SessionChunk.parent_id.is_(None),
                )
                .order_by(SessionChunk.chunk_index)
            )
            chunks_by_file: dict[str, list[str]] = {}
            for c in chunk_result.scalars().all():
                chunks_by_file.setdefault(c.file_id, []).append(c.content)

            return [
                SessionFileWithContentVO(
                    file=_row_to_vo(f),
                    content="\n\n".join(chunks_by_file.get(f.id, [])),
                    chunks=chunks_by_file.get(f.id, []),
                )
                for f in files
            ]

    async def get_file_raw(
        self, *, session_id: str, file_id: str
    ) -> tuple[bytes, str, str]:
        """读取会话文件原件（供预览/下载）。

        Returns:
            (内容字节, 文件名, 文件类型扩展名)。

        Raises:
            ValueError: 文件不存在 / 不属于该会话。
            RuntimeError: 对象存储不可用。
            FileNotFoundError: 对象已丢失（DB 有行但 MinIO 无对象）。
        """
        async with self._db_session_factory() as session:
            row = await session.get(SessionFile, file_id)
            if row is None or row.session_id != session_id:
                raise ValueError("文件不存在或不属于该会话")
            filename = row.filename
            ext = (row.file_type or "").lower()

        store = get_object_store()
        if store is None:
            raise RuntimeError("对象存储不可用")
        key = session_file_object_key(session_id, file_id, ext)
        if not await store.exists(key):
            raise FileNotFoundError("原始文件不存在")
        data = await store.get_bytes(key)
        return data, filename, ext

    # ------------------------------------------------------------------
    # 写路径
    # ------------------------------------------------------------------

    async def enqueue_upload(
        self,
        *,
        session_id: str,
        tenant_id: str | None,
        owner_user_id: str | None,
        filename: str,
        content: bytes,
        limits: "UploadLimits",
    ) -> SessionFileVO:
        """API 侧秒回路径：入队后台建索引，立即返回 ``queued`` 状态的 VO。

        流程（design C7 / REQ-1）：
        1. 文件大小闸门：超限抛 ``FileTooLargeError``（路由转 413），不入队 / 不留 MinIO。
        2. 解析队列单例：不可用直接抛 ``RuntimeError``（路由转 503）——在触达 MinIO / DB
           **之前**快速失败，保证不留任何残留（快速失败，无进程内降级）。
        3. 存 MinIO 原件（对象存储不可用抛 ``RuntimeError`` → 503）。
        4. 建 ``SessionFile(status='queued', chunk_count=0, progress=0)`` 行。
        5. ``queue.enqueue(SessionUploadTask)``；入队失败 → 删 DB 行 + 删 MinIO 原件
           后重新抛出（不留孤儿：无 DB 行、无 MinIO 残留）。
        6. publish ``queued`` 事件（best-effort，失败仅 WARNING，不阻塞请求）。
        7. 回读并返回 VO。

        不落盘本地临时文件——worker 从 MinIO 按 ``object_key`` 下载原件处理。

        Raises:
            FileTooLargeError: 文件大小超 ``upload_max_file_bytes``（413）。
            RuntimeError: 队列不可用 / 对象存储不可用 / 入队失败（503）。
        """
        # 1) 文件大小闸门（零解析成本拒绝超限文件；不入队、不留 MinIO）
        file_size = len(content)
        if file_size > limits.upload_max_file_bytes:
            raise FileTooLargeError.from_limit(limits.upload_max_file_bytes)

        # 2) 队列可用性检查——在触达 MinIO / DB 之前快速失败，确保无残留。
        #    队列为进程级资源（main.py lifespan 注入）；未就绪 / Redis 不可用 → 503。
        queue = self.queue
        if queue is None:
            raise RuntimeError("会话上传队列不可用，请稍后重试")

        # 3) 存 MinIO 原件（权威长期存储，worker 从此下载）
        ext = Path(filename).suffix.lstrip(".").lower()
        file_id = str(uuid.uuid4())
        object_key = session_file_object_key(session_id, file_id, ext)
        store = get_object_store()
        if store is None:
            raise RuntimeError("对象存储不可用，无法保存会话附件")
        await store.put_bytes(object_key, content)

        # 4) 建 SessionFile(queued) 行
        try:
            async with self._db_session_factory() as session:
                session.add(
                    SessionFile(
                        id=file_id,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        filename=filename,
                        file_type=ext,
                        file_size=file_size,
                        chunk_count=0,
                        status="queued",
                        progress=0,
                    )
                )
                await session.commit()
        except Exception:
            # 建行失败：清理已存 MinIO 原件，不留孤儿。
            await self._safe_remove_object(object_key)
            raise

        # 5) 入队；失败 → 删 DB 行 + 删 MinIO 原件后重新抛出（不留孤儿）
        task = SessionUploadTask(
            file_id=file_id,
            session_id=session_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            object_key=object_key,
            ext=ext,
            filename=filename,
        )
        try:
            await queue.enqueue(task)
        except Exception:
            await self._safe_delete_file_row(file_id)
            await self._safe_remove_object(object_key)
            raise

        # 6) publish queued 事件（best-effort：失败仅 WARNING，不阻塞秒回请求）
        bus = self.event_bus
        if bus is not None:
            try:
                await bus.publish(
                    make_queued(session_id, file_id, filename=filename)
                )
            except Exception as e:
                logger.warning(
                    "会话文件 %s queued 事件发布失败（非致命）: %s", file_id, e
                )

        # 7) 回读最新 SessionFile（取 server_default 时间字段填充 VO）
        return await self._fetch_one(file_id)

    async def upload(
        self,
        *,
        session_id: str,
        tenant_id: str | None,
        owner_user_id: str | None,
        filename: str,
        content: bytes,
        limits: "UploadLimits",
    ) -> SessionFileVO:
        """同步建会话文件索引。

        流程：
        1. 文件大小校验：零解析成本拒绝超限文件。
        2. 落盘到临时区。
        3. pipeline.process_to_vectors(source_kind="session")：内部 Pre_Embed_Gate
           按 kb_chunk_cap 在 Embed 之前精确判定（临时文件 = 会话级 KB，统一 chunk 闸门）。
        4. ensure_session_files_collection（幂等）。
        5. 写 Milvus（每条带 session_id 标量、doc_id == file_id）。
        6. 写 session_files + session_chunks（单事务）。
        7. publish 失效广播。

        Raises:
            FileTooLargeError: 文件大小超 upload_max_file_bytes。
            UploadCapExceeded: Chunk 数超 kb_chunk_cap（Pre_Embed_Gate）。
            EmptyDocumentContentError: 无可提取文本 / 切分后零 chunk。
        """
        # 1) 文件大小校验
        file_size = len(content)
        if file_size > limits.upload_max_file_bytes:
            raise FileTooLargeError.from_limit(limits.upload_max_file_bytes)

        # 2) 落盘到临时区（处理完毕 finally 中删除）+ 持久化到 MinIO（保留原件）
        ext = Path(filename).suffix.lstrip(".").lower()
        file_id = str(uuid.uuid4())
        _SESSION_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        save_path = _SESSION_UPLOAD_DIR / f"{file_id}.{ext}"
        save_path.write_bytes(content)

        # 持久化原件到 MinIO（权威长期存储，供后续预览/下载）。失败则整体失败：
        # 会话附件需保留，存不进对象存储应让用户知晓而非静默丢原件。
        object_key = session_file_object_key(session_id, file_id, ext)
        store = get_object_store()
        if store is None:
            self._cleanup_temp_file(save_path)
            raise RuntimeError("对象存储不可用，无法保存会话附件")
        await store.put_bytes(object_key, content)

        try:
            # 4) Pipeline 不依赖 Document 表的同步处理单元（Load→OCR→Clean→Chunk→
            #    Pre_Embed_Gate→Embed）。Pre_Embed_Gate 内部用精确 child chunk 数
            #    + 该会话已用 chunk 累计判定，超限抛 UploadCapExceeded（不写向量）。
            pipeline = await self._pipeline_factory()
            processed = await pipeline.process_to_vectors(
                str(save_path),
                source_kind="session",
                source_id=session_id,
                tenant_id=tenant_id,
                limits=limits,
            )

            # 4–7) 组装 + 写 Milvus + 写 session_files/session_chunks（共享 helper）
            #      同步路径新建 SessionFile 行（create_row=True）。
            await self._build_and_write_index(
                processed=processed,
                file_id=file_id,
                session_id=session_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                filename=filename,
                ext=ext,
                file_size=file_size,
                create_row=True,
            )

            # 8) 失效广播：让其他 API 进程清 ``kb_session_files`` 的 ``_loaded_at``
            #    （main.py 的 _handle_kb_data 已绑定该 collection 的失效逻辑）。
            await self._publish_invalidation()

            # 回读最新 SessionFile（取 server_default 时间字段填充 VO）
            return await self._fetch_one(file_id)

        except Exception:
            # 建索引 / 写库失败：原件对象不应残留（DB 无对应 SessionFile 行）。
            await self._safe_remove_object(object_key)
            raise
        finally:
            # 临时文件无论成败均清理（向量与 chunk 文本已落库，原文件已存 MinIO）
            self._cleanup_temp_file(save_path)

    async def _build_and_write_index(
        self,
        *,
        processed,
        file_id: str,
        session_id: str,
        tenant_id: str | None,
        owner_user_id: str | None,
        filename: str,
        ext: str,
        file_size: int | None,
        create_row: bool,
    ) -> int:
        """组装并写入索引（共享给同步 ``upload`` 与异步 ``process_task``）。

        步骤（与原 ``upload`` 内联逻辑等价，抽出以复用、避免重复大段代码）：
        1. 空文档兜底：``child_count == 0`` → ``EmptyDocumentContentError``。
        2. ``ensure_session_files_collection``（幂等建表）。
        3. 组装 Milvus 数据 + 父/子 ``SessionChunk`` 行（``doc_id == file_id``，
           每条带 ``session_id`` 标量做会话隔离）。
        4. 写 Milvus（批量；失败按 ``doc_id`` 清理并传播）。
        5. 写 ``session_files`` + ``session_chunks``（单事务，先 flush 父行满足外键）：
           - ``create_row=True``（同步路径）：新建 ``SessionFile(completed)`` 行。
           - ``create_row=False``（异步路径）：更新既有 ``queued`` 行为 ``completed``
             + 回填 ``chunk_count`` / ``progress=100``。

        Args:
            create_row: True 新建 SessionFile 行（同步路径）；False 更新既有 queued 行
                （worker 路径，行由 ``enqueue_upload`` 预先创建）。

        Returns:
            真实 child chunk 数（``chunk_count``）。

        Raises:
            EmptyDocumentContentError: 切分后零 child chunk。
        """
        child_count = len(processed.enriched_children)
        if child_count == 0:
            # 防御性兜底：pipeline 通常已对空内容 raise；走到此处说明切分后零 child
            # chunk（极端边界），无可建索引内容，统一以 422 优雅提示（非 500）。
            raise EmptyDocumentContentError()

        # 共享 collection 幂等建表（首个会话上传时创建）
        await self.milvus.ensure_session_files_collection()

        # 组装 Milvus 数据 + 父/子 SessionChunk 行
        #   parent SessionChunk 行（parent_id=None）供检索后父块扩展取内容；
        #   child SessionChunk 行（parent_id=parent_uuid）与 Milvus 子块一一对应。
        chunk_result = processed.chunk_result
        metadata_list = processed.metadata_list
        embed_result = processed.embed_result
        child_to_parent = processed.child_to_parent

        doc_title = Path(filename).stem  # BM25 content 前缀（与 KB 路径对齐）
        parent_ids: list[str] = [
            str(uuid.uuid4()) for _ in chunk_result.parent_chunks
        ]

        parent_chunk_records: list[SessionChunk] = [
            SessionChunk(
                id=pid,
                file_id=file_id,
                session_id=session_id,
                tenant_id=tenant_id,
                parent_id=None,
                content=ptext,
                chunk_index=idx,
            )
            for idx, (pid, ptext) in enumerate(
                zip(parent_ids, chunk_result.parent_chunks)
            )
        ]

        child_chunk_records: list[SessionChunk] = []
        milvus_data: list[dict] = []
        for child_idx, child_text in enumerate(processed.enriched_children):
            child_id = str(uuid.uuid4())
            parent_idx = child_to_parent.get(child_idx)
            parent_id = parent_ids[parent_idx] if parent_idx is not None else None

            meta = metadata_list[child_idx]
            child_chunk_records.append(
                SessionChunk(
                    id=child_id,
                    file_id=file_id,
                    session_id=session_id,
                    tenant_id=tenant_id,
                    parent_id=parent_id,
                    content=child_text,
                    chunk_index=child_idx,
                )
            )

            # BM25 content 增强：加文件名前缀（与 pipeline.py KB 路径对齐，帮助
            # BM25 / Rerank 区分同结构文档）。Dense embedding 不受影响（已用原文）。
            # 字节截断由 ``MilvusClient._insert_sync`` 内部统一处理，不在此重复。
            enhanced_content = (
                f"[{doc_title}] {child_text}" if doc_title else child_text
            )
            milvus_data.append({
                "chunk_id": child_id,
                # file_id 复用为 doc_id：使 delete_by_doc_id 能精准移除单文件向量（Req 1.8）
                "doc_id": file_id,
                "content": enhanced_content,
                "dense_vector": embed_result.dense_vectors[child_idx],
                "sparse_vector": embed_result.sparse_vectors[child_idx],
                "parent_id": parent_id or "",
                "chunk_index": child_idx,
                "file_type": meta.file_type,
                "element_type": meta.element_type,
                # 会话向量集合的 Partition Key：既是隔离条件也是分区裁剪条件（Req 1.11）。
                # 缺失会被 MilvusClient._stamp_records 判为非法写入并抛错。
                "session_id": session_id,
                # 租户维度（不参与鉴权）：仅供运维排障与按租户统计/清理。
                "tenant_id": tenant_id or "",
            })

        # 写 Milvus（批量；任一阶段失败清理 + 传播）
        try:
            if len(milvus_data) <= _MILVUS_INSERT_BATCH_SIZE:
                await self.milvus.insert(SESSION_FILES_KB_ID, milvus_data)
            else:
                for offset in range(0, len(milvus_data), _MILVUS_INSERT_BATCH_SIZE):
                    await self.milvus.insert(
                        SESSION_FILES_KB_ID,
                        milvus_data[offset:offset + _MILVUS_INSERT_BATCH_SIZE],
                    )
        except Exception:
            # 部分批次可能已写入；按 doc_id == file_id 清理（幂等）。
            await self._safe_cleanup_vectors(file_id)
            raise

        # 写 SessionFile + SessionChunk（单事务，commit 失败 with 自动回滚）
        #   顺序关键：SessionChunk.file_id 外键指向 session_files.id，PostgreSQL 强制
        #   外键约束，必须先插入/确保父行 SessionFile 再插入子行 SessionChunk，否则触发
        #   IntegrityError（外键违反）。SessionChunk 用裸 file_id 列（无 ORM
        #   relationship），SQLAlchemy 工作单元不会自动为其排序，故显式 add 父行 +
        #   flush 后再 add 子行。（SQLite 默认不强制外键，故单测未暴露此问题。）
        try:
            async with self._db_session_factory() as session:
                if create_row:
                    # 同步路径：新建 completed 行。
                    session.add(
                        SessionFile(
                            id=file_id,
                            session_id=session_id,
                            tenant_id=tenant_id,
                            owner_user_id=owner_user_id,
                            filename=filename,
                            file_type=ext,
                            file_size=file_size,
                            chunk_count=child_count,
                            status="completed",
                            progress=100,
                        )
                    )
                else:
                    # 异步 worker 路径：更新既有 queued 行为 completed + 回填 chunk_count。
                    row = await session.get(SessionFile, file_id)
                    if row is None:
                        # queued 行应存在（enqueue_upload 已建）；缺失属异常，兜底新建。
                        session.add(
                            SessionFile(
                                id=file_id,
                                session_id=session_id,
                                tenant_id=tenant_id,
                                owner_user_id=owner_user_id,
                                filename=filename,
                                file_type=ext,
                                file_size=file_size,
                                chunk_count=child_count,
                                status="completed",
                                progress=100,
                            )
                        )
                    else:
                        row.status = "completed"
                        row.chunk_count = child_count
                        row.progress = 100
                        row.progress_message = None
                        row.error_message = None
                # 先 flush 父行，确保 session_files 行已存在，满足子行外键约束。
                await session.flush()
                for row in parent_chunk_records:
                    session.add(row)
                for row in child_chunk_records:
                    session.add(row)
                await session.commit()
        except Exception:
            # DB 写失败 → Milvus 已写入需清理（with 已回滚 DB 事务）。
            await self._safe_cleanup_vectors(file_id)
            raise

        try:
            from app.summary.service import schedule_document_summary

            await schedule_document_summary(
                source="session-file",
                doc_id=file_id,
                session_id=session_id,
                tenant_id=tenant_id,
            )
        except Exception as exc:  # noqa: BLE001 - summary is best-effort
            logger.warning(
                "会话文件 %s 摘要调度失败（不影响建索引）: %s",
                file_id,
                exc,
            )

        return child_count

    async def process_task(self, task: SessionUploadTask) -> None:
        """worker 侧异步建索引路径（幂等，design C7 / REQ-2 / REQ-3 / REQ-6）。

        流程：
        0. 幂等检查：``SessionFile`` 不存在 → 跳过（文件已被删除）；
           ``status == 'completed'`` → 跳过（重复投递，不覆盖终态、不重复写向量）。
        1. status=processing（DB + publish processing 事件）。
        2. worker 侧重新 ``resolve`` limits（按 ``task.tenant_id``，Pre_Embed_Gate 用最新配置）。
        3. ``materialized_file(object_key)`` 从 MinIO 下载原件到本地临时文件。
        4. ``pipeline.process_to_vectors(source_kind='session')``；各阶段 publish progress。
        5. 幂等清理：``_safe_cleanup_vectors(file_id)``（delete_by_doc_id）后再写 Milvus，
           防重复投递产生重复向量。
        6. ``_build_and_write_index(create_row=False)`` 写 Milvus + 更新既有 queued 行为
           completed + 写 session_chunks。
        7. publish completed + 失效广播。

        异常处理：任一阶段异常 → 置 ``SessionFile.status=failed`` + ``error_message``
        + publish failed，随后**重新抛出**，交由 worker（任务 6）判定可重试 / DLQ。
        说明：即便是可重试错误也先落 failed 终态——worker 重试成功时 ``process_task``
        会再次把行更新为 completed（幂等）；毒消息进 DLQ 时 failed 亦为正确终态
        （REQ-3：不永久停留 processing）。事件推送 / 状态更新失败一律降级为 WARNING，
        不改变建索引成败判定（REQ-9）。
        """
        file_id = task.file_id
        session_id = task.session_id
        filename = task.filename

        # 0) 幂等检查
        async with self._db_session_factory() as session:
            row = await session.get(SessionFile, file_id)
            if row is None:
                logger.info(
                    "会话文件 %s 不存在（可能已被删除），跳过建索引", file_id
                )
                return
            if row.status == "completed":
                logger.info(
                    "会话文件 %s 已 completed，跳过重复建索引（幂等）", file_id
                )
                return

        try:
            # 1) status=processing（DB + 事件）
            await self._update_task_status(
                file_id, status="processing", progress=5,
                progress_message="开始建索引",
            )
            await self._publish_event(
                make_processing(
                    session_id, file_id, filename=filename,
                    stage="load", message="开始建索引", progress=5,
                )
            )

            # 2) worker 侧重新 resolve limits（按 tenant_id，用最新配置判定 Pre_Embed_Gate）
            from app.session_upload.limits import get_upload_limit_resolver

            limits = await get_upload_limit_resolver().resolve(task.tenant_id)

            # 3) 从 MinIO 下载原件到本地临时文件（退出时自动删除）
            suffix = f".{task.ext}" if task.ext else ""
            async with materialized_file(task.object_key, suffix) as local_path:
                # 进入解析阶段进度
                await self._publish_event(
                    make_progress(
                        session_id, file_id, 20, filename=filename,
                        stage="parse", message="正在解析与切分",
                    )
                )

                # 4) pipeline 处理（Load→OCR→Clean→Chunk→Pre_Embed_Gate→Embed）
                pipeline = await self._pipeline_factory()
                processed = await pipeline.process_to_vectors(
                    local_path,
                    source_kind="session",
                    source_id=session_id,
                    tenant_id=task.tenant_id,
                    limits=limits,
                )

                # 进入写索引阶段进度
                await self._update_task_status(
                    file_id, status="processing", progress=80,
                    progress_message="正在写入索引",
                )
                await self._publish_event(
                    make_progress(
                        session_id, file_id, 80, filename=filename,
                        stage="index", message="正在写入索引",
                    )
                )

                # 5) 幂等清理：先按 doc_id 删旧向量，防重复投递产生重复向量。
                await self._safe_cleanup_vectors(file_id)

                # 6) 组装 + 写 Milvus + 更新既有 queued 行为 completed + 写 session_chunks
                chunk_count = await self._build_and_write_index(
                    processed=processed,
                    file_id=file_id,
                    session_id=session_id,
                    tenant_id=task.tenant_id,
                    owner_user_id=task.owner_user_id,
                    filename=filename,
                    ext=task.ext,
                    file_size=None,  # 沿用 queued 行既有 file_size（更新路径不覆盖）
                    create_row=False,
                )

            # 7) publish completed + 失效广播
            await self._publish_event(
                make_completed(
                    session_id, file_id, filename=filename,
                    chunk_count=chunk_count,
                )
            )
            await self._publish_invalidation()

            logger.info(
                "会话文件 %s 建索引完成 (chunk_count=%d, session=%s)",
                file_id, chunk_count, session_id,
            )
        except Exception as e:
            # 失败：置 failed + error_message + publish failed，随后重新抛出交 worker 决策。
            error_text = str(e)[:_ERROR_MESSAGE_MAX_LEN]
            logger.error(
                "会话文件 %s 建索引失败: %s: %s",
                file_id, type(e).__name__, error_text,
            )
            await self._safe_mark_failed(file_id, error_text)
            await self._publish_event(
                make_failed(
                    session_id, file_id, error_text, filename=filename,
                    message="建索引失败",
                )
            )
            # 重新抛出：worker（任务 6）据此判定可重试 / DLQ；幂等更新保证重试成功时
            # 行会被再次更新为 completed，failed 不会成为永久终态。
            raise

    async def remove_file(
        self, *, session_id: str, file_id: str
    ) -> None:
        """移除单个会话文件（Req 1.8 / 6.7）。

        - 按 ``doc_id == file_id`` 删 Milvus 向量（仅清此文件，其余文件 / 会话不受影响）。
        - 删 ``session_chunks``（``file_id`` FK CASCADE 兜底，但显式删一次确保
          SQLite 等不一定开 FK 的环境也生效）+ 删 ``session_files`` 行（释放配额）。
        - publish ``InvalidationBus`` 失效 ``kb_session_files`` 的 ``_loaded_at``，避免
          其他 API 进程在 ``load_cache_ttl`` 内仍命中已删向量的旧加载快照（design C7.1）。

        Args:
            session_id: 校验文件归属的会话（防止跨会话误删）。
            file_id: ``SessionFile.id`` == Milvus ``doc_id``。

        Raises:
            ValueError: 文件不存在或不属于该会话。
        """
        async with self._db_session_factory() as session:
            row = await session.get(SessionFile, file_id)
            if row is None or row.session_id != session_id:
                raise ValueError("文件不存在或不属于该会话")

            # 在删行前捕获文件名/扩展名：commit + session 关闭后 ORM 行属性会失效，
            # 后续 publish removed 事件 / 删 MinIO 原件均依赖这两个值。
            filename = row.filename
            ext = (row.file_type or "").lower()

            # 先删向量：DB 行尚未删除时即使删向量失败，session_id expr 过滤仍能让用户
            # 看不到这些向量（隔离前提下安全），失败仅记 WARNING 不阻塞 DB 主流程。
            try:
                await self.milvus.delete_by_doc_id(SESSION_FILES_KB_ID, file_id)
            except Exception as e:
                logger.warning(
                    "会话文件 %s 向量删除失败（继续删 DB 行）: %s", file_id, e
                )

            # 显式删 session_chunks（FK CASCADE 兜底）+ 删 session_files
            await session.execute(
                sa_delete(SessionChunk).where(SessionChunk.file_id == file_id)
            )
            await session.delete(row)
            await session.commit()

        try:
            from app.summary.service import delete_document_summary

            await delete_document_summary("session-file", file_id)
        except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
            logger.warning("会话文件 %s 摘要清理失败（非致命）: %s", file_id, exc)

        # 删 MinIO 原件（按 session_id/file_id/ext 拼 key）。失败仅记 WARNING。
        await self._safe_remove_object(
            session_file_object_key(session_id, file_id, ext)
        )

        # 失效广播（与上传路径同款 kb_data 信号，main.py 的 _handle_kb_data 处理）
        await self._publish_invalidation()

        # publish removed 事件：通知订阅该会话的 WS 客户端文件已移除（REQ-6，best-effort：
        # 失败仅 WARNING，不阻塞移除主流程）。
        await self._publish_event(
            make_removed(session_id, file_id, filename=filename)
        )

    async def cleanup_session_files(self, session_id: str) -> None:
        """删会话级联清理（Req 1.6）。

        - ``delete_session(sid)`` 删共享库中该会话全部向量（按 ``session_id`` 标量 expr）。
        - 显式删 ``session_chunks``（``session_files`` 由 ChatSession FK CASCADE 删，
          ``session_chunks`` 由 ``session_files`` FK CASCADE 兜底，但显式删一次确保
          SQLite 等环境也生效）。
        - publish 失效广播。

        失败仅记 WARNING、不阻塞会话删除主流程（Req 1.6）：会话删除是更上层的语义，
        清理失败可由后台对账兜底（无对应 ChatSession 的孤儿向量）。
        """
        try:
            await self.milvus.delete_session(session_id)
        except Exception as e:
            logger.warning("会话 %s 向量清理失败（非致命）: %s", session_id, e)

        # 删该会话在 MinIO 的全部原件（按前缀）。失败仅记 WARNING。
        try:
            store = get_object_store()
            if store is not None:
                await store.remove_prefix(session_prefix(session_id))
        except Exception as e:
            logger.warning("会话 %s MinIO 原件清理失败（非致命）: %s", session_id, e)

        try:
            from app.summary.service import delete_session_summaries

            await delete_session_summaries(session_id)
        except Exception as e:
            logger.warning("会话 %s 摘要清理失败（非致命）: %s", session_id, e)

        try:
            async with self._db_session_factory() as session:
                await session.execute(
                    sa_delete(SessionChunk).where(SessionChunk.session_id == session_id)
                )
                await session.commit()
        except Exception as e:
            logger.warning(
                "会话 %s session_chunks 清理失败（非致命）: %s", session_id, e
            )

        try:
            await self._publish_invalidation()
        except Exception as e:
            logger.warning("会话 %s 失效广播失败（非致命）: %s", session_id, e)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _safe_cleanup_vectors(self, file_id: str) -> None:
        """部分写入失败时清理 Milvus 向量（幂等，失败仅记 WARNING）。"""
        try:
            await self.milvus.delete_by_doc_id(SESSION_FILES_KB_ID, file_id)
        except Exception as e:
            logger.warning(
                "会话文件 %s 失败清理向量异常（可能本无残留）: %s", file_id, e
            )

    @staticmethod
    async def _safe_remove_object(object_key: str) -> None:
        """删除 MinIO 对象（幂等，失败仅记 WARNING）。"""
        try:
            store = get_object_store()
            if store is not None:
                await store.remove(object_key)
        except Exception as e:
            logger.warning("会话附件对象 %s 删除异常（可能本无残留）: %s", object_key, e)

    async def _safe_delete_file_row(self, file_id: str) -> None:
        """删除 ``SessionFile`` 行（入队失败回滚用，幂等，失败仅记 WARNING）。

        用于 ``enqueue_upload`` 入队失败后清理已建的 queued 行，保证「队列不可用 →
        不留 DB 行」的不留孤儿约束。
        """
        try:
            async with self._db_session_factory() as session:
                row = await session.get(SessionFile, file_id)
                if row is not None:
                    await session.delete(row)
                    await session.commit()
        except Exception as e:
            logger.warning(
                "会话文件 %s DB 行清理异常（可能本无残留）: %s", file_id, e
            )

    async def _update_task_status(
        self,
        file_id: str,
        *,
        status: str,
        progress: int | None = None,
        progress_message: str | None = None,
    ) -> None:
        """更新 ``SessionFile`` 的建索引状态 / 进度（worker 路径用）。

        幂等且安全：``completed`` 终态不被非 completed 更新覆盖（防重复投递把已完成
        行改回 processing）。状态更新失败仅记 WARNING，不打断建索引主链路（REQ-9）。
        """
        try:
            async with self._db_session_factory() as session:
                row = await session.get(SessionFile, file_id)
                if row is None:
                    return
                # 不把已完成终态改回中间态（幂等保护）。
                if row.status == "completed" and status != "completed":
                    return
                row.status = status
                if progress is not None:
                    row.progress = progress
                if progress_message is not None:
                    row.progress_message = progress_message
                await session.commit()
        except Exception as e:
            logger.warning(
                "会话文件 %s 状态更新失败（非致命）: %s", file_id, e
            )

    async def _safe_mark_failed(self, file_id: str, error_message: str) -> None:
        """将 ``SessionFile`` 置 failed + 写 error_message（幂等，失败仅记 WARNING）。

        已 completed 的行不被覆盖（正确终态优先）。
        """
        try:
            async with self._db_session_factory() as session:
                row = await session.get(SessionFile, file_id)
                if row is None or row.status == "completed":
                    return
                row.status = "failed"
                row.error_message = error_message
                await session.commit()
        except Exception as e:
            logger.warning(
                "会话文件 %s 置 failed 失败（非致命）: %s", file_id, e
            )

    async def _publish_event(self, event) -> None:
        """best-effort 发布上传状态事件：失败仅记 WARNING，不打断主链路（REQ-9）。"""
        bus = self.event_bus
        if bus is None:
            return
        try:
            await bus.publish(event)
        except Exception as e:
            logger.warning(
                "会话上传事件发布失败（非致命, type=%s）: %s",
                event.get("type") if isinstance(event, dict) else "?", e,
            )

    async def _publish_invalidation(self) -> None:
        """publish ``kb_session_files`` 的 ``_loaded_at`` 失效信号。

        复用 bugfix ``retrieval-pipeline-hardening`` 的 ``InvalidationBus``，让其他
        API 进程在 ``load_cache_ttl`` 内不再读到旧加载快照（main.py 已绑定 ``kb_data``
        handler 调用 ``milvus._loaded_at.pop`` + ``cache.invalidate_kb``）。Redis 不可用时
        ``publish`` no-op 降级（不影响主流程）。
        """
        bus = get_invalidation_bus()
        if bus is not None:
            await bus.publish("kb_data", SESSION_FILES_KB_ID)

    async def _fetch_one(self, file_id: str) -> SessionFileVO:
        """取最新持久化的 ``SessionFile``（含 ``server_default`` 时间字段）。"""
        async with self._db_session_factory() as session:
            row = await session.get(SessionFile, file_id)
            if row is None:
                # 不应发生（刚 commit 完）；防御性兜底。
                raise RuntimeError(f"持久化失败：未找到 SessionFile {file_id}")
            return _row_to_vo(row)

    @staticmethod
    def _cleanup_temp_file(path: Path) -> None:
        """删除临时文件，失败仅记 WARNING（不影响主流程）。"""
        try:
            if path.exists():
                os.remove(path)
        except OSError as e:
            logger.warning("会话上传临时文件清理失败 %s: %s", path, e)


# ============================================================
# 进程内单例
# ============================================================

_service: SessionUploadService | None = None


def get_session_upload_service() -> SessionUploadService:
    """进程内 ``SessionUploadService`` 单例（无状态，单例仅为依赖注入便利）。

    与 ``get_upload_limit_resolver`` / ``get_milvus_client`` / ``get_retrieval_config_store``
    风格一致。
    """
    global _service
    if _service is None:
        _service = SessionUploadService()
    return _service
