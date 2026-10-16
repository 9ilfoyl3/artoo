"""Chat Session API - 会话管理接口（tenant-auth：Guard + 盖章 + 租户隔离 + 内容边界）。

会话/消息为受隔离资源（TenantScopedMixin）：Guard 设置的 contextvar 三态会让本模块
自开 async_session 的 SELECT 自动按租户过滤；创建时盖章 tenant_id；消息正文受
Content_View_Boundary 约束（Super_Admin 默认不可读）。

此外，会话/消息是**个人对话历史**，仅创建者本人可见可管理：在租户隔离之上再按
``owner_user_id == identity.acting_subject_id`` 收敛。仅靠 tenant_id 隔离会让同租户
的用户互相看到、打开、改名、删除对方的对话历史（本次修复的核心权限缺陷）。
"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select, func, delete

from app.api.deps import require_authenticated
from app.api.errors import CrossTenantError, NotFoundError, PermissionDeniedError, ValidationInputError
from app.auth.identity import IdentityContext
from app.config import get_settings
from app.schema.db import AgentCompactionCheckpoint, ChatSession, ChatMessageRecord
from app.session_upload.service import get_session_upload_service
from app.storage.database import async_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["Sessions"])


# ============================================================
# 请求/响应模型
# ============================================================


class SessionCreate(BaseModel):
    """创建会话请求"""
    title: str = Field(default="新对话", max_length=200)
    kb_id: str | None = None
    model_config_id: str | None = None


class SessionUpdate(BaseModel):
    """更新会话请求"""
    title: str | None = Field(default=None, max_length=200)


class SessionItem(BaseModel):
    """会话列表项"""
    id: str
    title: str
    kb_id: str | None
    model_config_id: str | None
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class MessageItem(BaseModel):
    """消息列表项"""
    id: str
    role: str
    content: str
    references: dict | list | None = None
    agent_steps: list | None = None
    attachments: list | None = None
    kb_id: str | None = None
    kb_ids: list | None = None
    feedback: str | None = None
    created_at: datetime


class FeedbackUpdate(BaseModel):
    """消息反馈请求：点赞/踩/取消（取消传 null）。"""
    feedback: str | None = Field(default=None, description="like | dislike | null（取消）")


class RetryResult(BaseModel):
    """重试结果：返回被移除的那一轮用户消息，供前端原样重发。"""
    content: str
    attachments: list | None = None
    kb_id: str | None = None
    kb_ids: list | None = None


def _ensure_not_super_admin_content(identity: IdentityContext) -> None:
    """Content_View_Boundary：Super_Admin 默认不可读 ChatMessage 正文（R34）。"""
    if identity.is_super_admin and not get_settings().content_view_boundary_open:
        raise PermissionDeniedError("超级管理员默认不可查看业务内容正文")


async def _get_owned_session(
    session, session_id: str, identity: IdentityContext
) -> ChatSession:
    """取会话并校验本人归属。

    - contextvar 兜底先保证仅本租户可见（跨租户/不存在 -> 404）。
    - 再校验 owner_user_id == 当前行事主体（acting_subject_id）：会话是个人对话历史，
      非本人（含同租户他人、无主历史会话）一律视为不存在，返回相同 404（不泄露存在性）。
    - tenant_level 机器身份（acting_subject_id 为 None）不绑定自然人，不可见任何会话。
    """
    result = await session.execute(
        select(ChatSession).where(ChatSession.id == session_id)
    )
    cs = result.scalar_one_or_none()
    if cs is None:
        raise CrossTenantError()
    subject = identity.acting_subject_id
    if subject is None or cs.owner_user_id != subject:
        raise CrossTenantError()
    return cs


# ============================================================
# 接口实现
# ============================================================


@router.get("")
async def list_sessions(
    identity: IdentityContext = Depends(require_authenticated()),
) -> list[SessionItem]:
    """获取会话列表（按更新时间倒序，仅本租户内本人会话；空会话过滤）"""
    subject = identity.acting_subject_id
    if subject is None:
        # 机器级身份（tenant_level Key）不绑定自然人，没有个人对话历史
        return []
    async with async_session() as session:
        msg_count_subq = (
            select(
                ChatMessageRecord.session_id,
                func.count(ChatMessageRecord.id).label("msg_count"),
            )
            .group_by(ChatMessageRecord.session_id)
            .subquery()
        )
        # inner join 仅返回至少有一条消息的会话（过滤空会话）。
        # ChatSession 经 contextvar 兜底已限定本租户；再按 owner 收敛为仅本人会话。
        result = await session.execute(
            select(ChatSession, msg_count_subq.c.msg_count)
            .join(msg_count_subq, ChatSession.id == msg_count_subq.c.session_id)
            .where(ChatSession.owner_user_id == subject)
            .order_by(ChatSession.updated_at.desc())
        )
        items = []
        for row in result.all():
            s = row[0]
            count = row[1] or 0
            items.append(SessionItem(
                id=s.id, title=s.title, kb_id=s.kb_id,
                model_config_id=s.model_config_id, message_count=count,
                created_at=s.created_at, updated_at=s.updated_at,
            ))
        return items


@router.post("")
async def create_session(
    req: SessionCreate,
    identity: IdentityContext = Depends(require_authenticated()),
) -> SessionItem:
    """创建新会话（盖章 tenant_id 与 owner_user_id）"""
    new_session = ChatSession(
        id=str(uuid.uuid4()),
        title=req.title,
        kb_id=req.kb_id,
        model_config_id=req.model_config_id,
        tenant_id=identity.tenant_id,
        owner_user_id=identity.acting_subject_id,
    )
    async with async_session() as session:
        session.add(new_session)
        await session.commit()
        await session.refresh(new_session)
        return SessionItem(
            id=new_session.id, title=new_session.title, kb_id=new_session.kb_id,
            model_config_id=new_session.model_config_id, message_count=0,
            created_at=new_session.created_at, updated_at=new_session.updated_at,
        )


@router.put("/{session_id}")
async def update_session(
    session_id: str,
    req: SessionUpdate,
    identity: IdentityContext = Depends(require_authenticated()),
) -> SessionItem:
    """更新会话（重命名）"""
    async with async_session() as session:
        chat_session = await _get_owned_session(session, session_id, identity)
        if req.title is not None:
            chat_session.title = req.title
        await session.commit()
        await session.refresh(chat_session)

        count_result = await session.execute(
            select(func.count(ChatMessageRecord.id)).where(
                ChatMessageRecord.session_id == session_id
            )
        )
        msg_count = count_result.scalar() or 0
        return SessionItem(
            id=chat_session.id, title=chat_session.title, kb_id=chat_session.kb_id,
            model_config_id=chat_session.model_config_id, message_count=msg_count,
            created_at=chat_session.created_at, updated_at=chat_session.updated_at,
        )


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
):
    """删除会话及其所有消息（仅本人）。

    DB 删除（``ChatSession`` → CASCADE 删 ``messages`` / ``session_files`` /
    ``session_chunks``）后，显式调用 ``SessionUploadService.cleanup_session_files``
    按 ``session_id`` 清理共享 ``kb_session_files`` 中的会话文件向量（DB FK 管不到
    Milvus，必须显式删，Req 1.6）。

    清理失败仅记 WARNING、不阻塞会话删除主流程：会话 DB 行已删除是用户主诉求；
    残留向量在隔离前提下用户已不可访问，可由后台对账兜底（无对应 ChatSession 的
    孤儿 session_id 向量）。``cleanup_session_files`` 内部已捕获并记 WARNING，此处
    再包一层 try/except 是对实现协议的防御性兜底。
    """
    async with async_session() as session:
        chat_session = await _get_owned_session(session, session_id, identity)
        await session.execute(
            delete(AgentCompactionCheckpoint).where(
                AgentCompactionCheckpoint.session_id == session_id
            )
        )
        await session.delete(chat_session)
        await session.commit()

    # DB 删除已成功提交（用户主诉求已满足），后续清理失败不应回滚 / 报错。
    try:
        await get_session_upload_service().cleanup_session_files(session_id)
    except Exception as e:
        logger.warning(
            "会话 %s 删除后向量级联清理异常（非致命，DB 已删）: %s", session_id, e
        )

    return {"detail": "已删除"}


@router.get("/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
) -> list[MessageItem]:
    """获取会话的所有消息（消息正文受内容边界约束）"""
    _ensure_not_super_admin_content(identity)
    async with async_session() as session:
        # 验证会话存在且为本人所有
        await _get_owned_session(session, session_id, identity)
        result = await session.execute(
            select(ChatMessageRecord)
            .where(ChatMessageRecord.session_id == session_id)
            .order_by(ChatMessageRecord.created_at)
        )
        messages = result.scalars().all()
        return [
            MessageItem(
                id=m.id, role=m.role, content=m.content,
                references=m.references, agent_steps=m.agent_steps,
                attachments=m.attachments,
                kb_id=m.kb_id, kb_ids=m.kb_ids, feedback=m.feedback,
                created_at=m.created_at,
            )
            for m in messages
        ]


@router.delete("/{session_id}/messages")
async def clear_session_messages(
    session_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
):
    """清空会话消息（保留会话本身，仅本人）"""
    async with async_session() as session:
        # 先确认会话为本人所有，再按 session_id 删除其消息
        await _get_owned_session(session, session_id, identity)
        await session.execute(
            delete(ChatMessageRecord).where(ChatMessageRecord.session_id == session_id)
        )
        await session.commit()
    return {"detail": "已清空"}


@router.put("/{session_id}/messages/{message_id}/feedback")
async def set_message_feedback(
    session_id: str,
    message_id: str,
    req: FeedbackUpdate,
    identity: IdentityContext = Depends(require_authenticated()),
):
    """设置/取消某条 AI 回答的反馈（点赞 like / 踩 dislike / null 取消）。

    反馈仅先落库保留，供后续 agent 优化与质量评估，不参与当前对话逻辑。
    """
    if req.feedback not in (None, "like", "dislike"):
        raise ValidationInputError("反馈取值非法（仅支持 like / dislike / null）")
    async with async_session() as session:
        await _get_owned_session(session, session_id, identity)
        msg = await session.get(ChatMessageRecord, message_id)
        if msg is None or msg.session_id != session_id:
            raise NotFoundError("消息不存在")
        if msg.role != "assistant":
            raise ValidationInputError("仅能对 AI 回答进行反馈")
        msg.feedback = req.feedback
        await session.commit()
    return {"detail": "已记录", "feedback": req.feedback}


@router.post("/{session_id}/messages/retry", response_model=RetryResult)
async def retry_last_round(
    session_id: str,
    identity: IdentityContext = Depends(require_authenticated()),
) -> RetryResult:
    """重试最新一轮对话：删除最后一条 user 消息及其之后的所有消息（即最新一轮的
    user + assistant），返回该 user 消息内容供前端原样重新发起。

    仅允许重试**最新一轮**：按 created_at 找到最后一条 user 消息，删除它及之后的消息。
    这样无论上一轮 AI 是否回答成功（报错轮可能没有 assistant 消息），都能正确回退，
    且不会破坏更早的历史消息结构。
    """
    async with async_session() as session:
        await _get_owned_session(session, session_id, identity)
        result = await session.execute(
            select(ChatMessageRecord)
            .where(ChatMessageRecord.session_id == session_id)
            .order_by(ChatMessageRecord.created_at)
        )
        messages = list(result.scalars().all())
        # 找到最后一条 user 消息的下标
        last_user_idx = next(
            (i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "user"),
            None,
        )
        if last_user_idx is None:
            raise NotFoundError("没有可重试的对话")

        last_user = messages[last_user_idx]
        retry = RetryResult(
            content=last_user.content,
            attachments=last_user.attachments,
            kb_id=last_user.kb_id,
            kb_ids=last_user.kb_ids,
        )
        # 删除该 user 消息及其之后的所有消息（最新一轮）
        to_delete = [m.id for m in messages[last_user_idx:]]
        await session.execute(
            delete(ChatMessageRecord).where(ChatMessageRecord.id.in_(to_delete))
        )
        await session.commit()
    return retry
