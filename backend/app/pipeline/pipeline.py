"""完整文档处理管道编排

流程：load → OCR(嵌入图片，并发+按页插入) → 合并文本 → chunk → enrich → embed → index

集成：
- ProgressTracker：各阶段加权进度更新
- PipelineLogger：结构化 JSON 日志 + 慢阶段检测
- trace_id：链路追踪贯穿全流程
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dataclasses import asdict, dataclass

from app.config import get_settings
from app.models.manager import ModelManager
from app.pipeline.chunker_router import ChunkerFactory, ChunkerRouter
import app.pipeline.chunkers  # noqa: F401 — 确保所有 Chunker 注册到 Factory
from app.pipeline.embedder import PipelineEmbedder
from app.pipeline.enricher import Enricher
from app.pipeline.cleaner import TextCleaner, strip_data_uris
from app.pipeline.loader import EmbeddedImage, LoadResult, get_loader
from app.pipeline.logging import PipelineLogger
from app.pipeline.context_embedder import ContextualEmbedder
from app.pipeline.metadata import ChunkMetadata, MetadataExtractor
from app.pipeline.progress import PipelineStage, ProgressTracker
from app.schema.db import Chunk, Document, KnowledgeBase
from app.storage.milvus import MilvusClient

if TYPE_CHECKING:
    from app.pipeline.chunker import ChunkResult
    from app.pipeline.embedder import EmbedResult
    from app.pipeline.ocr.manager import OCRManager
    from app.pipeline.ocr.provider import OCRResult
    from app.pipeline.asr.manager import ASRManager
    from app.pipeline.queue import TaskQueue
    from app.session_upload.limits import UploadLimits

logger = logging.getLogger(__name__)


class CancelledError(Exception):
    """文档处理被取消（文档已删除或用户主动取消）"""
    pass


class UploadCapExceeded(Exception):
    """容量闸门拒绝：Chunk 阶段后、Embed 前判定本次入库将超过生效 chunk 上限（design C5）。

    在昂贵的 embedding（占用全局并发 slot、用户在线等待）发生前精确拦截并拒绝，零浪费
    （Req 4.2 / 4.3 / 6.6 / 9.4）。属业务异常（对齐团队规范 ``BusinessException`` 思路），
    由上层转为明确的用户提示：
    - KB_Upload（异步）：置文档 ``failed`` 并写 ``error_message``。
    - Session_Upload（同步）：直接返回拒绝（"内容过多"，不截断、不转异步）。

    Attributes:
        scope: 闸门口径，``"kb"``（单库 chunk 上限）或 ``"session"``（会话累计 chunk 上限）。
        cap: 生效上限（Effective_Limit）。
        used: 该库 / 该会话当前已用 child chunk 数。
        incoming: 本次精确 child chunk 数（``len(chunk_result.child_chunks)``）。
    """

    def __init__(self, scope: str, cap: int, used: int, incoming: int):
        self.scope = scope
        self.cap = cap
        self.used = used
        self.incoming = incoming
        super().__init__(
            f"容量超限（{scope}）：已用 {used} + 本次 {incoming} = {used + incoming} "
            f"超过上限 {cap}"
        )


@dataclass
class ProcessedDocument:
    """不依赖 Document 表的同步处理单元产物（design C5）。

    ``DocumentPipeline.process_to_vectors`` 完成 Load→OCR→Clean→Chunk→（Pre_Embed_Gate）→Embed
    后返回本结构，供会话同步路径（``SessionUploadService``）与正式异步路径（``process``）共用。
    正式 ``process`` 在该单元外围保留 Document 状态更新 / 取消检查 / Index 写库。

    Attributes:
        chunk_result: 切分结果（含父块 / 子块 / 父子映射 / 面包屑标题）。
        enriched_children: 经 Enrich（当前 pass-through）后的子块文本，与 embedding 一一对应。
        metadata_list: 与 ``enriched_children`` 一一对应的 chunk 元数据。
        embed_result: 子块的稠密 / 稀疏向量。
        doc_metadata: 文档级元数据（含 ``ocr_provider`` 等）。
        child_to_parent: 子→父反向索引（O(1) 查找，Index 阶段构造 parent_id 用）。
    """

    chunk_result: "ChunkResult"
    enriched_children: list[str]
    metadata_list: list["ChunkMetadata"]
    embed_result: "EmbedResult"
    doc_metadata: dict
    child_to_parent: dict[int, int]


class DocumentPipeline:
    """文档处理管道，编排 load → chunk → enrich → embed → index 全流程"""

    def __init__(
        self,
        model_manager: ModelManager,
        milvus_client: MilvusClient,
        db_session_factory: async_sessionmaker[AsyncSession],
        ocr_manager: OCRManager | None = None,
        asr_manager: "ASRManager | None" = None,
        graph_queue: "TaskQueue | None" = None,
    ):
        self.milvus = milvus_client
        self.db_session_factory = db_session_factory
        self.ocr_manager = ocr_manager
        self.asr_manager = asr_manager
        # 知识图谱抽取慢道队列（pipeline:graph）。None 表示未启用/不可用 → 文档完成后
        # 不触发图谱抽取（优雅降级，主链路零影响）。
        self.graph_queue = graph_queue
        # 初始化管道各节点
        self.enricher = Enricher(llm=None, enabled=False)
        settings = get_settings()
        self.embedder = PipelineEmbedder(
            model_manager=model_manager,
            batch_size=settings.pipeline_embed_batch_size,
            concurrency=settings.pipeline_embed_concurrency,
            per_doc_concurrency=settings.pipeline_embed_per_doc_concurrency,
        )

    # ------------------------------------------------------------------
    # 不依赖 Document 表的同步处理单元（design C5 / Task 6）
    # ------------------------------------------------------------------

    # 切分阶段警告阈值：子块过多时打 warning（仅信息性，硬闸门由 Pre_Embed_Gate 精确判定）。
    _CHUNK_COUNT_WARN_THRESHOLD = 50000

    async def process_to_vectors(
        self,
        file_path: str,
        *,
        source_kind: str,
        source_id: str,
        tenant_id: str | None,
        limits: "UploadLimits",
        incoming_doc_id: str | None = None,
        progress_tracker: "ProgressTracker | None" = None,
    ) -> ProcessedDocument:
        """执行 Load→OCR→Clean→Chunk→Pre_Embed_Gate→Embed 的同步处理单元（design C5）。

        本方法**不依赖 Document 表**，不调 ``_check_cancelled`` / ``ProgressTracker`` /
        ``_update_status``，供两条路径共用：

        - 会话同步路径（``SessionUploadService.upload``）：``source_kind="session"``，
          ``source_id=session_id``。Pre_Embed_Gate 按该会话已用 chunk 累计判定。
        - 知识库异步路径（``DocumentPipeline.process``）：``source_kind="kb"``，
          ``source_id=kb_id``。Pre_Embed_Gate 按该知识库已用 chunk 累计判定，
          ``incoming_doc_id`` 用于排除当前文档自身（重处理场景下避免双计）。

        闸门判定使用精确 ``len(chunk_result.child_chunks)``（Req 4.3 / 6.6 / 9.4），
        发生在 Embed 之前；超限抛 :class:`UploadCapExceeded`，由上层转为明确提示。

        Args:
            file_path: 文件路径。
            source_kind: 来源标识，``"kb"`` 或 ``"session"``。
            source_id: KB 上传时为 ``kb_id``，会话上传时为 ``session_id``。
            tenant_id: 租户 ID（None 表示无租户上下文，分块参数走全默认）。
            limits: 单次上传校验取一次的生效限制快照（同一次校验全程复用，Req 9.3）。
            incoming_doc_id: KB 上传时本文档 ID，用于聚合时排除自身（避免重处理双计）；
                会话上传可不传。
            progress_tracker: 可选的进度追踪器。传入时（KB 异步路径）embedding 阶段
                会按批次实时写库，让前端进度条在大文件长时间 embedding 中持续推进；
                为 None 时（会话同步路径）不更新进度，行为与之前一致。

        Returns:
            :class:`ProcessedDocument` 产物（含 chunk_result / enriched_children / metadata_list /
            embed_result / doc_metadata / child_to_parent），由调用方据此写入索引。

        Raises:
            UploadCapExceeded: 该 scope 已用 chunk 与本次精确 child chunk 数之和超过生效上限。
            ValueError: 文档提取文本为空且未配置 OCR 服务。
        """
        if source_kind not in ("kb", "session"):
            raise ValueError(f"非法的 source_kind: {source_kind!r}（仅支持 'kb' / 'session'）")

        ext = Path(file_path).suffix.lstrip(".")
        loader = get_loader(ext)

        # ─── 1. Load ───
        # loader.load 是同步 CPU 密集调用（PDF/docx 解析等），丢线程池避免独占事件循环。
        load_result = await asyncio.to_thread(loader.load, file_path)
        images_to_cleanup = load_result.images
        try:
            # ─── 2. 音频 ASR 转写（音频文件走语音识别链路） ───
            is_audio = bool(load_result.metadata.get("is_audio"))
            if is_audio:
                if not self.asr_manager:
                    # 音频文件但未配置 ASR 服务：业务输入问题。
                    from app.api.errors import EmptyDocumentContentError

                    raise EmptyDocumentContentError("音频文件需要语音识别，但未配置 ASR 服务")

                asr_result = await self._transcribe_with_limit(file_path)
                if not asr_result.full_text.strip():
                    from app.api.errors import EmptyDocumentContentError

                    raise EmptyDocumentContentError("音频转写结果为空")

                load_result = LoadResult(
                    content=asr_result.full_text,
                    metadata={
                        **load_result.metadata,
                        "asr_provider": asr_result.provider_name,
                    },
                    images=[],
                )

            # ─── 2. OCR（与 process() 等价，但不更新 ProgressTracker / 不查 Document） ───
            stripped_content = load_result.content.strip()
            needs_ocr = (
                not is_audio
                and (not stripped_content or len(stripped_content) < 10)
                and self.ocr_manager
            )
            if needs_ocr:
                # recognize_document 按 Provider 能力准备输入：接受 PDF 的服务整文件直送，
                # 只接受图片的服务（如 PaddleOCR）由其内部按页渲染整页图片后逐页识别。
                # pipeline 不感知能力细节，也不再靠"返回空再回落"在运行时试探。
                ocr_result = await self.ocr_manager.recognize_document(file_path)
                load_result = LoadResult(
                    content=ocr_result.full_text,
                    metadata={
                        **load_result.metadata,
                        "ocr_provider": ocr_result.provider_name,
                    },
                    images=[],
                )
            elif not is_audio and (not stripped_content or len(stripped_content) < 10):
                if not self.ocr_manager:
                    # 无可提取文本且未配置 OCR：业务输入问题而非服务端故障。
                    # 会话同步路径透出后由全局 handler 映射 422；KB 异步路径被
                    # except Exception 捕获并置文档 failed（两路径行为均符合预期）。
                    from app.api.errors import EmptyDocumentContentError

                    raise EmptyDocumentContentError("文档提取文本为空，且未配置 OCR 服务")

            final_content = load_result.content
            if load_result.images and self.ocr_manager:
                # 嵌入图片 OCR：与 process() 路径一致（按页插入），
                # 但 _process_embedded_images 仅把 doc_id 用作日志，无 Document 表副作用。
                final_content = await self._process_embedded_images(
                    load_result, doc_id=incoming_doc_id or source_id
                )
            elif load_result.images and not self.ocr_manager:
                logger.warning(
                    "[%s=%s] 文档包含 %d 张嵌入图片，但未配置 OCR 服务，图片内容将被忽略",
                    source_kind, source_id, len(load_result.images),
                )

            # ─── 2.5 TextCleaner 去噪 ───
            # KB 路径按 KnowledgeBase.config.enable_cleaner 决定（默认 True，与既有 process() 一致，
            # 保留旧行为，避免对历史 KB 的去噪行为产生回归）；会话路径无 KB 配置，统一启用 cleaner
            # （cleaner 仅去噪、不丢内容，对会话级临时上传安全且与 KB 默认一致）。
            enable_cleaner = True
            if source_kind == "kb":
                async with self.db_session_factory() as kb_session:
                    kb_row = await kb_session.execute(
                        select(KnowledgeBase).where(KnowledgeBase.id == source_id)
                    )
                    kb_obj = kb_row.scalar_one_or_none()
                    if kb_obj and kb_obj.config and isinstance(kb_obj.config, dict):
                        enable_cleaner = kb_obj.config.get("enable_cleaner", True)

            if enable_cleaner:
                cleaner = TextCleaner()
                use_page_blocks = load_result.page_blocks if load_result.page_blocks else None
                if final_content != load_result.content:
                    # 嵌入图片 OCR 已合并，page_blocks 不再准确（不含图片 OCR 文本）。
                    use_page_blocks = None
                final_content = await asyncio.to_thread(
                    cleaner.clean,
                    content=final_content,
                    page_texts=load_result.page_texts if load_result.page_texts else None,
                    page_blocks=use_page_blocks,
                )

            # 清洗后兜底 OCR：与 process() 一致（音频转写结果不触发 OCR 兜底）
            cleaned_stripped = final_content.strip()
            if (
                (not cleaned_stripped or len(cleaned_stripped) < 10)
                and self.ocr_manager
                and not needs_ocr
                and not is_audio
            ):
                ocr_result = await self.ocr_manager.recognize_document(file_path)
                final_content = ocr_result.full_text
                load_result = LoadResult(
                    content=final_content,
                    metadata={
                        **load_result.metadata,
                        "ocr_provider": ocr_result.provider_name,
                    },
                    images=[],
                )

            # ─── 2.6 剥离内嵌 base64 图片 ───
            # 放在所有 OCR 分支（主路径 / 嵌入图片 / 兜底）收敛之后、空内容校验之前，
            # 一处覆盖全部来源。与 cleaner 不同，此步**无条件执行**：base64 是数据卫生
            # 问题而非去噪偏好，enable_cleaner=False 的知识库同样不该把它带进向量库。
            if final_content:
                _before_len = len(final_content)
                final_content = strip_data_uris(final_content)
                _stripped = _before_len - len(final_content)
                if _stripped > 0:
                    logger.info(
                        "[%s=%s] 剥离内嵌 base64 图片数据 %d 字符（原文 %d 字符）",
                        source_kind, source_id, _stripped, _before_len,
                    )

            # OCR / 转写 / 清洗全部走完仍无内容 → 明确失败，而不是入库一个 0 chunk 的空文档。
            # 常见成因：OCR 服务不支持该文件类型（如只收图片的服务收到 PDF），
            # 或扫描件质量过差。此处给出可操作的提示，避免"处理成功但检索不到"。
            if not final_content.strip() and not load_result.pre_chunked:
                from app.api.errors import EmptyDocumentContentError

                raise EmptyDocumentContentError(
                    "文档未提取到任何文本："
                    "若为扫描件请确认 OCR 服务可用且支持该文件类型"
                )

            # ─── 3. Chunk ───
            if load_result.pre_chunked:
                from app.pipeline.chunker import ChunkResult as _ChunkResult

                pre_chunks = load_result.pre_chunked
                chunk_result = _ChunkResult(
                    parent_chunks=pre_chunks,
                    child_chunks=pre_chunks,
                    parent_child_map={i: [i] for i in range(len(pre_chunks))},
                )
            else:
                chunker = await self._select_chunker_for_source(
                    source_kind=source_kind,
                    source_id=source_id,
                    tenant_id=tenant_id,
                    file_type=ext,
                    content=final_content,
                )
                chunk_result = await asyncio.to_thread(
                    chunker.chunk, final_content, load_result.metadata
                )

            # ─── 3.4 大小护栏（对齐 WeKnora absoluteMaxSize） ───
            # 无论上游用哪种 chunker（含手动指定的 laws/paper/qa 体裁切分器，
            # 它们本身无 size 控制），统一对最终父/子块施加绝对大小上限，杜绝
            # 超大块导致检索/问答时撑爆模型上下文。按租户分块参数取上限。
            from app.pipeline.chunker import enforce_size_limits
            from app.retrieval.config import get_retrieval_config_store

            guard_cfg = await get_retrieval_config_store().get_effective(tenant_id)
            chunk_result = enforce_size_limits(
                chunk_result,
                parent_size=guard_cfg.parent_chunk_size,
                child_size=guard_cfg.child_chunk_size,
                overlap=guard_cfg.chunk_overlap,
            )

            child_count = len(chunk_result.child_chunks)
            if child_count > self._CHUNK_COUNT_WARN_THRESHOLD:
                logger.warning(
                    "[%s=%s] 子块数量 %d 较多，处理时间可能较长",
                    source_kind, source_id, child_count,
                )

            # ─── 3.5 Pre_Embed_Gate 容量闸门（design C5 / Req 4.2 / 4.3 / 6.6 / 9.4） ───
            # 用精确 child_count（而非估算）在 Embed 之前判定，超限抛 UploadCapExceeded；
            # KB 与 Session 统一用 kb_chunk_cap（临时文件 = 会话级 KB，共用同一硬上限）。
            if source_kind == "kb":
                cap = limits.kb_chunk_cap
                used = await self._kb_used_chunks(source_id, exclude_doc_id=incoming_doc_id)
            else:  # source_kind == "session"
                cap = limits.kb_chunk_cap
                used = await self._session_used_chunks(source_id)

            if used + child_count > cap:
                raise UploadCapExceeded(
                    scope=source_kind, cap=cap, used=used, incoming=child_count
                )

            # Enrich（当前 pass-through）
            enriched_children = await self.enricher.enrich(chunk_result.child_chunks)

            # ─── 4. Embed ───
            extractor = MetadataExtractor()
            metadata_list = extractor.extract(
                child_chunks=enriched_children,
                parent_chunks=chunk_result.parent_chunks,
                parent_child_map=chunk_result.parent_child_map,
                doc_metadata=load_result.metadata,
                page_texts=load_result.page_texts if load_result.page_texts else None,
            )

            child_to_parent = self._build_child_to_parent_map(chunk_result.parent_child_map)
            ctx_embedder = ContextualEmbedder()
            context_headers = chunk_result.context_headers or []
            embed_texts: list[str] = []
            for child_idx, (child_text, meta) in enumerate(zip(enriched_children, metadata_list)):
                parent_idx = child_to_parent.get(child_idx)
                parent_text = (
                    chunk_result.parent_chunks[parent_idx] if parent_idx is not None else None
                )
                header = context_headers[child_idx] if child_idx < len(context_headers) else None
                embed_texts.append(
                    ctx_embedder.build_embed_text(
                        child_text, meta, parent_text, context_header=header
                    )
                )

            # 不传 ProgressTracker / doc_id（会话同步路径无 Document 行）；
            # KB 异步路径传入 progress_tracker，使 embedding 按批次实时写库，
            # 避免大文件 embedding 期间前端进度条长时间不动（progress_tracker 为 None
            # 时不更新进度，与会话路径行为一致）。
            if progress_tracker is not None:
                # 进度推进到 EMBED 区间起点（50%），让前端从此处开始随批次增长。
                await progress_tracker.start_stage(
                    PipelineStage.EMBED, "正在生成向量"
                )
            embed_result = await self._embed_with_progress(
                embed_texts, tracker=progress_tracker, doc_id=incoming_doc_id or ""
            )

            return ProcessedDocument(
                chunk_result=chunk_result,
                enriched_children=enriched_children,
                metadata_list=metadata_list,
                embed_result=embed_result,
                doc_metadata=load_result.metadata,
                child_to_parent=child_to_parent,
            )
        finally:
            # 清理图片临时目录：本方法在两条路径下都需兜底清理图片临时目录。
            # 外围 process() 已不再持有 images 引用（已委托给本方法），故此处必须执行
            # 清理；_cleanup_image_temp_dirs 对空列表与已删路径均幂等（os.path.exists 守卫）。
            self._cleanup_image_temp_dirs(images_to_cleanup)

    async def _kb_used_chunks(
        self, kb_id: str, exclude_doc_id: str | None = None
    ) -> int:
        """聚合该知识库当前已用 child chunk 数（``Document.chunk_count`` 之和）。

        用于 KB_Upload 的 Pre_Embed_Gate 判定（design C5）。``exclude_doc_id`` 排除当前
        正在处理的文档自身——重处理场景下，同一 doc_id 的旧 chunk 会被覆盖（Index 阶段
        先按 doc_id 删旧向量再写入），不应在闸门里被双计。
        """
        async with self.db_session_factory() as session:
            stmt = select(func.coalesce(func.sum(Document.chunk_count), 0)).where(
                Document.kb_id == kb_id
            )
            if exclude_doc_id is not None:
                stmt = stmt.where(Document.id != exclude_doc_id)
            result = await session.execute(stmt)
            return int(result.scalar_one() or 0)

    async def _session_used_chunks(self, session_id: str) -> int:
        """聚合该会话当前已用 child chunk 数（``SessionFile.chunk_count`` 之和）。

        用于 Session_Upload 的 Pre_Embed_Gate 判定（design C5 / Req 6.4）。移除文件后
        ``session_files`` 行被删除，配额自动释放（Req 6.7）。本地导入避免 ``app.pipeline``
        与 ``app.schema.db`` 形成不必要的顶层耦合（``SessionFile`` 仅在会话路径需要）。
        """
        from app.schema.db import SessionFile

        async with self.db_session_factory() as session:
            result = await session.execute(
                select(func.coalesce(func.sum(SessionFile.chunk_count), 0)).where(
                    SessionFile.session_id == session_id
                )
            )
            return int(result.scalar_one() or 0)

    async def _select_chunker_for_source(
        self,
        *,
        source_kind: str,
        source_id: str,
        tenant_id: str | None,
        file_type: str,
        content: str,
    ):
        """为 ``process_to_vectors`` 选择 Chunker，按来源分流：

        - KB 路径：复用既有 ``_select_chunker``（读 KB.config.chunker_type + 租户分块参数）。
        - 会话路径：无 KB 配置，按 ``ChunkerRouter.select`` 自动路由 + 租户分块参数。

        分块参数（parent/child/overlap）按租户从 ``RetrievalConfig`` 读取（与 KB 路径一致），
        ``tenant_id`` 为 None → 全默认。
        """
        if source_kind == "kb":
            async with self.db_session_factory() as session:
                return await self._select_chunker(session, source_id, file_type, content)

        # 会话路径：无 KB 配置，直接路由 + 按租户读分块参数
        from app.retrieval.config import get_retrieval_config_store

        cfg = await get_retrieval_config_store().get_effective(tenant_id)
        chunk_kwargs = {
            "parent_size": cfg.parent_chunk_size,
            "child_size": cfg.child_chunk_size,
            "overlap": cfg.chunk_overlap,
        }
        selected_type = ChunkerRouter.select(file_type, content)
        logger.info(
            "[session=%s] 自动路由 chunker_type=%s (file_type=%s)",
            source_id, selected_type, file_type,
        )
        # 仅 naive 类型接受 chunk 参数
        kwargs = chunk_kwargs if selected_type == "naive" else {}
        return ChunkerFactory.create(selected_type, **kwargs)

    async def process(
        self, file_path: str, doc_id: str, kb_id: str, trace_id: str | None = None
    ) -> None:
        """完整文档处理流程

        Load→OCR→Clean→Chunk→Pre_Embed_Gate→Embed 委托给 :meth:`process_to_vectors`
        （不依赖 Document 表的同步处理单元，design C5 / Task 6）；本方法在其外围保留
        Document 表状态更新 / 取消检查 / Index 写库（Chunk 写 SQLite + 向量写 Milvus）。

        Args:
            file_path: 文件路径
            doc_id: 文档 ID
            kb_id: 知识库 ID
            trace_id: 链路追踪 ID，不传则自动生成 UUID4
        """
        # 生成或使用传入的 trace_id
        if not trace_id:
            trace_id = str(uuid.uuid4())

        # 初始化进度追踪器和结构化日志器
        settings = get_settings()
        tracker = ProgressTracker(doc_id, self.db_session_factory)
        pl = PipelineLogger(
            trace_id=trace_id,
            doc_id=doc_id,
            slow_threshold_ms=settings.pipeline_slow_threshold_ms,
        )

        pipeline_start = time.monotonic()
        current_stage = PipelineStage.LOAD
        processed: ProcessedDocument | None = None

        async with self.db_session_factory() as session:
            try:
                # 更新状态为 processing（立即提交，让前端轮询能看到）
                await self._update_status(session, doc_id, "processing")
                await session.commit()

                # 取一次租户级生效限制快照（KB 路径仅消费 kb_chunk_cap，由 process_to_vectors
                # 内部用于 Pre_Embed_Gate 判定；Req 9.3 单次校验全程复用）。
                # tenant_id 以 KnowledgeBase.tenant_id 为权威来源（Worker 无请求上下文）。
                from app.session_upload.limits import get_upload_limit_resolver

                kb_tenant_result = await session.execute(
                    select(KnowledgeBase.tenant_id).where(KnowledgeBase.id == kb_id)
                )
                kb_tenant_id = kb_tenant_result.scalar_one_or_none()
                limits = await get_upload_limit_resolver().resolve(kb_tenant_id)

                # ─── 1–4. Load → OCR → Clean → Chunk → Pre_Embed_Gate → Embed ───
                # 委托给不依赖 Document 表的同步处理单元（design C5）。
                # Pre_Embed_Gate 在 Embed 之前用精确 child chunk 数判定单库容量
                # （Req 4.2 / 4.3 / 9.4）；超限抛 UploadCapExceeded，由下方 except 分支
                # 置文档 failed 并写明确 error_message。
                current_stage = PipelineStage.LOAD
                await tracker.start_stage(PipelineStage.LOAD, "正在加载文档")
                stage_start = time.monotonic()

                # 取消检查：Load 前先看一次（process_to_vectors 内部不查 Document 表）。
                await self._check_cancelled(doc_id)

                processed = await self.process_to_vectors(
                    file_path,
                    source_kind="kb",
                    source_id=kb_id,
                    tenant_id=kb_tenant_id,
                    limits=limits,
                    incoming_doc_id=doc_id,
                    progress_tracker=tracker,
                )

                # process_to_vectors 一次性走完 Load→Embed；其内部已在 embedding 阶段
                # 按批次实时推进进度（EMBED 区间 50%–90%）。此处把 Load/OCR/Chunk 标完成、
                # 并将 EMBED 收尾到区间终点（complete_stage→90%），保证后续 INDEX 衔接。
                load_to_embed_duration_ms = int((time.monotonic() - stage_start) * 1000)
                await tracker.complete_stage(PipelineStage.EMBED)

                child_count = len(processed.enriched_children)
                pl.stage_complete(
                    stage="load_to_embed",
                    duration_ms=load_to_embed_duration_ms,
                    input_size=1,
                    output_size=child_count,
                )

                # ─── 5. Index 阶段 ───
                await self._check_cancelled(doc_id)
                current_stage = PipelineStage.INDEX
                await tracker.start_stage(PipelineStage.INDEX, "正在写入索引")
                stage_start = time.monotonic()

                chunk_result = processed.chunk_result
                enriched_children = processed.enriched_children
                metadata_list = processed.metadata_list
                embed_result = processed.embed_result
                child_to_parent = processed.child_to_parent

                print(
                    f"[Pipeline] 文档 {doc_id} 开始写入索引，"
                    f"父块: {len(chunk_result.parent_chunks)}，子块: {len(enriched_children)}"
                )

                # 获取文档文件名，用于 BM25 content 前缀增强
                doc_result = await session.execute(
                    select(Document.filename).where(Document.id == doc_id)
                )
                doc_filename = doc_result.scalar_one_or_none() or ""
                # 去掉文件扩展名，只保留文档名称
                doc_title = Path(doc_filename).stem if doc_filename else ""

                parent_ids: list[str] = [
                    str(uuid.uuid4()) for _ in range(len(chunk_result.parent_chunks))
                ]

                # 写入父 chunk 到 SQLite
                for idx, (pid, content) in enumerate(
                    zip(parent_ids, chunk_result.parent_chunks)
                ):
                    parent_chunk = Chunk(
                        id=pid,
                        doc_id=doc_id,
                        kb_id=kb_id,
                        parent_id=None,
                        content=content,
                        chunk_index=idx,
                        tenant_id=kb_tenant_id,
                    )
                    session.add(parent_chunk)

                # 写入子 chunk 到 SQLite + Milvus
                milvus_data: list[dict] = []
                for child_idx, child_text in enumerate(enriched_children):
                    child_id = str(uuid.uuid4())

                    parent_idx = child_to_parent.get(child_idx)
                    parent_id = parent_ids[parent_idx] if parent_idx is not None else None

                    # 构造 chunk_metadata JSON
                    meta = metadata_list[child_idx]
                    chunk_metadata_dict = asdict(meta)

                    child_chunk = Chunk(
                        id=child_id,
                        doc_id=doc_id,
                        kb_id=kb_id,
                        parent_id=parent_id,
                        content=child_text,
                        chunk_index=child_idx,
                        chunk_metadata=chunk_metadata_dict,
                        tenant_id=kb_tenant_id,
                    )
                    session.add(child_chunk)

                    milvus_data.append({
                        "chunk_id": child_id,
                        "doc_id": doc_id,
                        # BM25 content 增强：加文件名前缀，帮助 BM25 和 Rerank 区分同结构文档
                        # Dense embedding 不受影响（使用原始 content 生成向量）
                        "content": self._truncate_utf8(
                            f"[{doc_title}] {child_text}" if doc_title else child_text,
                            60000,
                        ),
                        "dense_vector": embed_result.dense_vectors[child_idx],
                        "sparse_vector": embed_result.sparse_vectors[child_idx],
                        "parent_id": parent_id or "",
                        "chunk_index": child_idx,
                        "file_type": meta.file_type,
                        "element_type": meta.element_type,
                        # 租户维度（不参与鉴权，鉴权在 PG 侧 kb_authz 完成）：仅供运维排障
                        # 与将来按租户批量统计/清理。kb_id / session_id 两个 Partition Key
                        # 字段由 MilvusClient._stamp_records 统一兜底，此处不重复。
                        "tenant_id": kb_tenant_id or "",
                    })

                # 幂等确保承载该知识库的物理 collection 存在（单 collection + Partition Key
                # 拓扑下这是全局共享表，首次写入时懒建，之后恒命中"已存在"分支）。
                # 建表时按**该知识库所属租户**的检索配置决定 HNSW 建索引参数；
                # 取不到租户 → get_effective(None) 全默认。
                from app.retrieval.config import get_retrieval_config_store

                cfg = await get_retrieval_config_store().get_effective(kb_tenant_id)
                await self.milvus.ensure_collection(
                    kb_id,
                    ef_construction=cfg.hnsw_ef_construction,
                    m=cfg.hnsw_m,
                )

                # 写入前先清理本文档可能残留的旧向量（幂等，治本去孤儿）。
                # 覆盖三类重处理场景：手动 retry、批量 retry、机制 A 崩溃重投。
                # 上一次处理若在本阶段分批写入途中被硬杀（OOM/SIGKILL），
                # 已写入的批次会成为孤儿向量（DB 未 commit 故无对应 chunk 记录），
                # 且本次 chunk_id 全新，不覆盖旧向量。先按 doc_id 删一次确保干净。
                await self._cleanup_milvus_orphans(kb_id, doc_id)

                # 批量写入 Milvus
                if milvus_data:
                    # Index 写 Milvus 前再查一次取消（复用既有 _check_cancelled）
                    await self._check_cancelled(doc_id)

                    batch_size = 1000
                    total = len(milvus_data)
                    if total <= batch_size:
                        print(f"[Pipeline] 文档 {doc_id} 写入 Milvus，共 {total} 条向量")
                        await self.milvus.insert(kb_id, milvus_data)
                    else:
                        print(f"[Pipeline] 文档 {doc_id} 分批写入 Milvus，共 {total} 条向量，每批 {batch_size} 条")
                        for i in range(0, total, batch_size):
                            # 大批量分批写入时，批次间查取消（对齐 embed 阶段每 N 批查一次）
                            if i > 0 and (i // batch_size) % 10 == 0:
                                await self._check_cancelled(doc_id)
                            batch = milvus_data[i:i + batch_size]
                            await self.milvus.insert(kb_id, batch)
                            if (i // batch_size + 1) % 10 == 0:
                                print(f"[Pipeline] Milvus 写入进度: {min(i + batch_size, total)}/{total}")
                        print(f"[Pipeline] Milvus 写入完成")

                index_duration_ms = int((time.monotonic() - stage_start) * 1000)
                await tracker.complete_stage(PipelineStage.INDEX)
                pl.stage_complete(
                    stage="index",
                    duration_ms=index_duration_ms,
                    input_size=len(milvus_data),
                    output_size=len(milvus_data),
                )

                # ─── 完成 ───
                await self._update_status(session, doc_id, "completed", chunk_count=child_count)
                await session.commit()
                await tracker.complete()

                # 通知其他进程：该知识库有新文档入库（跨进程失效广播）
                from app.storage.invalidation import get_invalidation_bus
                bus = get_invalidation_bus()
                if bus:
                    await bus.publish("kb_data", kb_id)

                # 文档入库完成后非阻塞触发知识图谱抽取（双开关 + 队列门控，任何故障不影响
                # 主入库结果；fire-and-forget 不阻塞 process 返回）。Req 1.1 / 1.2 / 4.1。
                self._fire_graph_extract(doc_id=doc_id, kb_id=kb_id, tenant_id=kb_tenant_id)

                total_duration_ms = int((time.monotonic() - pipeline_start) * 1000)
                pl.summary(total_duration_ms)

                print(f"[Pipeline] 文档 {doc_id} 全部处理完成 ✓ 父块: {len(chunk_result.parent_chunks)}，子块: {child_count}")
                logger.info(
                    "文档 %s 处理完成，父块: %d，子块: %d",
                    doc_id, len(chunk_result.parent_chunks), child_count,
                )

            except CancelledError:
                print(f"[Pipeline] 文档 {doc_id} 处理已取消（文档被删除）")
                logger.info("文档 %s 处理已取消", doc_id)
                await session.rollback()

                # 清理可能已写入 Milvus 的孤儿向量
                # （Index 阶段分批写入时被取消，部分批次已写入 Milvus 但 DB 已 rollback）
                await self._cleanup_milvus_orphans(kb_id, doc_id)
                # 不标记失败，不 re-raise（任务静默终止）

            except UploadCapExceeded as cap_err:
                # KB 异步路径的容量闸门拒绝：发生在 Embed 之前（design C5 / Req 4.2），
                # 不会写入任何向量；此处仅置 Document failed + 写明确 error_message
                # 供前端展示；不调用孤儿清理（无向量可清）。
                print(f"[Pipeline] 文档 {doc_id} 容量超限拒绝: {cap_err}")
                logger.warning("文档 %s 容量超限拒绝: %s", doc_id, cap_err)
                await session.rollback()

                error_message = (
                    f"知识库容量超限：本次 {cap_err.incoming} 个 chunk + 已用 "
                    f"{cap_err.used} 个 chunk 超过单库上限 {cap_err.cap}"
                )
                await tracker.fail(current_stage, error_message)

                async with self.db_session_factory() as err_session:
                    await self._update_status(
                        err_session, doc_id, "failed", error_message=error_message
                    )
                    await err_session.commit()
                # 不 re-raise：业务异常，已落地状态，外围 Worker 不需进入失败重试。

            except Exception as e:
                import traceback
                print(f"[Pipeline] 文档 {doc_id} 处理失败: {type(e).__name__}: {e}")
                traceback.print_exc()
                await session.rollback()

                # 清理可能已写入 Milvus 的孤儿向量（与 CancelledError 分支一致）
                await self._cleanup_milvus_orphans(kb_id, doc_id)

                # 进度追踪：标记失败（progress 值不变）
                await tracker.fail(current_stage, f"{type(e).__name__}: {e}")

                async with self.db_session_factory() as err_session:
                    await self._update_status(
                        err_session, doc_id, "failed", error_message=f"{type(e).__name__}: {e}"
                    )
                    await err_session.commit()
                logger.error("文档 %s 处理失败: %s", doc_id, e)
                raise

    def _fire_graph_extract(
        self, *, doc_id: str, kb_id: str, tenant_id: str | None
    ) -> None:
        """非阻塞触发知识图谱抽取（fire-and-forget）。

        以 ``asyncio.create_task`` 调度 :func:`maybe_trigger_graph_extract`，包一层 try/except
        守卫，使图谱触发的任何失败（双开关关闭、队列不可用、DB 异常等）都不影响文档入库
        主链路的返回（Req 1.1，优雅降级）。``graph_queue`` 为 None 时直接 no-op，不增加成本。
        """
        if self.graph_queue is None:
            return

        from app.pipeline.graph.trigger import maybe_trigger_graph_extract

        async def _run() -> None:
            try:
                await maybe_trigger_graph_extract(
                    kb_id=kb_id,
                    doc_id=doc_id,
                    tenant_id=tenant_id,
                    db_session_factory=self.db_session_factory,
                    graph_queue=self.graph_queue,
                )
            except Exception as e:  # noqa: BLE001 — 图谱触发失败绝不影响主入库结果
                logger.warning("文档 %s 触发知识图谱抽取失败（已忽略，不影响入库）: %s", doc_id, e)

        asyncio.create_task(_run())

    async def _select_chunker(
        self, session: AsyncSession, kb_id: str, file_type: str, content: str
    ):
        """选择 Chunker：优先使用知识库 config 中的 chunker_type，否则自动路由

        分块参数（parent_chunk_size, child_chunk_size, chunk_overlap）按**该知识库所属
        租户**从 RetrievalConfig 读取并传递给 Chunker 实例（Worker 无请求级租户上下文，
        以 KnowledgeBase.tenant_id 为权威来源反查；取不到租户回落全默认）。

        Args:
            session: 数据库会话
            kb_id: 知识库 ID
            file_type: 文件扩展名
            content: 文档文本内容

        Returns:
            BaseChunker 实例
        """
        # 先取该知识库行：一次查询同时拿到 tenant_id（按租户读分块参数）与 config（chunker_type）。
        result = await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        )
        kb = result.scalar_one_or_none()

        # 按该知识库所属租户读取分块参数：Worker 是独立进程、无请求级租户上下文，
        # 以 KnowledgeBase.tenant_id 为权威来源反查租户；取不到租户（kb 为 None 或
        # tenant_id 为 None）→ get_effective(None) 全默认（2500/450/70），与现状一致。
        from app.retrieval.config import get_retrieval_config_store

        kb_tenant_id = kb.tenant_id if kb else None
        cfg = await get_retrieval_config_store().get_effective(kb_tenant_id)
        chunk_kwargs = {
            "parent_size": cfg.parent_chunk_size,
            "child_size": cfg.child_chunk_size,
            "overlap": cfg.chunk_overlap,
        }

        # 查询知识库 config 中是否指定了 chunker_type
        chunker_type = None
        if kb and kb.config and isinstance(kb.config, dict):
            chunker_type = kb.config.get("chunker_type")

        if chunker_type:
            # 手动覆盖：使用知识库配置指定的 Chunker
            logger.info("知识库 %s 手动指定 chunker_type=%s", kb_id, chunker_type)
            try:
                # 只有 naive 类型接受 chunk 参数，其他类型不传
                kwargs = chunk_kwargs if chunker_type == "naive" else {}
                return ChunkerFactory.create(chunker_type, **kwargs)
            except (ValueError, TypeError):
                logger.warning(
                    "知识库 %s 指定的 chunker_type=%s 创建失败，回退到自动路由",
                    kb_id, chunker_type,
                )

        # 自动路由：根据文件类型和内容特征选择
        selected_type = ChunkerRouter.select(file_type, content)
        logger.info("自动路由选择 chunker_type=%s (file_type=%s)", selected_type, file_type)
        # 只有 naive 类型接受 chunk 参数
        kwargs = chunk_kwargs if selected_type == "naive" else {}
        return ChunkerFactory.create(selected_type, **kwargs)

    async def _embed_with_progress(
        self,
        texts: list[str],
        tracker: ProgressTracker | None = None,
        doc_id: str = "",
    ):
        """带进度追踪的 embed 调用

        逐批调用 embedder，每完成一批更新子进度到数据库，
        前端轮询时能看到实时进度。

        Args:
            texts: 待向量化的文本列表
            tracker: 进度追踪器
            doc_id: 文档 ID（用于日志）

        Returns:
            EmbedResult
        """
        from app.pipeline.embedder import EmbedResult

        if not texts:
            return EmbedResult(dense_vectors=[], sparse_vectors=[])

        batch_size = self.embedder.batch_size
        total_batches = (len(texts) + batch_size - 1) // batch_size

        # 批次较少时直接一次性处理
        if total_batches <= 2:
            result = await self.embedder.embed(texts)
            if tracker is not None:
                await tracker.update_sub_progress(
                    PipelineStage.EMBED, total_batches, total_batches,
                    f"正在生成向量 ({total_batches}/{total_batches} 批)"
                )
            return result

        # 批次较多时，逐批处理并实时更新进度
        import asyncio
        import math

        # 清洗和截断文本（复用 embedder 的逻辑）
        sanitized = self.embedder._sanitize_texts(texts)
        sanitized = self.embedder._truncate_texts(sanitized)

        # 构建批次
        batches = []
        for i in range(0, len(sanitized), batch_size):
            batches.append(sanitized[i:i + batch_size])

        print(f"[Pipeline] 文档 {doc_id} embed 逐批处理: {total_batches} 批, batch_size={batch_size}, 全局并发={self.embedder.concurrency}, 单文档并发={self.embedder.per_doc_concurrency}")
        import sys
        sys.stdout.flush()

        # 并发控制：全局信号量（进程级，所有文档共享，保护远程服务）+ 单文档信号量
        # （限制本文档占用的全局 slot 数，保证多文档交错执行、小文件不被大文件饿死）。
        from app.pipeline.concurrency import get_embed_semaphore
        global_sem = get_embed_semaphore()
        doc_sem = asyncio.Semaphore(self.embedder.per_doc_concurrency)
        results: list[tuple[list[list[float]], list[dict[int, float]]] | None] = [None] * len(batches)
        completed_count = 0
        progress_lock = asyncio.Lock()
        _cancelled = False  # 共享取消标志，让所有批次协程快速退出

        # 获取 provider 引用（避免在并发中反复通过 property 获取）
        provider = self.embedder.provider
        print(f"[Pipeline] 文档 {doc_id} provider 类型: {type(provider).__name__}, 开始提交批次任务")
        sys.stdout.flush()

        async def _process_batch(batch_idx: int, batch: list[str]):
            nonlocal completed_count, _cancelled

            # 快速退出：如果已取消，不再发送请求
            if _cancelled:
                return

            async with doc_sem:
                async with global_sem:
                    # 获得 semaphore 后再次检查（可能在等待期间被取消）
                    if _cancelled:
                        return
                    if batch_idx == 0:
                        print(f"[Pipeline] 文档 {doc_id} 第一批进入 semaphore，开始调用 embed...")
                        sys.stdout.flush()
                    dense = await provider.embed(batch)
                    if _cancelled:
                        return
                    if batch_idx == 0:
                        print(f"[Pipeline] 文档 {doc_id} 第一批 embed 返回，dense 长度: {len(dense)}")
                        sys.stdout.flush()
                    sparse = await provider.embed_sparse(batch)
                    if _cancelled:
                        return
                    results[batch_idx] = (dense, sparse)

            # 让出事件循环
            await asyncio.sleep(0)

            # 更新进度
            async with progress_lock:
                completed_count += 1
                print(f"[Embedder] 批次 {completed_count}/{total_batches} 完成 ({completed_count * 100 // total_batches}%，本批 {len(batch)} 个文本)")
                # 每 5 批或最后一批更新一次 DB 进度（避免过于频繁的 DB 写入）
                if tracker is not None and (completed_count % 5 == 0 or completed_count == total_batches):
                    await tracker.update_sub_progress(
                        PipelineStage.EMBED, completed_count, total_batches,
                        f"正在生成向量 ({completed_count}/{total_batches} 批)"
                    )

                # 每 5 批检查一次是否已取消（平衡响应速度和 DB 查询开销）
                # 仅在有 doc_id（正式 Document 路径）时检查；会话同步路径无 Document 行，跳过。
                if doc_id and completed_count % 5 == 0:
                    try:
                        async with self.db_session_factory() as check_session:
                            r = await check_session.execute(
                                select(Document.status).where(Document.id == doc_id)
                            )
                            st = r.scalar_one_or_none()
                            if st is None or st == "cancelled":
                                _cancelled = True
                                raise CancelledError(f"文档 {doc_id} 已被取消或删除")
                    except CancelledError:
                        raise
                    except Exception:
                        pass  # 取消检查失败不影响主流程

        await asyncio.gather(*[_process_batch(i, batch) for i, batch in enumerate(batches)])

        # 合并结果
        all_dense: list[list[float]] = []
        all_sparse: list[dict[int, float]] = []
        for dense, sparse in results:
            all_dense.extend(dense)
            all_sparse.extend(sparse)

        # 修复 NaN
        has_nan = False
        for idx, vec in enumerate(all_dense):
            if any(math.isnan(v) or math.isinf(v) for v in vec):
                has_nan = True
                all_dense[idx] = self.embedder._fix_nan_vector(vec)
        for idx, sp in enumerate(all_sparse):
            if not sp or any(math.isnan(v) or math.isinf(v) for v in sp.values()):
                has_nan = True
                all_sparse[idx] = self.embedder._fix_nan_sparse(sp)
        if has_nan:
            logger.warning("检测到 embedding 结果包含 NaN，已修复")

        print(f"[Pipeline] 文档 {doc_id} embed 完成，共 {len(all_dense)} 个向量")
        return EmbedResult(dense_vectors=all_dense, sparse_vectors=all_sparse)

    async def _process_embedded_images(
        self, load_result: LoadResult, doc_id: str
    ) -> str:
        """处理嵌入图片：并发 OCR 识别，按页位置将图片文本插入到对应页面文本之后

        对 PDF 中已由可信文本层覆盖的整页扫描底图跳过 OCR，避免同一页的隐藏文本层
        与扫描图 OCR 结果被拼接成双份正文。普通嵌入图片仍按原有规则处理。

        Args:
            load_result: 文档加载结果
            doc_id: 文档 ID

        Returns:
            合并了图片 OCR 文本的最终文档文本
        """
        images = [
            image for image in load_result.images
            if not image.is_page_background
        ]
        skipped_page_backgrounds = len(load_result.images) - len(images)
        if skipped_page_backgrounds:
            logger.info(
                "文档 %s 跳过 %d 张整页扫描底图 OCR（页面已有可信文本层）",
                doc_id,
                skipped_page_backgrounds,
            )
        if not images:
            return load_result.content

        logger.info("文档 %s 开始并发处理 %d 张嵌入图片 OCR", doc_id, len(images))

        # 并发 OCR 识别所有图片
        ocr_results = await self._concurrent_ocr_images(images, doc_id)

        # 按页码分组图片 OCR 文本
        page_image_texts: dict[int, list[str]] = defaultdict(list)
        for img, ocr_text in zip(images, ocr_results):
            if ocr_text:
                page_image_texts[img.page_or_index].append(ocr_text)

        if not page_image_texts:
            return load_result.content

        # 如果有按页文本，按页插入图片 OCR 文本
        if load_result.page_texts:
            merged_pages: list[str] = []
            for page_idx, page_text in enumerate(load_result.page_texts):
                merged_pages.append(page_text)
                page_num = page_idx + 1
                if page_num in page_image_texts:
                    img_section = "\n".join(
                        f"[图片内容]\n{txt}" for txt in page_image_texts[page_num]
                    )
                    merged_pages.append(img_section)

            # 处理可能超出页码范围的图片（如 docx 的序号索引）
            for page_num, texts in page_image_texts.items():
                if page_num > len(load_result.page_texts):
                    img_section = "\n".join(
                        f"[图片内容]\n{txt}" for txt in texts
                    )
                    merged_pages.append(img_section)

            final_content = "\n\n".join(merged_pages)
        else:
            # 没有按页文本（如 docx 只有段落），追加到末尾
            all_img_texts = []
            for page_num in sorted(page_image_texts.keys()):
                for txt in page_image_texts[page_num]:
                    all_img_texts.append(f"[图片内容 - 第{page_num}张]\n{txt}")
            final_content = load_result.content + "\n\n" + "\n\n".join(all_img_texts)

        logger.info(
            "文档 %s 图片 OCR 完成，成功识别 %d/%d 张，最终文本长度: %d",
            doc_id,
            sum(1 for r in ocr_results if r),
            len(images),
            len(final_content),
        )

        return final_content

    async def _transcribe_with_limit(self, file_path: str):
        """整文件 ASR：通过进程级全局 ASR 信号量限流后调用 asr_manager.transcribe。

        所有文档共享同一个全局 ASR 信号量，保证对远程 ASR 服务的总并发恒定可控。
        """
        from app.pipeline.concurrency import get_asr_semaphore
        async with get_asr_semaphore():
            return await self.asr_manager.transcribe(file_path)

    async def _concurrent_ocr_images(
        self, images: list[EmbeddedImage], doc_id: str
    ) -> list[str]:
        """并发调用 OCR 识别多张嵌入图片

        并发上限由 ``OCRManager.recognize`` 内部的进程级全局 OCR 信号量统一控制
        （限流职责归 Manager，此处不再自持信号量——否则与 Manager 内层形成嵌套，
        在并发度为 1 时会自锁）。

        Args:
            images: 嵌入图片列表
            doc_id: 文档 ID（用于日志）

        Returns:
            与 images 等长的列表，每个元素为 OCR 识别文本（失败为空字符串）
        """

        async def _ocr_single(idx: int, img: EmbeddedImage) -> str:
            try:
                ocr_result = await self.ocr_manager.recognize(img.file_path)
                text = ocr_result.full_text.strip()
                if text:
                    logger.debug(
                        "文档 %s 图片 %d/%d OCR 成功，文本长度: %d",
                        doc_id, idx + 1, len(images), len(text),
                    )
                return text
            except Exception as e:
                logger.warning(
                    "文档 %s 图片 %d/%d OCR 失败: %s",
                    doc_id, idx + 1, len(images), e,
                )
                return ""

        tasks = [_ocr_single(i, img) for i, img in enumerate(images)]
        results = await asyncio.gather(*tasks)
        return list(results)

    @staticmethod
    def _cleanup_image_temp_dirs(images: list[EmbeddedImage]) -> None:
        """清理图片临时文件和目录

        从图片路径推断临时目录并删除整个目录。

        Args:
            images: 嵌入图片列表
        """
        if not images:
            return

        # 收集所有临时目录（去重）
        tmp_dirs: set[str] = set()
        for img in images:
            if img.file_path and os.path.exists(img.file_path):
                tmp_dirs.add(os.path.dirname(img.file_path))

        for tmp_dir in tmp_dirs:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                logger.debug("已清理图片临时目录: %s", tmp_dir)
            except Exception as e:
                logger.warning("清理临时目录失败 %s: %s", tmp_dir, e)

    @staticmethod
    def _find_parent(parent_child_map: dict[int, list[int]], child_idx: int) -> int | None:
        """根据子 chunk 索引查找对应的父 chunk 索引"""
        for parent_idx, children in parent_child_map.items():
            if child_idx in children:
                return parent_idx
        return None

    async def _cleanup_milvus_orphans(self, kb_id: str, doc_id: str) -> None:
        """按 doc_id 清理 Milvus 向量。拒绝空 kb_id/doc_id（防误删整库）。失败记 WARNING。

        删除范围由 ``MilvusClient`` 注入的 ``kb_id == "..."`` Partition Key 条件与本处
        ``doc_id`` 条件共同约束，单 collection 拓扑下不会波及其它知识库。

        统一用于：
        - Index 阶段写入前先删旧（幂等，治本去孤儿）
        - CancelledError 分支回滚后清理已写入的孤儿向量
        - 普通 Exception 分支回滚后清理已写入的孤儿向量
        """
        if not doc_id or not kb_id:
            logger.warning("跳过孤儿清理：kb_id/doc_id 为空")
            return
        try:
            # has_collection 在单 collection 拓扑下表示"承载表是否已建"，
            # 未建表则必然无向量可删，直接返回。
            if not await self.milvus.has_collection(kb_id):
                return
            await self.milvus.delete_by_doc_id(kb_id, doc_id)
            logger.info("文档 %s 孤儿向量清理完成", doc_id)
        except Exception as e:
            logger.warning("文档 %s 孤儿向量清理失败（非致命）: %s", doc_id, e)

    # 保留旧名作为向后兼容别名
    async def _cleanup_milvus_on_cancel(self, kb_id: str, doc_id: str) -> None:
        """向后兼容别名，委托给 _cleanup_milvus_orphans"""
        await self._cleanup_milvus_orphans(kb_id, doc_id)

    @staticmethod
    def _truncate_utf8(text: str, max_bytes: int = 60000) -> str:
        """按 UTF-8 字节数截断字符串，确保不超过 Milvus VarChar 字节限制。

        Milvus 的 max_length 实际按 UTF-8 字节数计算，中文字符占 3 字节，
        因此不能简单按 Python 字符数截断。
        """
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    @staticmethod
    def _build_child_to_parent_map(parent_child_map: dict[int, list[int]]) -> dict[int, int]:
        """构建子→父的反向索引，O(n) 构建，O(1) 查找"""
        child_to_parent = {}
        for parent_idx, children in parent_child_map.items():
            for child_idx in children:
                child_to_parent[child_idx] = parent_idx
        return child_to_parent

    @staticmethod
    async def _update_status(
        session: AsyncSession,
        doc_id: str,
        status: str,
        chunk_count: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """更新文档状态"""
        result = await session.execute(select(Document).where(Document.id == doc_id))
        doc = result.scalar_one_or_none()
        if doc is None:
            return
        doc.status = status
        if chunk_count is not None:
            doc.chunk_count = chunk_count
        if error_message is not None:
            doc.error_message = error_message

    async def _check_cancelled(self, doc_id: str) -> None:
        """检查文档是否已被取消/删除，是则抛出异常终止处理"""
        async with self.db_session_factory() as session:
            result = await session.execute(
                select(Document.status).where(Document.id == doc_id)
            )
            status = result.scalar_one_or_none()
            if status is None or status == "cancelled":
                raise CancelledError(f"文档 {doc_id} 已被取消或删除")
