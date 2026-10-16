"""文档上传与管理接口"""

import asyncio
import logging
import os
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.api.deps import get_db_session, require_authenticated, require_member
from app.api.errors import CrossTenantError, FileTooLargeError, PermissionDeniedError
from app.api.validators import NameValidationError, validate_filename, validate_folder_name
from app.auth.identity import IdentityContext
from app.auth.kb_authz import GrantView, KbAccessEnum, kb_authorization_decision
from app.models.manager import get_model_manager
from app.pipeline.pipeline import DocumentPipeline
from app.pipeline.queue import TaskMessage, TaskQueue
from app.schema.api import PageResult
from app.schema.db import (
    Chunk,
    Document,
    DocumentSummary,
    Folder,
    KnowledgeBase,
    KnowledgeBaseGrant,
    SessionChunk,
    SessionFile,
)
from app.session_upload.limits import get_upload_limit_resolver
from app.storage.database import async_session
from app.storage.milvus import MilvusClient, get_milvus_client
from app.storage.object_store import (
    document_object_key,
    get_object_store,
    thumbnail_object_key,
)

logger = logging.getLogger(__name__)


# Redis 降级时的进程内回退并发上限：防止 Redis 不可用时，大量上传一起涌入
# API 进程把事件循环/内存压垮。超过上限的任务会等待空闲额度（而非无限堆积）。
# 注意：正常路径走 Redis + 独立 Worker，根本不触发此回退；这是降级路径的护栏。
_FALLBACK_MAX_CONCURRENT = 2
_fallback_semaphore = asyncio.Semaphore(_FALLBACK_MAX_CONCURRENT)


async def _run_pipeline_fallback(
    file_path: str, doc_id: str, kb_id: str, object_key: str | None = None
) -> None:
    """进程内回退执行（受 _fallback_semaphore 限流）。

    仅在 Redis 不可用时使用。受限流保护：同一时刻最多 _FALLBACK_MAX_CONCURRENT 个
    文档在 API 进程内处理，其余排队，避免降级时压垮 API。pipeline 内部 load/clean/
    chunk 已用 to_thread 卸载，embedding 为 async I/O，故不会独占事件循环。

    object_key 存在时从 MinIO 下载到临时文件处理；否则回退用 file_path。
    """
    async with _fallback_semaphore:
        if object_key:
            from app.storage.object_store import materialized_file

            suffix = os.path.splitext(object_key)[1]
            async with materialized_file(object_key, suffix) as local_path:
                await _run_pipeline_safe(local_path, doc_id, kb_id)
        else:
            await _run_pipeline_safe(file_path, doc_id, kb_id)


async def _safe_remove_objects(keys: list[str]) -> None:
    """删除 MinIO 对象（幂等，失败仅记 WARNING）。用于上传失败的一致性补偿。"""
    try:
        store = get_object_store()
        if store is not None and keys:
            await store.remove_many(keys)
    except Exception as e:  # noqa: BLE001
        logger.warning("一致性补偿删除对象失败 keys=%s: %s", keys, e)


async def _authorize_kb_access(
    db: AsyncSession, identity: IdentityContext, kb_id: str, access: KbAccessEnum
) -> KnowledgeBase:
    """加载 KB 并经唯一授权判定（读404不泄露 / 写403）。返回已授权的 KB。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise CrossTenantError()
    grant_rows = await db.execute(
        select(
            KnowledgeBaseGrant.grantee_type,
            KnowledgeBaseGrant.grantee_id,
            KnowledgeBaseGrant.permission,
        ).where(KnowledgeBaseGrant.kb_id == kb_id)
    )
    grants = [GrantView(gt, gid, perm) for gt, gid, perm in grant_rows.all()]
    decision = kb_authorization_decision(
        identity,
        kb_id=kb.id, kb_tenant_id=kb.tenant_id, kb_owner_user_id=kb.owner_user_id,
        kb_visibility=kb.visibility, kb_org_permission=kb.org_permission,
        access=access, grants=grants,
    )
    if not decision.allow:
        if decision.http_status == 403:
            raise PermissionDeniedError()
        raise CrossTenantError()
    return kb


async def _kb_write_allowed(
    db: AsyncSession, identity: IdentityContext, kb: KnowledgeBase
) -> bool:
    """判断当前身份对该 KB 是否有内容写权限（owner / 组织读写 / write 共享）。

    复用唯一判定纯函数 kb_authorization_decision(WRITE)，不另起规则。
    用于：决定只读访客的文档列表是否过滤、前端是否显示写操作入口。
    """
    grant_rows = await db.execute(
        select(
            KnowledgeBaseGrant.grantee_type,
            KnowledgeBaseGrant.grantee_id,
            KnowledgeBaseGrant.permission,
        ).where(KnowledgeBaseGrant.kb_id == kb.id)
    )
    grants = [GrantView(gt, gid, perm) for gt, gid, perm in grant_rows.all()]
    decision = kb_authorization_decision(
        identity,
        kb_id=kb.id, kb_tenant_id=kb.tenant_id, kb_owner_user_id=kb.owner_user_id,
        kb_visibility=kb.visibility, kb_org_permission=kb.org_permission,
        access=KbAccessEnum.WRITE, grants=grants,
    )
    return decision.allow


def _ensure_not_super_admin_content(identity: IdentityContext) -> None:
    """Content_View_Boundary：Super_Admin 默认不可读业务内容正文（R34）。

    可按 content_view_boundary_open 配置放宽（v1 默认关闭）。
    """
    if identity.is_super_admin and not get_settings().content_view_boundary_open:
        raise PermissionDeniedError("超级管理员默认不可查看业务内容正文")

router = APIRouter(tags=["Document"])

# 支持的文件类型（含音频，音频走 ASR 语音转写链路）
_ALLOWED_EXTENSIONS = {
    "pdf", "doc", "docx", "xlsx", "pptx", "csv", "txt", "md", "jpg", "jpeg", "png",
    "mp3", "wav", "m4a", "flac", "ogg",
}

async def _generate_and_store_thumbnail(
    doc_id: str,
    file_type: str,
    content: bytes,
    cover_image: bytes | None = None,
) -> None:
    """生成缩略图并存入 MinIO。失败不影响上传主流程（事件循环外渲染）。

    - PDF：fitz 渲染首页。
    - 图片：预览直接返回原件，无需缩略图。
    - md / txt：方案 A 优先用 ``cover_image``（链接转存抓到的封面图）转 PNG；
      无封面图时方案 B 兜底——用 fitz 把「标题 + 正文摘要」渲染成文字卡片，
      保证所有 md/txt 文档都有一致预览图（含用户手动上传的）。
    """
    store = get_object_store()
    if store is None:
        return

    if file_type == "pdf":
        def _render_pdf() -> bytes | None:
            import fitz

            try:
                pdf_doc = fitz.open(stream=content, filetype="pdf")
                page = pdf_doc[0]
                zoom = 200.0 / page.rect.width
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat)
                data = pix.tobytes("png")
                pdf_doc.close()
                return data
            except Exception as e:  # noqa: BLE001
                logger.warning("生成 PDF 缩略图失败 doc_id=%s: %s", doc_id, e)
                return None

        png_bytes = await asyncio.to_thread(_render_pdf)
    elif file_type in ("md", "txt"):
        def _render_text() -> bytes | None:
            from app.pipeline.thumbnail import image_bytes_to_png, render_text_card

            # 方案 A：封面图优先。
            if cover_image:
                png = image_bytes_to_png(cover_image)
                if png:
                    return png
            # 方案 B：文字卡片兜底。标题取首个 markdown 一级标题或首行，正文取全文。
            text = content.decode("utf-8", errors="ignore")
            title, body = _split_title_body(text)
            return render_text_card(title, body)

        png_bytes = await asyncio.to_thread(_render_text)
    else:
        return

    try:
        if png_bytes:
            await store.put_bytes(
                thumbnail_object_key(doc_id), png_bytes, content_type="image/png"
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("存储缩略图失败 doc_id=%s: %s", doc_id, e)


def _split_title_body(text: str) -> tuple[str, str]:
    """从 markdown/纯文本中拆出标题与正文，用于文字卡片渲染。

    标题取首个一级标题（``# xxx``）或首个非空行；正文为去掉该标题后的剩余文本。
    """
    title = ""
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        title = line.lstrip("#").strip() or line
        break
    return title, text


# ============================================================
# 响应模型
# ============================================================


class DocumentResponse(BaseModel):
    """文档响应"""
    model_config = {"from_attributes": True}

    id: str
    kb_id: str
    filename: str
    file_type: str
    file_size: int | None
    status: str
    error_message: str | None
    chunk_count: int
    progress: float = 0
    progress_message: str | None = None
    source_url: str | None = None
    created_at: str


class ChunkResponse(BaseModel):
    """切片响应"""
    model_config = {"from_attributes": True}

    id: str
    doc_id: str
    kb_id: str
    parent_id: str | None
    content: str
    chunk_index: int | None
    created_at: str
    children: list[str] = []  # 子块内容列表，用于前端高亮


class DocumentEventResponse(BaseModel):
    """文档抽取事件响应（文档详情事件展示，Requirements 4.2）。

    事件中心图谱从每个 chunk 抽取的完整语义单元，含标题/摘要/正文及关联实体名，
    供文档处理结果页展示。
    """

    id: str
    title: str
    summary: str
    content: str
    chunk_id: str
    entity_names: list[str] = []


# ============================================================
# 辅助函数
# ============================================================


def _get_milvus() -> MilvusClient:
    """获取 Milvus 客户端"""
    return get_milvus_client()


async def _describe_doc_location(db: AsyncSession, folder_id: str | None) -> str:
    """把文档所在位置翻译成用户可读描述，用于「文件已存在」提示。

    去重为 KB 级（同一知识库内相同内容只保留一份，与 WeKnora 一致），命中的既有
    文档可能不在用户当前所处文件夹，故提示需带上它的实际位置，避免用户在当前文件夹
    看不到该文件时的困惑。folder_id 为空或文件夹已不存在时归为「根目录」。
    """
    if not folder_id:
        return "根目录"
    folder = await db.get(Folder, folder_id)
    if folder is None:
        return "根目录"
    return f"「{folder.name}」文件夹"


async def _run_pipeline(file_path: str, doc_id: str, kb_id: str) -> None:
    """后台执行文档处理管道"""
    try:
        print(f"[Pipeline] 文档 {doc_id} 开始处理，文件: {file_path}")
        manager = get_model_manager()
        milvus = _get_milvus()

        # 从数据库加载 OCR / ASR 配置（本降级路径每篇文档重建，本就取最新配置）
        from app.startup import load_asr_manager, load_ocr_manager

        ocr_manager = await load_ocr_manager()
        asr_manager = await load_asr_manager()
        from app.summary.queue import get_document_summary_queue

        pipeline = DocumentPipeline(
            model_manager=manager,
            milvus_client=milvus,
            db_session_factory=async_session,
            ocr_manager=ocr_manager,
            asr_manager=asr_manager,
            summary_queue=get_document_summary_queue(),
        )
        await pipeline.process(file_path, doc_id, kb_id)
        print(f"[Pipeline] 文档 {doc_id} 处理完成")
    except Exception as e:
        import traceback
        print(f"[Pipeline] 文档 {doc_id} 管道处理失败: {type(e).__name__}: {e}")
        traceback.print_exc()
        logger.error("文档 %s 管道处理失败: %s", doc_id, e)


async def _run_pipeline_safe(file_path: str, doc_id: str, kb_id: str) -> None:
    """安全包装，捕获异常避免 task 崩溃。处理成功后清除该知识库的检索缓存。"""
    try:
        await _run_pipeline(file_path, doc_id, kb_id)
        # 文档处理成功，清除该知识库的检索缓存
        from app.retrieval.cache import get_retrieval_cache
        cache = await get_retrieval_cache()
        if cache:
            await cache.invalidate_kb(kb_id)
    except Exception as e:
        import traceback
        print(f"[Pipeline] 文档 {doc_id} 管道处理异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        logger.error("文档 %s 管道处理异常: %s", doc_id, e)


def _get_task_queue(request: Request) -> TaskQueue | None:
    """从 app.state 获取快道 TaskQueue 实例，不存在或为 None 时返回 None"""
    return getattr(request.app.state, "task_queue", None)


def _get_slow_task_queue(request: Request) -> TaskQueue | None:
    """从 app.state 获取慢道 TaskQueue 实例（大文件），不存在时返回 None"""
    return getattr(request.app.state, "slow_task_queue", None)


def _select_queue(
    request: Request, file_size: int | None
) -> TaskQueue | None:
    """按文件大小选择入队队列：大文件走慢道，其余走快道。

    慢道不可用（未初始化）时回退到快道。返回 None 表示 Redis 不可用，
    调用方应降级为进程内处理。
    """
    fast = _get_task_queue(request)
    if fast is None:
        return None
    settings = get_settings()
    threshold = settings.pipeline_slow_lane_min_mb * 1024 * 1024
    if file_size is not None and file_size >= threshold:
        slow = _get_slow_task_queue(request)
        if slow is not None:
            return slow
    return fast


async def _enqueue_or_fallback(
    request: Request, file_path: str, doc_id: str, kb_id: str,
    file_size: int | None = None, tenant_id: str | None = None,
    object_key: str | None = None,
) -> None:
    """尝试将任务入队 Redis Stream（按大小选择快/慢道），失败时降级为 asyncio.create_task"""
    queue = _select_queue(request, file_size)
    if queue is not None:
        try:
            msg = TaskMessage(
                doc_id=doc_id, kb_id=kb_id, file_path=file_path,
                tenant_id=tenant_id, object_key=object_key,
            )
            msg_id = await queue.enqueue(msg)
            print(f"[Queue] 文档 {doc_id} 已入队 Redis Stream (msg_id={msg_id})")
            return
        except Exception as e:
            print(f"[Queue] ⚠️ Redis 入队失败，降级为 create_task: {e}")
            logger.warning(
                "Redis unavailable, falling back to in-process task: %s", e
            )
    else:
        print(f"[Queue] ⚠️ Redis 不可用，降级为 create_task (doc_id={doc_id})")
        logger.warning("Redis unavailable, falling back to in-process task")
    # 降级：进程内执行（受 _fallback_semaphore 限流，防止压垮 API）
    asyncio.create_task(_run_pipeline_fallback(file_path, doc_id, kb_id, object_key))


# ============================================================
# 接口实现
# ============================================================


@router.get("/api/knowledge-bases/{kb_id}/documents", response_model=PageResult[DocumentResponse])
async def list_documents(
    kb_id: str,
    folder_id: str | None = None,
    page: int = 1,
    page_size: int = 20,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """获取知识库下的文档列表（支持按文件夹过滤 + 分页/滚动加载）。

    只读访客（无写权限：组织只读/共享只读/管理员看他人私有库）仅返回 completed 文档——
    未完成/失败的文档是库主内务，对只读访客无意义且无操作入口，故不展示（方案 A）。
    有写权限者（owner/组织读写/write 共享）看到全部状态文档，保留重试入口。
    """
    # 先校验对该 KB 的读权限（跨租户/不可读 -> 404）
    kb = await _authorize_kb_access(db, identity, kb_id, KbAccessEnum.READ)
    can_write = await _kb_write_allowed(db, identity, kb)

    # 参数兜底
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size

    # 按文件夹过滤
    if folder_id:
        cond = [Document.kb_id == kb_id, Document.folder_id == folder_id]
    else:
        cond = [Document.kb_id == kb_id, Document.folder_id.is_(None)]
    # 只读访客：仅展示已完成文档
    if not can_write:
        cond.append(Document.status == "completed")

    # 总数
    total = await db.scalar(select(func.count(Document.id)).where(*cond)) or 0

    # 排序：completed > failed > processing > pending，同状态按创建时间倒序
    from sqlalchemy import case
    status_order = case(
        (Document.status == "completed", 0),
        (Document.status == "failed", 1),
        (Document.status == "processing", 2),
        (Document.status == "pending", 3),
        else_=4,
    )
    result = await db.execute(
        select(Document)
        .where(*cond)
        .order_by(status_order, Document.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    docs = result.scalars().all()
    items = [
        DocumentResponse(
            id=d.id,
            kb_id=d.kb_id,
            filename=d.filename,
            file_type=d.file_type,
            file_size=d.file_size,
            status=d.status,
            error_message=d.error_message,
            chunk_count=d.chunk_count,
            progress=d.progress or 0,
            progress_message=d.progress_message,
            source_url=d.source_url,
            created_at=d.created_at.isoformat() if d.created_at else "",
        )
        for d in docs
    ]
    return PageResult[DocumentResponse](
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=offset + len(items) < total,
    )


@router.post("/api/knowledge-bases/{kb_id}/documents/upload", response_model=DocumentResponse, status_code=201)
async def upload_document(
    kb_id: str,
    request: Request,
    file: UploadFile = File(...),
    folder_id: str | None = None,
    identity: IdentityContext = Depends(require_member()),
    db: AsyncSession = Depends(get_db_session),
):
    """上传文档（multipart/form-data），支持指定文件夹"""
    # 先校验对该 KB 的写权限（跨租户404 / 无写权403）
    kb = await _authorize_kb_access(db, identity, kb_id, KbAccessEnum.WRITE)

    # 校验文件名
    filename = file.filename or "unknown"
    try:
        filename = validate_filename(filename)
    except NameValidationError as e:
        raise HTTPException(status_code=422, detail=e.message)

    # 验证文件类型
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {ext}，支持: {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
        )

    # 源文件权威存储在 MinIO，对象 key = {doc_id}.{ext}
    store = get_object_store()
    if store is None:
        raise HTTPException(status_code=503, detail="对象存储不可用，无法上传文件")
    doc_id = str(uuid.uuid4())
    object_key = document_object_key(doc_id, ext)

    content = await file.read()
    file_size = len(content)

    # 文件大小校验：按租户级 Upload_File_Size_Limit 拦截（session-file-upload Req 3.2/3.5）。
    # 真实校验来源已由前端展示用的 Settings.upload_max_file_size_mb 切到租户级配置，
    # 经 UploadLimitResolver 即时热生效；超限在落盘/入队前拒绝，超限文案带允许上限。
    limits = await get_upload_limit_resolver().resolve(identity.tenant_id)
    if file_size > limits.upload_max_file_bytes:
        raise FileTooLargeError.from_limit(limits.upload_max_file_bytes)

    # 计算文件哈希，检测重复
    import hashlib
    file_hash = hashlib.sha256(content).hexdigest()
    existing = await db.execute(
        select(Document).where(
            Document.kb_id == kb_id,
            Document.file_hash == file_hash,
        )
    )
    existing_doc = existing.scalar_one_or_none()
    if existing_doc is not None:
        location = await _describe_doc_location(db, existing_doc.folder_id)
        return DocumentResponse(
            id=existing_doc.id,
            kb_id=existing_doc.kb_id,
            filename=existing_doc.filename,
            file_type=existing_doc.file_type,
            file_size=existing_doc.file_size,
            status="duplicate",
            error_message=f"该文件已存在于{location}（与 {existing_doc.filename} 内容相同）",
            chunk_count=existing_doc.chunk_count,
            created_at=existing_doc.created_at.isoformat() if existing_doc.created_at else "",
        )

    # 写入 MinIO（权威存储）
    await store.put_bytes(object_key, content, content_type=file.content_type or "application/octet-stream")

    # 创建文档记录（盖章 tenant_id = 所属 KB 的 tenant_id）。
    # 一致性补偿：MinIO 已写入但 DB 写失败时，删除刚上传的对象，避免留孤儿对象
    # （与会话上传路径对称；兜底另有启动期 reconcile 对账，见 main._reconcile_orphan_objects）。
    try:
        doc = Document(
            id=doc_id,
            kb_id=kb_id,
            folder_id=folder_id,
            filename=filename,
            file_type=ext,
            file_size=file_size,
            file_hash=file_hash,
            status="pending",
            tenant_id=kb.tenant_id,
        )
        db.add(doc)

        # 更新知识库文档计数
        kb.doc_count = (kb.doc_count or 0) + 1

        await db.flush()
        await db.refresh(doc)
        await db.commit()
    except Exception:
        await _safe_remove_objects([object_key])
        raise

    # 生成缩略图（PDF 首页渲染）并存入 MinIO
    await _generate_and_store_thumbnail(doc_id, ext, content)

    # 后台触发管道处理（按文件大小路由快/慢道，优先入队 Redis Stream，降级为 asyncio.create_task）
    await _enqueue_or_fallback(
        request, object_key, doc_id, kb_id,
        file_size=file_size, tenant_id=kb.tenant_id, object_key=object_key,
    )

    return DocumentResponse(
        id=doc.id,
        kb_id=doc.kb_id,
        filename=doc.filename,
        file_type=doc.file_type,
        file_size=doc.file_size,
        status=doc.status,
        error_message=doc.error_message,
        chunk_count=doc.chunk_count,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
    )


class UrlImportRequest(BaseModel):
    """链接转存请求：粘贴一个网页链接，抓取正文转存进知识库。"""
    url: str
    folder_id: str | None = None


@router.post(
    "/api/knowledge-bases/{kb_id}/documents/from-url",
    response_model=DocumentResponse,
    status_code=201,
)
async def import_document_from_url(
    kb_id: str,
    request: Request,
    body: UrlImportRequest,
    identity: IdentityContext = Depends(require_member()),
    db: AsyncSession = Depends(get_db_session),
):
    """从网页链接抓取正文转存为知识库文档（移动端核心入口）。

    抓取链接 → trafilatura 提取正文为 Markdown → 当作一篇 ``.md`` 文档复用既有上传
    管线（存 MinIO → 入队/回退 → load→chunk→embed→index→可选图谱抽取）。支持通用
    网页文章与微信公众号永久图文链接；登录态/动态渲染/强反爬页面会返回明确失败提示。

    与 ``upload_document`` 共用同一套写权限校验、容量/重复判定与入库触发逻辑，
    仅来源从「上传文件字节」换成「抓取的网页正文」。
    """
    from app.pipeline.url_fetcher import (
        UrlFetchError,
        download_image,
        fetch_article,
        safe_filename_from_title,
    )

    # 写权限校验（跨租户404 / 无写权403），与上传一致。
    kb = await _authorize_kb_access(db, identity, kb_id, KbAccessEnum.WRITE)

    # 抓取并提取正文（失败转 422，detail 为明确中文原因）。
    try:
        article = await fetch_article(body.url)
    except UrlFetchError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # 正文转字节，按 .md 文档处理。
    content = article.markdown.encode("utf-8")
    file_size = len(content)
    ext = "md"

    # 文件名取标题（去非法字符、限长），并经统一文件名校验兜底。
    filename = safe_filename_from_title(article.title)
    try:
        filename = validate_filename(filename)
    except NameValidationError:
        filename = "网页内容.md"

    # 文件大小校验（与上传同一租户级上限）。
    limits = await get_upload_limit_resolver().resolve(identity.tenant_id)
    if file_size > limits.upload_max_file_bytes:
        raise FileTooLargeError.from_limit(limits.upload_max_file_bytes)

    store = get_object_store()
    if store is None:
        raise HTTPException(status_code=503, detail="对象存储不可用，无法转存内容")

    doc_id = str(uuid.uuid4())
    object_key = document_object_key(doc_id, ext)

    # 按正文内容哈希去重：同一链接内容未变时命中既有文档。
    import hashlib
    file_hash = hashlib.sha256(content).hexdigest()
    existing = await db.execute(
        select(Document).where(
            Document.kb_id == kb_id,
            Document.file_hash == file_hash,
        )
    )
    existing_doc = existing.scalar_one_or_none()
    if existing_doc is not None:
        location = await _describe_doc_location(db, existing_doc.folder_id)
        return DocumentResponse(
            id=existing_doc.id,
            kb_id=existing_doc.kb_id,
            filename=existing_doc.filename,
            file_type=existing_doc.file_type,
            file_size=existing_doc.file_size,
            status="duplicate",
            error_message=f"该内容已存在于{location}（与 {existing_doc.filename} 相同）",
            chunk_count=existing_doc.chunk_count,
            created_at=existing_doc.created_at.isoformat() if existing_doc.created_at else "",
        )

    # 写入 MinIO（权威存储）。
    await store.put_bytes(object_key, content, content_type="text/markdown; charset=utf-8")

    # 创建文档记录；DB 写失败时补偿删除已上传对象（与上传路径对称）。
    try:
        doc = Document(
            id=doc_id,
            kb_id=kb_id,
            folder_id=body.folder_id,
            filename=filename,
            file_type=ext,
            file_size=file_size,
            file_hash=file_hash,
            status="pending",
            source_url=article.url,
            tenant_id=kb.tenant_id,
        )
        db.add(doc)
        kb.doc_count = (kb.doc_count or 0) + 1
        await db.flush()
        await db.refresh(doc)
        await db.commit()
    except Exception:
        await _safe_remove_objects([object_key])
        raise

    # 生成缩略图：方案 A 优先用文章封面图（og:image，带 referer 绕过防盗链），
    # 下载失败则由 _generate_and_store_thumbnail 内部回退方案 B（fitz 文字卡片）。
    cover_bytes: bytes | None = None
    if article.cover_image_url:
        cover_bytes = await download_image(article.cover_image_url, referer=article.url)
    await _generate_and_store_thumbnail(doc_id, ext, content, cover_image=cover_bytes)

    # 后台触发管道处理（与上传同一入队/回退逻辑）。
    await _enqueue_or_fallback(
        request, object_key, doc_id, kb_id,
        file_size=file_size, tenant_id=kb.tenant_id, object_key=object_key,
    )

    return DocumentResponse(
        id=doc.id,
        kb_id=doc.kb_id,
        filename=doc.filename,
        file_type=doc.file_type,
        file_size=doc.file_size,
        status=doc.status,
        error_message=doc.error_message,
        chunk_count=doc.chunk_count,
        source_url=doc.source_url,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
    )


@router.get("/api/documents/{doc_id}", response_model=DocumentResponse)
async def get_document(
    doc_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """获取文档详情（元数据；contextvar 兜底确保仅本租户可见 -> 跨租户 404）"""
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise CrossTenantError()
    return DocumentResponse(
        id=doc.id,
        kb_id=doc.kb_id,
        filename=doc.filename,
        file_type=doc.file_type,
        file_size=doc.file_size,
        status=doc.status,
        error_message=doc.error_message,
        chunk_count=doc.chunk_count,
        progress=doc.progress or 0,
        progress_message=doc.progress_message,
        source_url=doc.source_url,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
    )


@router.post("/api/documents/{doc_id}/retry", response_model=DocumentResponse)
async def retry_document(
    doc_id: str,
    request: Request,
    identity: IdentityContext = Depends(require_member()),
    db: AsyncSession = Depends(get_db_session),
):
    """重新识别文档（清除旧数据后重新处理）"""
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise CrossTenantError()
    # 校验对所属 KB 的写权限
    await _authorize_kb_access(db, identity, doc.kb_id, KbAccessEnum.WRITE)
    if doc.status == "processing":
        raise HTTPException(status_code=400, detail="文档正在处理中")

    # 清除旧的 chunk 数据
    chunk_result = await db.execute(select(Chunk.id).where(Chunk.doc_id == doc_id))
    chunk_ids = [row[0] for row in chunk_result.all()]
    if chunk_ids:
        try:
            milvus = _get_milvus()
            await milvus.delete(doc.kb_id, chunk_ids)
        except Exception as e:
            logger.warning("清除旧向量失败（可忽略）: %s", e)
        # 删除 SQLite 中的 chunks
        from sqlalchemy import delete as sql_delete
        await db.execute(sql_delete(Chunk).where(Chunk.doc_id == doc_id))

    # 重置状态
    doc.status = "pending"
    doc.error_message = None
    doc.chunk_count = 0
    doc.progress = 0
    doc.progress_message = None
    # 重解析图谱一致性（design.md 4.6 / Req 5.2）：先自增 graph_attempt 使在途旧抽取任务
    # 失效（worker 陈旧守卫据此跳过），再异步清理该文档旧图；新内容入库完成后由
    # maybe_trigger_graph_extract 再次自增 attempt 并按新内容 seed。
    doc.graph_attempt = (doc.graph_attempt or 0) + 1
    doc.graph_status = "none"
    await db.flush()

    # 重新触发管道（优先入队 Redis Stream）。源文件权威存储在 MinIO。
    object_key = document_object_key(doc_id, doc.file_type)
    store = get_object_store()
    if store is None or not await store.exists(object_key):
        doc.status = "failed"
        doc.error_message = "原始文件已丢失，无法重新识别"
        await db.flush()
        raise HTTPException(status_code=400, detail="原始文件已丢失")

    # 尝试入队 Redis Stream（按大小选择快/慢道），降级为进程内回退（限流）
    queue = _select_queue(request, doc.file_size)
    if queue is not None:
        try:
            msg = TaskMessage(
                doc_id=doc_id, kb_id=doc.kb_id, file_path=object_key,
                tenant_id=doc.tenant_id, object_key=object_key,
            )
            await queue.enqueue(msg)
        except Exception as e:
            logger.warning("Redis 入队失败，降级为 create_task: %s", e)
            asyncio.create_task(_run_pipeline_fallback(object_key, doc_id, doc.kb_id, object_key))
    else:
        asyncio.create_task(_run_pipeline_fallback(object_key, doc_id, doc.kb_id, object_key))

    await db.commit()
    # 重解析：异步清理该文档旧图（在 graph_attempt 已自增、在途旧任务已失效之后）。
    # fire-and-forget + 优雅降级，绝不阻塞 / 影响重解析主流程。Req 5.2。
    from app.pipeline.graph.cleanup import cleanup_graph_for_doc
    asyncio.create_task(cleanup_graph_for_doc(doc.kb_id, doc_id))
    return DocumentResponse(
        id=doc.id,
        kb_id=doc.kb_id,
        filename=doc.filename,
        file_type=doc.file_type,
        file_size=doc.file_size,
        status="pending",
        error_message=None,
        chunk_count=0,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
    )


@router.delete("/api/documents/{doc_id}", status_code=204)
async def delete_document(
    doc_id: str,
    identity: IdentityContext = Depends(require_member()),
    db: AsyncSession = Depends(get_db_session),
):
    """删除文档（快速响应版：立即删除 DB 记录并返回，后台异步清理 Milvus 和文件）"""
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise CrossTenantError()
    # 校验对所属 KB 的写权限
    await _authorize_kb_access(db, identity, doc.kb_id, KbAccessEnum.WRITE)

    # 收集清理所需信息（在删除 DB 记录前）
    kb_id = doc.kb_id
    file_type = doc.file_type

    # 如果文档正在处理中，先标记为 cancelled（Pipeline 各阶段会检查此状态并终止）
    if doc.status in ("pending", "processing"):
        doc.status = "cancelled"
        await db.flush()

    # 更新知识库文档计数
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = kb_result.scalar_one_or_none()
    if kb and kb.doc_count > 0:
        kb.doc_count -= 1

    # 删除文档（ORM cascade 会自动删除关联 chunks）
    from sqlalchemy import delete as sql_delete

    await db.execute(
        sql_delete(DocumentSummary).where(
            DocumentSummary.source == "document",
            DocumentSummary.doc_id == doc_id,
        )
    )
    await db.delete(doc)
    await db.commit()

    # 后台异步清理 Milvus 向量 + 本地文件 + 缓存（不阻塞 API 响应）
    asyncio.create_task(_doc_cleanup_background(doc_id, kb_id, file_type))


async def _doc_cleanup_background(doc_id: str, kb_id: str, file_type: str) -> None:
    """单文档删除后台清理：Milvus 向量、MinIO 源文件与缩略图、缓存"""
    # 删除 Milvus 中的向量
    try:
        milvus = _get_milvus()
        if await milvus.has_collection(kb_id):
            await milvus.delete_by_doc_id(kb_id, doc_id)
    except Exception as e:
        logger.warning("删除 Milvus 向量失败（可忽略）: %s", e)

    # 删除 MinIO 中的源文件 + 缩略图
    store = get_object_store()
    if store is not None:
        await store.remove_many([
            document_object_key(doc_id, file_type),
            thumbnail_object_key(doc_id),
        ])

    # 删除知识图谱中该文档贡献的实体/关系（优雅降级：图谱未启用/不可用时静默跳过，
    # 任何 Neo4j 故障不影响主删除链路）。design.md 4.6 / Req 5.1。
    from app.pipeline.graph.cleanup import cleanup_graph_for_doc
    await cleanup_graph_for_doc(kb_id, doc_id)

    # 清除该知识库的检索缓存
    from app.retrieval.cache import get_retrieval_cache
    cache = await get_retrieval_cache()
    if cache:
        await cache.invalidate_kb(kb_id)


# ============================================================
# 批量删除文档
# ============================================================


class BatchRetryRequest(BaseModel):
    """批量重试请求"""
    doc_ids: list[str]


@router.post("/api/documents/batch-retry", status_code=200)
async def batch_retry_documents(
    body: BatchRetryRequest,
    request: Request,
    identity: IdentityContext = Depends(require_member()),
    db: AsyncSession = Depends(get_db_session),
):
    """批量重试失败的文档（contextvar 兜底确保仅命中本租户文档）"""
    if not body.doc_ids:
        return {"retried_count": 0, "total_requested": 0}

    # 查询所有指定文档
    result = await db.execute(
        select(Document).where(Document.id.in_(body.doc_ids))
    )
    docs = result.scalars().all()

    retried = []
    skipped = []
    for doc in docs:
        if doc.status == "processing":
            skipped.append(doc.id)
            continue

        # 清除旧 chunks
        from sqlalchemy import delete as sql_delete
        await db.execute(sql_delete(Chunk).where(Chunk.doc_id == doc.id))

        # 重置状态
        doc.status = "pending"
        doc.error_message = None
        doc.chunk_count = 0
        doc.progress = 0
        doc.progress_message = None
        # 重解析图谱一致性（Req 5.2）：自增 graph_attempt 使在途旧任务失效。
        doc.graph_attempt = (doc.graph_attempt or 0) + 1
        doc.graph_status = "none"
        retried.append(doc)

    await db.flush()

    # 批量清理 Milvus 旧向量
    kb_doc_map: dict[str, list[str]] = {}
    for doc in retried:
        kb_doc_map.setdefault(doc.kb_id, []).append(doc.id)

    for kb_id, doc_ids in kb_doc_map.items():
        try:
            milvus = _get_milvus()
            if await milvus.has_collection(kb_id):
                await milvus.delete_by_doc_ids(kb_id, doc_ids)
        except Exception as e:
            logger.warning("批量重试 - 清除旧向量失败: %s", e)

    # 批量入队（按文件大小选择快/慢道）。源文件权威存储在 MinIO。
    store = get_object_store()
    for doc in retried:
        object_key = document_object_key(doc.id, doc.file_type)
        if store is None or not await store.exists(object_key):
            doc.status = "failed"
            doc.error_message = "原始文件已丢失"
            continue

        queue = _select_queue(request, doc.file_size)
        if queue is not None:
            try:
                msg = TaskMessage(
                    doc_id=doc.id, kb_id=doc.kb_id, file_path=object_key,
                    tenant_id=doc.tenant_id, object_key=object_key,
                )
                await queue.enqueue(msg)
            except Exception:
                asyncio.create_task(_run_pipeline_fallback(object_key, doc.id, doc.kb_id, object_key))
        else:
            asyncio.create_task(_run_pipeline_fallback(object_key, doc.id, doc.kb_id, object_key))

    await db.commit()
    # 重解析：异步清理这些文档的旧图（graph_attempt 已自增，在途旧任务失效后）。Req 5.2。
    from app.pipeline.graph.cleanup import cleanup_graph_for_docs
    retry_kb_doc_map: dict[str, list[str]] = {}
    for doc in retried:
        retry_kb_doc_map.setdefault(doc.kb_id, []).append(doc.id)
    for kb_id, doc_ids in retry_kb_doc_map.items():
        asyncio.create_task(cleanup_graph_for_docs(kb_id, doc_ids))
    return {"retried_count": len(retried), "skipped_count": len(skipped), "total_requested": len(body.doc_ids)}


class BatchDeleteRequest(BaseModel):
    """批量删除请求"""
    doc_ids: list[str]


@router.post("/api/documents/batch-delete", status_code=200)
async def batch_delete_documents(
    body: BatchDeleteRequest,
    identity: IdentityContext = Depends(require_member()),
    db: AsyncSession = Depends(get_db_session),
):
    """批量删除文档（快速响应版：立即删除 DB 记录并返回，后台异步清理 Milvus 和文件）"""
    if not body.doc_ids:
        raise HTTPException(status_code=400, detail="doc_ids 不能为空")

    from sqlalchemy import update as sql_update, delete as sql_delete

    # ─── 先用受兜底过滤的 SELECT 收敛到"本租户内确实存在"的 doc_ids ───
    # bulk update/delete 不被方案B兜底覆盖，故必须先据此把范围限定在本租户，
    # 杜绝跨租户 doc_id 混入批量操作。
    scoped = await db.execute(
        select(Document).where(Document.id.in_(body.doc_ids))
    )
    docs = scoped.scalars().all()  # 已被 contextvar 兜底限定为本租户
    if not docs:
        return {"deleted_count": 0, "total_requested": len(body.doc_ids)}
    allowed_ids = [d.id for d in docs]

    # ─── 标记 cancelled（仅限已确认本租户的文档） ───
    await db.execute(
        sql_update(Document)
        .where(Document.id.in_(allowed_ids))
        .where(Document.status.in_(("pending", "processing")))
        .values(status="cancelled")
    )
    await db.flush()

    # 收集清理信息
    kb_ids_affected: set[str] = set()
    doc_ids_found: list[str] = []
    cleanup_info: list[dict] = []  # [{id, kb_id, file_type}]

    for doc in docs:
        kb_ids_affected.add(doc.kb_id)
        doc_ids_found.append(doc.id)
        cleanup_info.append({"id": doc.id, "kb_id": doc.kb_id, "file_type": doc.file_type})

    # ─── 批量查询所有 chunk_ids（在删除前收集） ───
    kb_chunk_map: dict[str, list[str]] = {}
    if doc_ids_found:
        chunk_result = await db.execute(
            select(Chunk.id, Chunk.doc_id).where(Chunk.doc_id.in_(doc_ids_found))
        )
        for chunk_id, doc_id in chunk_result.all():
            doc_obj = next((d for d in docs if d.id == doc_id), None)
            if doc_obj:
                kb_chunk_map.setdefault(doc_obj.kb_id, []).append(chunk_id)

    # ─── 批量更新知识库文档计数 ───
    for kb_id in kb_ids_affected:
        doc_count_in_batch = sum(1 for d in docs if d.kb_id == kb_id)
        kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        kb = kb_result.scalar_one_or_none()
        if kb:
            kb.doc_count = max(0, (kb.doc_count or 0) - doc_count_in_batch)

    # ─── 批量删除 DB 记录（仅本租户已确认的文档） ───
    await db.execute(sql_delete(Chunk).where(Chunk.doc_id.in_(doc_ids_found)))
    await db.execute(
        sql_delete(DocumentSummary).where(
            DocumentSummary.source == "document",
            DocumentSummary.doc_id.in_(doc_ids_found),
        )
    )
    await db.execute(sql_delete(Document).where(Document.id.in_(doc_ids_found)))
    await db.commit()

    # ─── 后台异步清理 Milvus 向量 + 本地文件 + 缓存 ───
    asyncio.create_task(_batch_cleanup_background(kb_chunk_map, cleanup_info, kb_ids_affected))

    return {"deleted_count": len(doc_ids_found), "total_requested": len(body.doc_ids)}


async def _batch_cleanup_background(
    kb_chunk_map: dict[str, list[str]],
    cleanup_info: list[dict],
    kb_ids_affected: set[str],
) -> None:
    """后台清理 Milvus 向量、本地文件和缓存（不阻塞 API 响应）"""
    # 删除 Milvus 向量（使用 doc_id 表达式删除，覆盖孤儿向量）
    # 按 kb_id 分组，对每个文档用 delete_by_doc_id 确保清理干净
    kb_doc_map: dict[str, list[str]] = {}
    for info in cleanup_info:
        kb_doc_map.setdefault(info["kb_id"], []).append(info["id"])

    for kb_id, doc_ids in kb_doc_map.items():
        try:
            milvus = _get_milvus()
            if await milvus.has_collection(kb_id):
                await milvus.delete_by_doc_ids(kb_id, doc_ids)
        except Exception as e:
            logger.warning("批量删除后台清理 - 删除 Milvus 向量失败: %s", e)

    # 删除 MinIO 中的源文件 + 缩略图
    store = get_object_store()
    if store is not None:
        keys: list[str] = []
        for info in cleanup_info:
            keys.append(document_object_key(info["id"], info["file_type"]))
            keys.append(thumbnail_object_key(info["id"]))
        await store.remove_many(keys)

    # 删除知识图谱中这些文档贡献的实体/关系（按 kb 分组逐 doc 清理，优雅降级）。Req 5.1。
    from app.pipeline.graph.cleanup import cleanup_graph_for_docs
    for kb_id, doc_ids in kb_doc_map.items():
        await cleanup_graph_for_docs(kb_id, doc_ids)

    # 清除检索缓存
    from app.retrieval.cache import get_retrieval_cache
    cache = await get_retrieval_cache()
    if cache:
        for kb_id in kb_ids_affected:
            await cache.invalidate_kb(kb_id)


# ============================================================
# 文件夹批量上传
# ============================================================


class FolderUploadFileInfo(BaseModel):
    """文件夹上传中单个文件的信息"""
    relative_path: str
    filename: str
    file_type: str
    supported: bool
    reason: str | None = None


class FolderUploadValidateRequest(BaseModel):
    """文件夹上传校验请求"""
    paths: list[str]  # 文件相对路径列表（含目录结构）


class FolderUploadValidateResponse(BaseModel):
    """文件夹上传校验响应"""
    supported_files: list[FolderUploadFileInfo]
    unsupported_files: list[FolderUploadFileInfo]
    folder_structure: list[str]  # 需要创建的文件夹路径列表


class FolderUploadResultItem(BaseModel):
    """文件夹上传结果中的单个文件"""
    relative_path: str
    filename: str
    doc_id: str | None = None
    folder_id: str | None = None
    status: str  # uploaded | skipped | error
    message: str | None = None


class FolderUploadResponse(BaseModel):
    """文件夹批量上传响应"""
    total_files: int
    uploaded_count: int
    skipped_count: int
    created_folders: list[str]
    results: list[FolderUploadResultItem]


@router.post("/api/knowledge-bases/{kb_id}/documents/validate-folder", response_model=FolderUploadValidateResponse)
async def validate_folder_upload(
    kb_id: str,
    body: FolderUploadValidateRequest,
    identity: IdentityContext = Depends(require_member()),
    db: AsyncSession = Depends(get_db_session),
):
    """校验文件夹上传：解析目录结构，区分支持和不支持的文件"""
    # 校验对该 KB 的写权限（跨租户404 / 无写权403）
    await _authorize_kb_access(db, identity, kb_id, KbAccessEnum.WRITE)

    supported_files: list[FolderUploadFileInfo] = []
    unsupported_files: list[FolderUploadFileInfo] = []
    folder_paths: set[str] = set()

    for rel_path in body.paths:
        # 提取目录部分
        parts = rel_path.replace("\\", "/").split("/")
        filename = parts[-1]

        # 收集所有中间目录
        for i in range(1, len(parts)):
            folder_path = "/".join(parts[:i])
            folder_paths.add(folder_path)

        # 校验文件名
        try:
            validate_filename(filename)
        except NameValidationError as e:
            unsupported_files.append(FolderUploadFileInfo(
                relative_path=rel_path,
                filename=filename,
                file_type="",
                supported=False,
                reason=f"文件名校验失败: {e.message}",
            ))
            continue

        # 检查文件扩展名
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in _ALLOWED_EXTENSIONS:
            supported_files.append(FolderUploadFileInfo(
                relative_path=rel_path,
                filename=filename,
                file_type=ext,
                supported=True,
            ))
        else:
            unsupported_files.append(FolderUploadFileInfo(
                relative_path=rel_path,
                filename=filename,
                file_type=ext or "(无扩展名)",
                supported=False,
                reason=f"不支持的文件类型: {ext or '无扩展名'}，支持: {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
            ))

    # 排序文件夹路径（确保父目录在前）
    sorted_folders = sorted(folder_paths)

    return FolderUploadValidateResponse(
        supported_files=supported_files,
        unsupported_files=unsupported_files,
        folder_structure=sorted_folders,
    )


@router.post("/api/knowledge-bases/{kb_id}/documents/upload-folder", response_model=FolderUploadResponse, status_code=201)
async def upload_folder(
    kb_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    paths: Annotated[str, Form(...)] = "",
    parent_folder_id: Annotated[str | None, Form()] = None,
    identity: IdentityContext = Depends(require_member()),
    db: AsyncSession = Depends(get_db_session),
):
    """批量上传文件夹

    - files: 多个文件（multipart）
    - paths: JSON 字符串，文件相对路径列表，与 files 一一对应
    - parent_folder_id: 上传到的父文件夹 ID（可选，为空表示当前目录）
    """
    import json

    # 校验对该 KB 的写权限
    kb = await _authorize_kb_access(db, identity, kb_id, KbAccessEnum.WRITE)

    # 解析路径列表
    try:
        path_list: list[str] = json.loads(paths)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="paths 参数格式错误，需要 JSON 数组字符串")

    if len(path_list) != len(files):
        raise HTTPException(status_code=400, detail="文件数量与路径数量不匹配")

    # 1. 收集需要创建的文件夹结构
    folder_paths: set[str] = set()
    for rel_path in path_list:
        parts = rel_path.replace("\\", "/").split("/")
        for i in range(1, len(parts)):
            folder_path = "/".join(parts[:i])
            folder_paths.add(folder_path)

    # 2. 按层级排序并创建文件夹
    sorted_folders = sorted(folder_paths)
    folder_id_map: dict[str, str] = {}  # path -> folder_id
    created_folders: list[str] = []

    for folder_path in sorted_folders:
        parts = folder_path.split("/")
        folder_name = parts[-1]

        # 校验文件夹名称
        try:
            folder_name = validate_folder_name(folder_name)
        except NameValidationError as e:
            raise HTTPException(
                status_code=422,
                detail=f"文件夹 '{folder_path}' 名称校验失败: {e.message}",
            )

        # 确定父文件夹 ID
        if len(parts) == 1:
            parent_id = parent_folder_id
        else:
            parent_path = "/".join(parts[:-1])
            parent_id = folder_id_map.get(parent_path, parent_folder_id)

        # 检查同名文件夹是否已存在
        existing_query = select(Folder).where(
            Folder.kb_id == kb_id,
            Folder.name == folder_name,
            Folder.parent_id == parent_id if parent_id else Folder.parent_id.is_(None),
        )
        existing_result = await db.execute(existing_query)
        existing_folder = existing_result.scalar_one_or_none()

        if existing_folder:
            folder_id_map[folder_path] = existing_folder.id
        else:
            folder_id = str(uuid.uuid4())
            new_folder = Folder(
                id=folder_id,
                kb_id=kb_id,
                parent_id=parent_id,
                name=folder_name,
                tenant_id=kb.tenant_id,
            )
            db.add(new_folder)
            folder_id_map[folder_path] = folder_id
            created_folders.append(folder_path)

    await db.flush()

    # 3. 逐个处理文件
    import hashlib

    results: list[FolderUploadResultItem] = []
    uploaded_count = 0
    skipped_count = 0
    # 批次内已入库的内容哈希，避免同一批里多份相同内容重复落库（DB 查重只覆盖已提交记录）。
    seen_hashes: set[str] = set()

    for file, rel_path in zip(files, path_list):
        parts = rel_path.replace("\\", "/").split("/")
        filename = parts[-1]
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        # 跳过不支持的文件
        if ext not in _ALLOWED_EXTENSIONS:
            skipped_count += 1
            results.append(FolderUploadResultItem(
                relative_path=rel_path,
                filename=filename,
                status="skipped",
                message=f"不支持的文件类型: {ext}",
            ))
            continue

        # 校验文件名
        try:
            filename = validate_filename(filename)
        except NameValidationError as e:
            skipped_count += 1
            results.append(FolderUploadResultItem(
                relative_path=rel_path,
                filename=filename,
                status="skipped",
                message=f"文件名校验失败: {e.message}",
            ))
            continue

        # 确定文件所属文件夹
        if len(parts) > 1:
            folder_path = "/".join(parts[:-1])
            target_folder_id = folder_id_map.get(folder_path, parent_folder_id)
        else:
            target_folder_id = parent_folder_id

        object_key: str | None = None
        try:
            content = await file.read()
            file_size = len(content)

            # KB 级内容去重（与单文件上传一致）：命中既有文档或同批已入库内容则跳过，
            # 不重复落 MinIO / 建记录 / 切片向量化。提示带既有文件的实际位置。
            file_hash = hashlib.sha256(content).hexdigest()
            existing = await db.execute(
                select(Document).where(
                    Document.kb_id == kb_id,
                    Document.file_hash == file_hash,
                )
            )
            existing_doc = existing.scalar_one_or_none()
            if existing_doc is not None or file_hash in seen_hashes:
                skipped_count += 1
                if existing_doc is not None:
                    location = await _describe_doc_location(db, existing_doc.folder_id)
                    msg = f"该文件已存在于{location}（与 {existing_doc.filename} 内容相同）"
                else:
                    msg = "该文件与本次上传的另一文件内容相同，已跳过"
                results.append(FolderUploadResultItem(
                    relative_path=rel_path,
                    filename=filename,
                    status="skipped",
                    message=msg,
                ))
                continue

            # 保存文件到 MinIO（权威存储）
            doc_id = str(uuid.uuid4())
            object_key = document_object_key(doc_id, ext)

            store = get_object_store()
            if store is None:
                raise RuntimeError("对象存储不可用")
            await store.put_bytes(
                object_key, content,
                content_type=file.content_type or "application/octet-stream",
            )

            # 创建文档记录（盖章 tenant_id = 所属 KB 的 tenant_id）
            doc = Document(
                id=doc_id,
                kb_id=kb_id,
                folder_id=target_folder_id,
                filename=filename,
                file_type=ext,
                file_size=file_size,
                file_hash=file_hash,
                status="pending",
                tenant_id=kb.tenant_id,
            )
            db.add(doc)
            seen_hashes.add(file_hash)

            # 更新知识库文档计数
            kb.doc_count = (kb.doc_count or 0) + 1
            await db.flush()
            await db.commit()

            # 生成缩略图（PDF 首页渲染）并存入 MinIO
            await _generate_and_store_thumbnail(doc_id, ext, content)

            # 后台触发管道处理（按文件大小路由快/慢道，优先入队 Redis Stream，降级为 asyncio.create_task）
            await _enqueue_or_fallback(
                request, object_key, doc_id, kb_id,
                file_size=file_size, tenant_id=kb.tenant_id, object_key=object_key,
            )

            uploaded_count += 1
            results.append(FolderUploadResultItem(
                relative_path=rel_path,
                filename=filename,
                doc_id=doc_id,
                folder_id=target_folder_id,
                status="uploaded",
            ))
        except Exception as e:
            logger.error("文件 %s 上传失败: %s", rel_path, e)
            # 一致性补偿：MinIO 已写入但后续失败时删除孤儿对象（put 之后才失败的情况）
            if object_key is not None:
                await _safe_remove_objects([object_key])
            results.append(FolderUploadResultItem(
                relative_path=rel_path,
                filename=filename,
                status="error",
                message=str(e),
            ))

    # 显式提交（get_db_session 不自动提交）：确保即使无可上传文件，已创建的文件夹也落库。
    await db.commit()

    return FolderUploadResponse(
        total_files=len(files),
        uploaded_count=uploaded_count,
        skipped_count=skipped_count,
        created_folders=created_folders,
        results=results,
    )


@router.get("/api/documents/{doc_id}/preview")
async def preview_document_file(
    doc_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """预览文档缩略图（图片返回原件，PDF 返回首页缩略图）。源文件存于 MinIO。"""
    _ensure_not_super_admin_content(identity)  # 内容边界：超管默认不可查看正文/预览
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise CrossTenantError()

    store = get_object_store()
    if store is None:
        raise HTTPException(status_code=503, detail="对象存储不可用")

    # 图片类型：直接返回原文件
    if doc.file_type in ("jpg", "jpeg", "png"):
        key = document_object_key(doc_id, doc.file_type)
        if not await store.exists(key):
            raise HTTPException(status_code=404, detail="文件不存在")
        media_type_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}
        data = await store.get_bytes(key)
        return Response(
            content=data,
            media_type=media_type_map.get(doc.file_type, "application/octet-stream"),
        )

    # PDF 类型：返回缩略图（上传时已预生成）
    # PDF / Markdown / TXT 类型：返回缩略图（上传/转存时已预生成）。
    # md/txt 的缩略图为「封面图（链接转存）或文字卡片（fitz 渲染）」，与 PDF 走同一存取链路。
    if doc.file_type in ("pdf", "md", "txt"):
        thumb_key = thumbnail_object_key(doc_id)
        if not await store.exists(thumb_key):
            raise HTTPException(status_code=404, detail="缩略图不可用")
        data = await store.get_bytes(thumb_key)
        return Response(content=data, media_type="image/png")

    raise HTTPException(status_code=400, detail="该文件类型不支持预览")


@router.get("/api/documents/{doc_id}/raw")
async def get_document_raw_file(
    doc_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """返回文档原始文件（用于原件在线预览/下载）。源文件存于 MinIO，流式透传。"""
    _ensure_not_super_admin_content(identity)  # 内容边界：超管默认不可查看正文
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise CrossTenantError()

    store = get_object_store()
    if store is None:
        raise HTTPException(status_code=503, detail="对象存储不可用")

    key = document_object_key(doc_id, doc.file_type)
    if not await store.exists(key):
        raise HTTPException(status_code=404, detail="原始文件不存在")

    media_type_map = {
        "pdf": "application/pdf",
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "txt": "text/plain; charset=utf-8", "md": "text/markdown; charset=utf-8",
        "csv": "text/csv; charset=utf-8",
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
    media_type = media_type_map.get(doc.file_type, "application/octet-stream")
    data = await store.get_bytes(key)

    from urllib.parse import quote

    # filename* 用 RFC 5987 编码，兼容中文文件名；inline 让浏览器尽量内嵌预览。
    disposition = f"inline; filename*=UTF-8''{quote(doc.filename)}"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )


@router.get("/api/documents/{doc_id}/chunks", response_model=PageResult[ChunkResponse])
async def list_document_chunks(
    doc_id: str,
    page: int = 1,
    page_size: int = 20,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """查看文档的切片列表（父块分页 + 当前页父块对应的子块内容用于高亮）"""
    _ensure_not_super_admin_content(identity)  # 内容边界：超管默认不可查看 Chunk 正文
    # 验证文档存在（contextvar 兜底确保仅本租户文档可见 -> 跨租户 404）
    doc_result = await db.execute(select(Document).where(Document.id == doc_id))
    if doc_result.scalar_one_or_none() is None:
        raise CrossTenantError()

    # 参数兜底
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size

    # 父块总数
    total = await db.scalar(
        select(func.count(Chunk.id)).where(Chunk.doc_id == doc_id, Chunk.parent_id.is_(None))
    ) or 0

    # 当前页父块
    result = await db.execute(
        select(Chunk)
        .where(Chunk.doc_id == doc_id, Chunk.parent_id.is_(None))
        .order_by(Chunk.chunk_index)
        .offset(offset)
        .limit(page_size)
    )
    parent_chunks = result.scalars().all()

    # 仅查询当前页父块对应的子块（一次 in_ 查询，避免 N+1 与全量加载）
    parent_ids = [c.id for c in parent_chunks]
    children_by_parent: dict[str, list[str]] = {}
    if parent_ids:
        child_result = await db.execute(
            select(Chunk)
            .where(Chunk.doc_id == doc_id, Chunk.parent_id.in_(parent_ids))
            .order_by(Chunk.chunk_index)
        )
        for child in child_result.scalars().all():
            children_by_parent.setdefault(child.parent_id, []).append(child.content)

    items = [
        ChunkResponse(
            id=c.id,
            doc_id=c.doc_id,
            kb_id=c.kb_id,
            parent_id=c.parent_id,
            content=c.content,
            chunk_index=c.chunk_index,
            created_at=c.created_at.isoformat() if c.created_at else "",
            children=children_by_parent.get(c.id, []),
        )
        for c in parent_chunks
    ]

    return PageResult[ChunkResponse](
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=offset + len(items) < total,
    )


class FileContentResponse(BaseModel):
    """按 fileId 统一返回解析后原文文本（跨 KB 文档 / 会话临时文件两类来源）。

    ``source`` 标明该 id 命中的来源：``document``（KB 文档）或 ``session_file``（会话临时
    文件）。``content`` 为父块按 ``chunk_index`` 有序拼接的完整解析文本；文件未建索引完成
    （``status != completed``）时可能为空串。此处返回解析后可读文本，与原件字节流
    （``/raw``）区分。
    """

    file_id: str = Field(..., description="文件 ID（入参回显）")
    source: str = Field(..., description="来源：document（KB 文档）| session_file（会话临时文件）")
    filename: str = Field(..., description="原始文件名")
    file_type: str | None = Field(None, description="文件类型扩展名（小写，无点）")
    status: str = Field(..., description="文件状态")
    content: str = Field(..., description="解析后的完整原文文本（父块按 chunk_index 有序拼接）")


@router.get("/api/files/{file_id}/content", response_model=FileContentResponse)
async def get_file_content_by_id(
    file_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """按 fileId 返回解析后的原文文本，自动识别「KB 文档」与「会话临时文件」两类来源。

    统一入口：第三方从 references / 上传回执拿到的 id 可能是 KB 文档（``Document.id``）
    或会话临时文件（``SessionFile.id``），二者均为全局唯一 UUID，不撞号，故按「先文档、
    后会话文件」顺序解析：

    1. KB 文档：``select(Document)`` 命中即从 ``Chunk`` 父块（``parent_id is None``）按
       ``chunk_index`` 拼接。跨租户由仓储层方案 B 兜底过滤自动不可见（→ 落到 404）。
    2. 会话临时文件：``select(SessionFile)`` 命中后再校验归属
       （``owner_user_id == acting_subject_id``）。外部用户共享同一租户，租户过滤不足以
       区分不同外部用户，必须叠加归属校验；从 ``SessionChunk`` 父块拼接。
    3. 两类均未命中 / 归属不符 → ``404``（存在性非泄露，与既有内容端点一致）。

    内容边界：``_ensure_not_super_admin_content`` 禁止超管默认查看正文（与 ``/chunks`` 一致）。
    """
    _ensure_not_super_admin_content(identity)

    # 1) 先按 KB 文档解析（方案 B 租户兜底过滤保证跨租户不可见 → 404）
    doc = (
        await db.execute(select(Document).where(Document.id == file_id))
    ).scalar_one_or_none()
    if doc is not None:
        rows = await db.execute(
            select(Chunk.content)
            .where(Chunk.doc_id == file_id, Chunk.parent_id.is_(None))
            .order_by(Chunk.chunk_index)
        )
        return FileContentResponse(
            file_id=doc.id,
            source="document",
            filename=doc.filename,
            file_type=doc.file_type,
            status=doc.status,
            content="\n\n".join(rows.scalars().all()),
        )

    # 2) 再按会话临时文件解析（租户兜底过滤 + 归属校验，缺一不可）
    sf = (
        await db.execute(select(SessionFile).where(SessionFile.id == file_id))
    ).scalar_one_or_none()
    if sf is not None and sf.owner_user_id == identity.acting_subject_id:
        rows = await db.execute(
            select(SessionChunk.content)
            .where(SessionChunk.file_id == file_id, SessionChunk.parent_id.is_(None))
            .order_by(SessionChunk.chunk_index)
        )
        return FileContentResponse(
            file_id=sf.id,
            source="session_file",
            filename=sf.filename,
            file_type=sf.file_type,
            status=sf.status,
            content="\n\n".join(rows.scalars().all()),
        )

    # 3) 都未命中 / 归属不符 → 404（存在性非泄露）
    raise CrossTenantError()


# 文档详情事件列表单次返回上限（避免大文档一次拉爆，前端展示足够）。
_DOC_EVENTS_MAX_LIMIT = 200


@router.get("/api/documents/{doc_id}/events", response_model=list[DocumentEventResponse])
async def list_document_events(
    doc_id: str,
    limit: int = _DOC_EVENTS_MAX_LIMIT,
    identity: IdentityContext = Depends(require_authenticated()),
    db: AsyncSession = Depends(get_db_session),
):
    """查看文档抽取出的事件列表（title/summary + 关联实体，Requirements 4.2）。

    事件中心图谱从文档各 chunk 抽取的完整语义单元，供文档处理结果页展示。强制带
    ``kb_id`` + ``doc_id`` 双重隔离（事件读取经 GraphStore）。

    降级：图存储未启用 / Neo4j 不可用 / 该文档无事件时返回空列表 ``[]``（不报错，
    与图谱整体「可选展示」语义一致，不影响文档详情主链路）。
    """
    _ensure_not_super_admin_content(identity)  # 内容边界：超管默认不可查看事件正文
    # 验证文档存在（contextvar 兜底确保仅本租户文档可见 -> 跨租户 404）。
    doc_result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = doc_result.scalar_one_or_none()
    if doc is None:
        raise CrossTenantError()

    safe_limit = max(1, min(limit, _DOC_EVENTS_MAX_LIMIT))

    # 图存储不可用（未启用 / Neo4j 故障）时干净降级为空列表。
    from app.storage.graph_store import get_graph_store

    store = await get_graph_store()
    if store is None:
        return []
    try:
        events = await store.events_by_doc(kb_id=doc.kb_id, doc_id=doc_id, limit=safe_limit)
    except Exception as e:  # noqa: BLE001 — 图查询故障不影响文档详情主链路，降级为空
        logger.warning("[doc-events] doc_id=%s 事件查询失败，降级为空: %s", doc_id, e)
        return []

    return [
        DocumentEventResponse(
            id=ev.id,
            title=ev.title,
            summary=ev.summary,
            content=ev.content,
            chunk_id=ev.chunk_id,
            entity_names=ev.entity_names,
        )
        for ev in events
    ]
