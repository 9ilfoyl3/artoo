"""Chat API - OpenAI 兼容接口

实现 POST /v1/chat/completions 端点，支持流式和非流式响应，
集成三档检索模式（direct / hybrid / agent）。
"""

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncGenerator
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.agent.config import AgentConfig
from app.api.agent_config import get_effective_preset_config
from app.api.deps import require_authenticated
from app.api.query_understanding import understand_query
from app.auth.identity import IdentityContext
from app.auth.kb_authz import KbAccessEnum
from app.auth.kb_scope import authorize_requested_kbs
from app.auth.session_ownership import verify_session_owner
from app.agent.engine import AgentEngine
from app.agent.events import AgentEvent, EventBus, EventType
from app.agent.state import AgentState
from app.agent.tools.base import ToolContext
from app.agent.tools.grep_chunks import GrepChunksTool
from app.agent.tools.knowledge_search import KnowledgeSearchTool, SearchTarget
from app.agent.tools.list_chunks import ListKnowledgeChunksTool
from app.agent.tools.read_attachment import ReadAttachmentTool
from app.agent.tools.read_skill import ReadSkillTool
from app.agent.tools.web_search import WebSearchTool
from app.agent.tools.registry import ToolRegistry
from app.agent.tools.mcp_client import get_mcp_tools
from app.agent.skills import SkillManager, default_skill_dirs
from app.api.skills import load_user_custom_skills
from app.agent.prompts.progressive_rag import render_system_prompt
from app.agent.prompts.runtime_context import render_runtime_context
from app.config import get_settings
from app.models.manager import get_model_manager
from app.models.provider import LLMProvider
from app.models.llm.ollama import OllamaLLM
from app.models.llm.vllm import VllmLLM
from app.retrieval.base import RetrievalResult
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.filter import RetrievalFilter
from app.retrieval.factory import build_hybrid_retriever
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.log_safety import sanitize_for_log
from app.retrieval.multi_kb import KBRetrievalConfig, MultiKBRetriever, MultiKBSearchResult
from app.retrieval.sparse import SparseRetriever
from app.retrieval.vector import VectorRetriever
from app.schema.api import (
    AgentRetrievalRequest,
    AgentRetrievalResponse,
    ChatChoice,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    DeltaContent,
    ReferenceItem,
    ResponseMessage,
    StreamChoice,
    UsageInfo,
)
from app.schema.db import LLMConfig, ChatSession, ChatMessageRecord
from app.session_upload.service import get_session_upload_service
from app.storage.database import async_session
from app.storage.milvus import MilvusClient, SESSION_FILES_KB_ID, build_session_id_expr, get_milvus_client

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

router = APIRouter()

# 历史上下文最大轮数（每轮 = 1 user + 1 assistant）
MAX_HISTORY_ROUNDS = 10


class Route(str, Enum):
    """问答检索路由决策（进程内枚举，不持久化）。

    - ``AGENT``：Agent ReAct 多轮检索（含/不含会话源由 session_has_files 决定）。
    - ``MULTI_KB``：非 agent 的单轮多源检索（含会话源经 MultiKB 接入，现状不变）。
    - ``SINGLE_KB``：非 agent 的单轮单库检索（现状不变）。
    - ``CHITCHAT``：skip_retrieval（闲聊/纯历史追问），跳过检索直接作答。
    - ``NONE``：无任何检索源（agent 无 kb 无会话文件），纯 LLM 作答兜底。
    """

    AGENT = "agent"
    MULTI_KB = "multi_kb"
    SINGLE_KB = "single_kb"
    CHITCHAT = "chitchat"
    NONE = "none"


def _resolve_retrieval_route(
    mode: str,
    requested_kb_ids: list[str],
    session_has_files: bool,
    skip_retrieval: bool,
    multi_kb_requested: bool = False,
    external_tools_available: bool = False,
) -> Route:
    """统一路由决策纯函数（design「Route Resolution 真值表」），流式/非流式两入口共用。

    关键不变式（Property 2 - Agent 不降级）：``mode==agent`` 且非 skip 且（有 KB 或有会话
    文件）→ 恒为 ``AGENT``，不再因 ``session_has_files`` 切到 MULTI_KB。非 agent 模式
    （hybrid/direct）路由与改造前逐条一致（Property 4 - 向后兼容）。

    非 agent 的 MULTI_KB vs SINGLE_KB 判定精确复刻改造前 ``use_multi_kb =
    bool(request.kb_ids) or session_has_files``：用了多选字段 ``kb_ids``（哪怕仅 1 个）、
    或有会话文件 → MULTI_KB；仅单选 ``knowledge_base_id`` 且无会话文件 → SINGLE_KB。
    （``len>1`` 作防御性兜底：即便调用方漏传 multi_kb_requested，多库也走 MULTI_KB。）

    Args:
        mode: 检索模式 direct / hybrid / agent。
        requested_kb_ids: 已统一的所选知识库 ID（单选 knowledge_base_id 与多选 kb_ids 合并后）。
        session_has_files: 当前会话是否有上传文件（探测失败按 False 传入）。
        skip_retrieval: 查询理解判定为闲聊/纯历史追问。
        multi_kb_requested: 请求是否使用了多选字段 ``request.kb_ids``（非空）。仅影响非 agent
            模式 MULTI_KB vs SINGLE_KB 的区分，agent 模式不受影响（统一进 _build_agent_runtime）。
        external_tools_available: 本次生效预设的 allowed_tools 白名单里是否存在**已启用的
            外部 MCP 工具**。用于修正"agent 无检索源必降级"的旧假设：MCP 工具（如文书精修
            的 read_document / propose_doc_edit）本身不依赖知识库，调用方常常不带任何 kb，
            此时若降级为纯 LLM 生成，工具定义根本不会下发给模型（payload 无 tools），模型
            只能把工具名当普通文本吐出来（外部租户"没按工具方法调用"的直接原因）。

    Returns:
        Route 枚举。
    """
    has_kb = bool(requested_kb_ids)
    has_any_source = has_kb or session_has_files

    if mode == "agent":
        if skip_retrieval:
            return Route.CHITCHAT
        if has_any_source:
            return Route.AGENT
        # agent 无检索源但有可用的外部 MCP 工具 → 仍走 ReAct：Agent 的价值不止检索，
        # 工具调用本身就是目的。检索类工具因 search_targets 为空自然不注册，
        # _build_agent_runtime 对 kb_ids=[] 已是安全路径（primary_kb_id=None）。
        if external_tools_available:
            return Route.AGENT
        # agent 既无检索源也无外部工具 → 纯 LLM 作答兜底（既有行为，避免为闲聊
        # 多付一轮 ReAct 开销）。
        return Route.NONE

    # 非 agent（hybrid / direct）：保持改造前路由。
    if skip_retrieval:
        return Route.CHITCHAT
    # 多选字段（含单元素）、或有会话文件、或库数 >1（防御）→ 多源单轮检索。
    if multi_kb_requested or session_has_files or len(requested_kb_ids) > 1:
        return Route.MULTI_KB
    if has_kb:
        return Route.SINGLE_KB
    return Route.NONE


async def _verify_session_owner(session_id: str, identity: IdentityContext) -> None:
    """校验 session_id 归属当前行事主体本人，否则 404（存在性非泄露）。

    会话/消息是个人对话历史。问答端点接收前端传入的 session_id 用于加载历史上下文
    与续写消息；若不校验归属，任何同租户用户都能凭他人 session_id 读取其历史（泄露进
    本次回答上下文）并向其会话注入消息。此处与 session API 的 owner 收敛保持一致。

    - 跨租户由 contextvar 兜底过滤为不可见 -> get 返回 None -> 404。
    - owner 不匹配（含同租户他人、无主历史会话）-> 404。
    - tenant_level 机器身份（acting_subject_id 为 None）不绑定自然人 -> 一律 404。

    安全逻辑已收敛到 ``app.auth.session_ownership.verify_session_owner``（单一权威实现），
    此处委托复用，与开放检索的会话附件源校验保持一致。
    """
    await verify_session_owner(session_id, identity)


# 历史检索结果占位符：历史轮次的工具输出不落库（避免膨胀，也避免模型复用过期检索
# 结果），重放时统一以此占位，配合系统提示词强制每个新事实问题重新检索。
_HISTORY_TOOL_OUTPUT_PLACEHOLDER = (
    "[Previous retrieval omitted — please perform a fresh search.]"
)

# 历史 assistant 正文中可能残留的旧版工具注解（如 "[Agent used: grep_chunks(27ms)]"），
# 重放前剥除，避免内部工具名泄露进模型上下文乃至被模型抄进答案。
_LEGACY_TOOL_ANNOTATION_RE = re.compile(r"\n*\[Agent used:[^\]]*\]\s*$")
_LEGACY_INFRA_TOOLS = {"final_answer", "thinking"}


async def _load_session_history(session_id: str) -> list[dict]:
    """从数据库加载会话历史消息，返回最近 N 轮对话（OpenAI 消息格式）。

    只保留最近 MAX_HISTORY_ROUNDS 轮（user+assistant 各一条算一轮），避免上下文
    过长超出 LLM token 限制。

    对于带 agent_steps 的 assistant 消息，将其事件流还原为结构化的
    ``assistant(tool_calls)`` + ``tool`` 消息对，而非把工具名拼进正文文本：
    工具名与调用参数仅进入 LLM 协议字段，不会出现在任何用户可见内容里。历史工具
    输出一律以占位符替代，强制后续轮次重新检索。
    """
    async with async_session() as session:
        try:
            result = await session.execute(
                select(ChatMessageRecord)
                .where(ChatMessageRecord.session_id == session_id)
                .order_by(ChatMessageRecord.created_at)
            )
            messages = result.scalars().all()
        except Exception as e:
            # agent_steps 列可能不存在（需要数据库迁移），降级为仅查询基础字段
            logger.warning("加载会话历史异常（可能缺少 agent_steps 列），降级查询: %s", e)
            await session.rollback()
            from sqlalchemy import text
            raw_result = await session.execute(
                text("SELECT id, session_id, role, content, created_at FROM chat_messages WHERE session_id = :sid ORDER BY created_at"),
                {"sid": session_id},
            )
            rows = raw_result.fetchall()
            history = [{"role": row.role, "content": row.content} for row in rows]
            max_messages = MAX_HISTORY_ROUNDS * 2
            if len(history) > max_messages:
                history = history[-max_messages:]
            return history

    if not messages:
        return []

    # 先按源消息（user/assistant 各一条）截断到最近 2N 条，再逐条还原。
    # 在源消息边界截断是安全的：每条 assistant 还原出的消息块自成完整结构
    # （以 assistant(tool_calls) 开头、tool 紧随其后），不会产生孤立的 tool 消息。
    max_messages = MAX_HISTORY_ROUNDS * 2
    if len(messages) > max_messages:
        messages = messages[-max_messages:]

    history: list[dict] = []
    for m in messages:
        if m.role == "assistant":
            agent_steps = getattr(m, "agent_steps", None)
            history.extend(_reconstruct_assistant_turn(m.content or "", agent_steps))
        else:
            history.append({"role": m.role, "content": m.content})

    return history


def _reconstruct_assistant_turn(content: str, agent_steps: list | None) -> list[dict]:
    """将一条历史 assistant 消息还原为结构化的 OpenAI 消息序列。

    输出形如::

        assistant(content=思考, tool_calls=[…])   # 每个 LLM 决策轮一条
        tool(tool_call_id=…, content=占位符)        # 紧随其后，逐个工具结果
        …
        assistant(content=最终答案)                  # 末尾承载用户可见答案

    要点：
    - 工具名、调用参数只出现在 ``tool_calls`` 协议字段中，绝不进入可见正文。
    - 自然停止的最终正文由末尾 assistant 消息承载，避免上一轮看起来仍在进行中。
    - 历史工具输出未落库，统一以占位符替代，强制后续轮次重新检索。
    - 无 agent_steps（普通 RAG / 旧数据）时退化为单条 assistant 最终答案消息。
    """
    final_answer = _LEGACY_TOOL_ANNOTATION_RE.sub("", content or "").strip()

    if not agent_steps or not isinstance(agent_steps, list):
        return [{"role": "assistant", "content": final_answer}] if final_answer else []

    # 按 iteration 聚合：还原"一次 LLM 决策可发起多个并行工具调用"的结构。
    # agent_steps 按时间顺序存储：reasoning_delta → tool_call(s) → tool_result(s) → … → text_delta。
    iter_reasoning: dict[int, str] = {}
    iter_calls: dict[int, list[dict]] = {}
    iter_order: list[int] = []
    streamed_answer_parts: list[str] = []

    for step in agent_steps:
        if not isinstance(step, dict):
            continue
        stype = step.get("type")
        iteration = step.get("iteration", 0)
        if stype in {"reasoning_delta", "thought"}:
            text = step.get("content", "")
            if text:
                iter_reasoning[iteration] = iter_reasoning.get(iteration, "") + text
        elif stype == "text_delta":
            text = step.get("content", "")
            if text:
                streamed_answer_parts.append(text)
        elif stype == "tool_call":
            name = step.get("tool_name", "")
            if not name or name in _LEGACY_INFRA_TOOLS:
                continue
            if iteration not in iter_calls:
                iter_calls[iteration] = []
                iter_order.append(iteration)
            iter_calls[iteration].append({
                "id": step.get("tool_call_id", ""),
                "name": name,
                "arguments": step.get("arguments", {}),
            })

    msgs: list[dict] = []
    for iteration in iter_order:
        calls = iter_calls[iteration]
        if not calls:
            continue
        assistant_msg: dict = {
            "role": "assistant",
            "content": iter_reasoning.get(iteration, "") or None,
            "tool_calls": [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {
                        "name": c["name"],
                        "arguments": json.dumps(c["arguments"], ensure_ascii=False)
                        if not isinstance(c["arguments"], str)
                        else c["arguments"],
                    },
                }
                for c in calls
            ],
        }
        msgs.append(assistant_msg)
        for c in calls:
            msgs.append({
                "role": "tool",
                "tool_call_id": c["id"],
                "content": _HISTORY_TOOL_OUTPUT_PLACEHOLDER,
            })

    if not final_answer and streamed_answer_parts:
        final_answer = "".join(streamed_answer_parts).strip()
    if final_answer:
        msgs.append({"role": "assistant", "content": final_answer})

    return msgs


async def _save_message(session_id: str, role: str, content: str, references: list | None = None, agent_steps: list | None = None, kb_id: str | None = None, kb_ids: list | None = None, tenant_id: str | None = None, attachments: list | None = None) -> str:
    """保存一条消息到会话，返回新消息的 ID。

    tenant_id 由调用方在请求处理期间从 IdentityContext 取好后传入（后台任务在响应返回后
    执行，届时请求级 contextvar 已失效，故必须显式透传，不在此重新解析身份）。

    attachments 仅 user 消息可能非空：发送时绑定的会话文件快照（file_id/filename/...），
    用于历史回放在对应用户气泡上方渲染附件 chip。
    """
    msg_id = str(uuid.uuid4())
    msg = ChatMessageRecord(
        id=msg_id,
        session_id=session_id,
        role=role,
        content=content,
        references=references,
        agent_steps=agent_steps,
        attachments=attachments,
        kb_id=kb_id,
        kb_ids=kb_ids,
        tenant_id=tenant_id,
    )
    async with async_session() as session:
        session.add(msg)
        await session.commit()
    return msg_id


def _truncate_title(user_query: str) -> str:
    """用问题文本截断生成占位标题（≤30 字符）。"""
    return user_query[:30] + ("..." if len(user_query) > 30 else "")


async def _persist_user_message_and_seed_title(
    session_id: str,
    user_query: str,
    *,
    kb_id: str | None = None,
    kb_ids: list | None = None,
    tenant_id: str | None = None,
    attachments: list | None = None,
) -> bool:
    """发起对话即入库：保存用户消息，并在首轮用问题文本播种会话标题。

    在生成回答之前调用，使新会话立即拥有消息记录（侧栏过滤空会话，落库后即可见）
    与一个可读标题（问题截断）。AI 答完后再异步精炼标题。

    Returns:
        本会话是否为首轮对话（无任何历史消息）。供调用方决定 AI 答完后是否精炼标题。
    """
    async with async_session() as session:
        count = await session.execute(
            select(func.count(ChatMessageRecord.id)).where(
                ChatMessageRecord.session_id == session_id
            )
        )
        is_first_round = (count.scalar() or 0) == 0

        session.add(
            ChatMessageRecord(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="user",
                content=user_query,
                attachments=attachments,
                kb_id=kb_id,
                kb_ids=kb_ids,
                tenant_id=tenant_id,
            )
        )

        if is_first_round:
            chat_session = await session.get(ChatSession, session_id)
            if chat_session is not None and chat_session.title == "新对话":
                chat_session.title = _truncate_title(user_query)

        await session.commit()
        return is_first_round


async def _refine_session_title(
    session_id: str, user_query: str, assistant_answer: str = ""
) -> None:
    """AI 答完后异步精炼会话标题（仅首轮调用）。

    标题在发起对话时已用问题文本播种，此处用 LLM 生成更贴切的 ≤15 字标题覆盖。
    LLM 失败则保留已播种的问题标题，不影响主流程。
    """
    title = await _generate_title_with_llm(user_query, assistant_answer)
    if not title:
        return
    async with async_session() as session:
        chat_session = await session.get(ChatSession, session_id)
        if chat_session is not None:
            chat_session.title = title
            await session.commit()


async def _generate_title_with_llm(user_query: str, assistant_answer: str) -> str | None:
    """调用 LLM 生成会话标题

    Returns:
        生成的标题字符串，失败时返回 None
    """
    try:
        llm, _, _ = await _get_llm_for_request(None)
        prompt = (
            f"根据以下对话生成一个≤15字的简短标题：\n"
            f"用户：{user_query[:200]}\n"
            f"助手：{assistant_answer[:200]}\n"
            f"标题："
        )
        title = await llm.generate([{"role": "user", "content": prompt}])
        if title and title.strip():
            # 清理引号和多余空白，确保不超过 30 字符
            title = title.strip().strip('"').strip("'").strip("《》")
            return title[:30]
    except Exception as e:
        logger.warning("LLM 生成标题失败: %s", e)

    return None


async def _get_llm_for_request(model_config_id: str | None) -> tuple[LLMProvider, bool, int | None]:
    """根据 model_config_id 获取 LLM 实例和配置

    优先级：指定 ID > 数据库中的默认配置 > 系统全局配置

    Returns:
        (LLM 实例, 是否启用流式, 最大上下文 token 数)

    注：思考开关（深度思考）由智能体预设独占控制，不再来自模型配置。
    thinking 开关写入请求体的字段格式由 LLMConfig.thinking_control 决定（已在
    _create_llm_from_config 注入 VllmLLM）。
    """
    if model_config_id:
        async with async_session() as session:
            result = await session.execute(
                select(LLMConfig).where(LLMConfig.id == model_config_id)
            )
            config = result.scalar_one_or_none()
            if config:
                return _create_llm_from_config(config), config.stream_enabled, config.max_context_tokens

    # 尝试使用数据库中标记为默认的配置
    async with async_session() as session:
        result = await session.execute(
            select(LLMConfig).where(LLMConfig.is_default == True)  # noqa: E712
        )
        config = result.scalar_one_or_none()
        if config:
            return _create_llm_from_config(config), config.stream_enabled, config.max_context_tokens

    # 回退到系统全局配置
    settings = get_settings()
    if settings.llm_provider == "vllm":
        return _get_cached_llm("vllm", settings.llm_base_url, settings.llm_model, settings.llm_api_key), True, None
    return _get_cached_llm("ollama", settings.llm_base_url, settings.llm_model, ""), True, None


# 进程内 LLM 实例缓存：复用底层 httpx.AsyncClient 连接池，避免每个请求新建客户端
# 却从不关闭导致的连接/文件描述符泄漏（高并发下会耗尽 FD）。
# httpx.AsyncClient 绑定创建它的事件循环；生产是单循环长驻进程，缓存长期复用即可。
# 测试常为每个用例新建事件循环，故按「当前运行循环」缓存，循环切换时整体重建，
# 避免「Future attached to a different loop」错误。
_llm_cache: dict[tuple, LLMProvider] = {}
_llm_cache_loop: "asyncio.AbstractEventLoop | None" = None


def _get_cached_llm(
    provider: str,
    base_url: str,
    model: str,
    api_key: str,
    vendor: str | None = None,
    thinking_control: str | None = None,
    max_output_tokens: int | None = None,
) -> LLMProvider:
    """按 (provider, base_url, model, api_key, vendor, thinking_control) 复用 LLM 实例。

    同一配置返回同一实例（复用 httpx 连接池）；配置变更（如改地址/密钥/厂商/思考格式）
    自然命中新 key 生成新实例。注意：测试连通性端点（llm_config.py）仍各自新建并
    close()，不走此缓存。
    """
    global _llm_cache, _llm_cache_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not _llm_cache_loop:
        # 事件循环切换（主要发生在测试）：丢弃旧缓存，旧实例随旧循环一并回收。
        _llm_cache = {}
        _llm_cache_loop = loop

    key = (provider, base_url, model, api_key, vendor, thinking_control, max_output_tokens)
    inst = _llm_cache.get(key)
    if inst is None:
        if provider == "ollama":
            inst = OllamaLLM(base_url=base_url, model=model)
        else:
            inst = VllmLLM(
                base_url=base_url,
                model=model,
                api_key=api_key,
                provider=vendor,
                thinking_control=thinking_control,
                max_output_tokens=max_output_tokens,
            )
        _llm_cache[key] = inst
    return inst


def _create_llm_from_config(config: LLMConfig) -> LLMProvider:
    """根据数据库配置创建（或复用）LLM 实例"""
    # 模型配置优先；未配置时回落到全局 LLM_MAX_OUTPUT_TOKENS。
    max_output_tokens = config.max_output_tokens or get_settings().llm_max_output_tokens
    if config.provider == "ollama":
        return _get_cached_llm(
            "ollama", config.base_url, config.model, "",
            max_output_tokens=max_output_tokens,
        )
    else:
        # provider 字段承载基础设施类型（ollama/vllm）；vendor 承载实际模型厂商，
        # thinking_control 承载思考开关的写入格式。两者显式传入 VllmLLM，使 thinking
        # 方言分派不再依赖 base_url/模型名的脆弱自动猜测（为空时 VllmLLM 内部仍自动检测）。
        return _get_cached_llm(
            "vllm",
            config.base_url,
            config.model,
            config.api_key or "",
            vendor=config.vendor,
            thinking_control=config.thinking_control,
            max_output_tokens=max_output_tokens,
        )



# RAG 系统提示词模板
_SYSTEM_PROMPT = """你是一个知识库问答助手。请根据以下检索到的参考内容回答用户问题。

规则：
1. 基于参考内容回答，不要编造信息
2. 如果参考内容不足以回答问题，如实告知用户
3. 回答应结构清晰，必要时使用编号或分点
4. 引用具体内容时，标注来源编号（如 [1]、[2]）
5. 根据用户意图选择回答方式：
   - 如果用户在寻找/筛选/列举内容（如"有哪些..."、"帮我找..."、"哪些是..."），
     从参考内容中筛选出符合条件的结果并逐一列举，说明每条为什么符合条件
   - 如果用户在问一个具体问题，直接基于参考内容回答该问题
6. 当参考内容包含多个文档/条目时，注意区分不同来源，不要混淆

参考内容：
{context}"""


def _estimate_tokens(text: str) -> int:
    """简单的 token 数量估算（基于字符数）

    中文约 1.5 字符/token，英文约 4 字符/token，取平均约 2 字符/token
    """
    return max(1, len(text) // 2)


def _build_context(chunks: list[RetrievalResult], max_tokens: int | None = None) -> str:
    """将检索结果拼接为上下文文本，按 chunk 粒度控制总长度

    Args:
        chunks: 检索结果列表（已按相关性排序）
        max_tokens: 上下文最大 token 数，None 表示使用默认 200K（与 Agent 模式一致）
    """
    import re

    if not chunks:
        return "（未找到相关内容）"
    # 与 Agent 模式保持一致：模型未配置 max_context_tokens 时回退默认 200K
    if not max_tokens:
        max_tokens = AgentConfig.max_context_tokens
    parts = []
    total_chars = 0
    # 按 2 字符/token 估算
    max_chars = max_tokens * 2 if max_tokens else None
    for i, chunk in enumerate(chunks, 1):
        # 去掉 BM25 content 前缀 [filename]
        content = chunk.content
        if content and content.startswith("["):
            content = re.sub(r'^\[[^\]]*\]\s*', '', content)
        entry = f"[{i}] {content}"
        if max_chars and total_chars + len(entry) > max_chars:
            break
        parts.append(entry)
        total_chars += len(entry)
    return "\n\n".join(parts)


async def _build_references(chunks: list[RetrievalResult]) -> list[ReferenceItem]:
    """将检索结果转换为引用来源列表，包含文件名"""
    if not chunks:
        return []

    # 批量查询所有涉及的文档文件名
    doc_ids = list(set(chunk.doc_id for chunk in chunks))
    doc_filenames: dict[str, str] = {}
    async with async_session() as session:
        from app.schema.db import Document
        result = await session.execute(
            select(Document.id, Document.filename).where(Document.id.in_(doc_ids))
        )
        for row in result:
            doc_filenames[row.id] = row.filename

        # 会话临时文件的 doc_id 是 SessionFile.id（在 session_files 表，不在 documents 表），
        # 上面查不到。对未匹配到的 doc_id 再查 SessionFile 补齐文件名，否则前端会回退显示
        # doc_id 前 8 位（如 "101bed7c"）而非真实文件名。
        missing_ids = [d for d in doc_ids if d not in doc_filenames]
        if missing_ids:
            from app.schema.db import SessionFile
            sf_result = await session.execute(
                select(SessionFile.id, SessionFile.filename).where(SessionFile.id.in_(missing_ids))
            )
            for row in sf_result:
                doc_filenames[row.id] = row.filename

    refs = []
    for chunk in chunks:
        child = chunk.child_content[:500] if chunk.child_content else ""
        parent = chunk.content[:1500] if chunk.content else ""

        # 去掉 BM25 content 前缀 [filename] （入库时为 BM25 检索加的文件名前缀）
        import re
        if child and child.startswith("["):
            child = re.sub(r'^\[[^\]]*\]\s*', '', child)
        if parent and parent.startswith("["):
            parent = re.sub(r'^\[[^\]]*\]\s*', '', parent)

        # 如果截断后 child 和 parent 相同（子块就是父块本身），清空 child 避免前端误判
        if child and parent and child == parent:
            child = ""

        refs.append(
            ReferenceItem(
                doc_id=chunk.doc_id,
                chunk_id=chunk.chunk_id,
                filename=doc_filenames.get(chunk.doc_id, ""),
                content=parent,
                child_content=child,
                score=round(chunk.score, 4),
            )
        )
    return refs


def _get_milvus_client() -> MilvusClient:
    """获取 Milvus 客户端实例"""
    return get_milvus_client()


# 图谱第四路 / 混合检索器的装配已下沉到 ``app.retrieval.factory``，供问答链路（本模块）与
# 检索召回接口（``app.api.retrieval``）共享同一召回口径。此处保留同名薄包装，不改动本文件内
# 既有调用点（_build_hybrid_retriever），也让两入口零漂移。
_build_hybrid_retriever = build_hybrid_retriever


def _build_degraded_metadata(failed_kb_ids: list[str]) -> dict:
    """从失败源列表派生前端可消费的降级元数据（Req 2.x：区分会话源 vs 知识库源）。

    - ``failed_source_count``：失败源总数（向后兼容既有字段）。
    - ``failed_kb_ids``：失败源 ID 列表（含 ``SESSION_FILES_KB_ID`` 时表示会话文件源失败）。
    - ``session_source_failed``：会话文件源是否失败（前端据此提示"会话文件检索失败"）。
    - ``kb_source_failed``：是否有正式知识库源失败（前端据此提示"知识库检索失败"）。

    前端拿到后即可分别渲染"会话文件检索失败"与"知识库检索失败"两类提示（design C7.1）。
    """
    session_failed = SESSION_FILES_KB_ID in failed_kb_ids
    kb_failed = any(kid != SESSION_FILES_KB_ID for kid in failed_kb_ids)
    return {
        "failed_source_count": len(failed_kb_ids),
        "failed_kb_ids": list(failed_kb_ids),
        "session_source_failed": session_failed,
        "kb_source_failed": kb_failed,
    }


async def _retrieve_multi_kb(
    query: str,
    kb_ids: list[str],
    filter_obj: RetrievalFilter | None = None,
    tenant_id: str | None = None,
    session_id: str | None = None,
) -> tuple[list[RetrievalResult], bool, list[str]]:
    """多知识库联合检索（含可选会话文件源）

    所有选中的知识库平等参与召回（priority 统一 1.0），不区分主/副：库只是召回范围，
    不是排序信号，最终顺序由统一 rerank 按真实语义相关性决定。此前主库 1.0 / 辅助库 0.8
    的区分施加在跨库不可比的 RRF 分数上，几乎不影响最终结果（合并结果会重新送 reranker），
    边缘情况下反而可能把相关的辅助库 chunk 挤出 rerank 候选池，故取消。
    若 ``session_id`` 给定且该会话有上传文件，则把会话文件源（kb_id=
    ``SESSION_FILES_KB_ID``、priority=1.0、expr=``session_id == "{sid}"``）作为
    一个额外检索源接入，与知识库同权参与合并（Req 2.1/2.3/2.4/2.6）。会话源由此自动纳入
    bugfix H6 的 ``Semaphore`` 并发限流（占一个并发位，
    总源数 = ``len(kb_ids) + 1``）；失败按 bugfix H3 经 ``failed_kb_ids`` 透传
    （含 ``"session_files"``）供前端区分"会话文件检索失败"与"知识库检索失败"。
    返回 ``(检索结果, 是否降级, 失败知识库 ID 列表)``。

    H3：完整透传降级信息——不再丢弃 ``failed_kb_ids``，供上层填充 SSE meta 的
    ``failed_source_count`` 并据此向用户提示结果不完整。

    Args:
        tenant_id: 显式租户 ID（H5）。透传给 ``MultiKBRetriever.search``，确保流式响应中
            contextvar 已 reset 时仍能取到正确租户检索配置；None 时底层回退 contextvar。
        session_id: 当前会话 ID。非 None 且该会话已上传文件时追加会话源（Req 1.3/1.5/
            1.11）。会话源以 ``session_id == "..."`` 标量 expr 强制会话隔离。
    """
    # 构建知识库配置：所有库同权（priority=1.0），不分主/副。
    kb_configs: list[KBRetrievalConfig] = [
        KBRetrievalConfig(kb_id=kb_id, priority=1.0) for kb_id in kb_ids
    ]

    # 追加会话文件源（仅当指定 session_id 且该会话有上传文件，Req 1.3/1.5/2.4）。
    # 用 SESSION_FILES_KB_ID 常量 + session_id 标量 expr 隔离，复用 MultiKBRetriever
    # 的并发限流 + 降级透传链路，不引入新的检索分支（design C7 / C7.1）。
    if session_id:
        try:
            session_upload_service = get_session_upload_service()
            if await session_upload_service.has_files(session_id):
                kb_configs.append(
                    KBRetrievalConfig(
                        kb_id=SESSION_FILES_KB_ID,
                        # 与知识库源同权（1.0）：会话文件与所选知识库公平竞争，最终顺序交由
                        # 统一 rerank 按真实语义相关性决定。此前用 1.2 加权会让会话文件
                        # chunk 在合并排序时整体抬到 KB 之前，叠加 rerank 候选窗口截断
                        # （top_k*2）与软阈值过滤后，KB 候选被挤出 / 砍掉，导致"选了知识库
                        # 仍只答临时文件"。会话文件"刚上传即可被检索"已保证其可见性，
                        # 无需再人为加权垄断候选池。
                        priority=1.0,
                        expr=build_session_id_expr(session_id),
                    )
                )
        except Exception as e:
            # 会话源探测失败（例如 DB 临时抖动）不应阻塞正式 KB 检索；
            # 退化为"无会话源"继续主流程（Req 9.2 安全降级）。
            logger.warning(
                "探测会话文件源失败，本次检索将不包含会话源: %s",
                sanitize_for_log(e),
            )

    # 构建 HybridRetriever
    hybrid_retriever = await _build_hybrid_retriever()

    # 使用 MultiKBRetriever 执行联合检索
    multi_kb = MultiKBRetriever(hybrid_retriever)
    multi_result: MultiKBSearchResult = await multi_kb.search(
        query, kb_configs, top_k=10, filters=filter_obj, tenant_id=tenant_id
    )
    return multi_result.results, multi_result.degraded, multi_result.failed_kb_ids


async def _retrieve_chunks(
    query: str, kb_id: str, mode: str, llm: LLMProvider, progress_queue: asyncio.Queue | None = None,
    expr: str | None = None, tenant_id: str | None = None,
) -> tuple[list[RetrievalResult], bool]:
    """根据模式执行检索，返回 (检索结果, 是否降级)

    三档模式：
    - direct: 仅稠密向量检索
    - hybrid: 混合检索 + RRF + Rerank
    - agent: 完整 Agent 编排（路由→改写→迭代检索→反思）

    Args:
        progress_queue: 兼容保留参数（旧 agent 分支用于推送进度事件）。当前 agent 分支已改走
            统一的 _run_agent_nonstream，不再消费此参数；direct/hybrid 分支本就不使用。
        tenant_id: 显式租户 ID（H5）。透传给 hybrid 检索与 agent 模式的 KnowledgeSearchTool，
            确保流式响应中 contextvar 已 reset 时仍能取到正确租户检索配置；None 时底层回退 contextvar。
    """
    from app.retrieval.cache import get_retrieval_cache

    # 检查缓存（agent 模式不缓存，因为有迭代反思逻辑）
    cache = await get_retrieval_cache()
    if cache and mode != "agent":
        cached = await cache.get(kb_id, query, mode)
        if cached is not None:
            print(f"[Cache] 命中: query={query!r}, mode={mode}, 结果数={len(cached)}")
            return cached, False

    manager = get_model_manager()
    milvus = _get_milvus_client()
    settings = get_settings()

    if mode == "direct":
        # 直检索：仅稠密向量
        retriever = VectorRetriever(manager.embedder, milvus)
        results = await retriever.search(query, kb_id, top_k=10, expr=expr)
        # 写入缓存
        if cache:
            await cache.set(kb_id, query, mode, results)
        return results, False

    elif mode == "agent":
        # Agent 模式：复用统一的 _run_agent_nonstream（与流式/非流式主链路共用
        # _build_agent_runtime），不再在此维护第三份独立的工具注册/配置副本。
        # 该分支现已不在生产主链路触达（非流式单库 agent 在 chat_completions 中直接走
        # _run_agent_nonstream），保留仅为兼容潜在的内部调用方，返回 (refs, degraded)。
        answer, refs, degraded, _steps, _failed = await _run_agent_nonstream(
            query, [kb_id] if kb_id else [], llm, preset_cfg={},
            max_context_tokens=None, thinking_enabled=False,
            tenant_id=tenant_id, session_id=None, history=None,
        )
        return refs, degraded

    else:
        # hybrid 模式（默认）：混合检索 + RRF + Rerank
        hybrid_retriever = await _build_hybrid_retriever()
        # H3：用 search_with_degraded 取真实路级降级（经返回结构承载，并发安全），不再硬编码 False。
        results, degraded = await hybrid_retriever.search_with_degraded(
            query, kb_id, top_k=10, expr=expr, tenant_id=tenant_id
        )
        # 写入缓存
        if cache:
            await cache.set(kb_id, query, mode, results)
        return results, degraded


def _build_messages(
    request: ChatCompletionRequest, context: str, has_context: bool,
    history: list[dict] | None = None,
    timezone_name: str | None = None,
) -> list[dict]:
    """构建发送给 LLM 的消息列表（有检索结果时注入上下文，支持历史对话）

    Args:
        request: 请求体
        context: 检索到的参考内容
        has_context: 是否有检索结果
        history: 历史对话消息列表 [{"role": "user", "content": "..."}, ...]
    """
    base_system = render_runtime_context(timezone_name or request.timezone_name)
    if has_context:
        base_system = f"{base_system}\n\n{_SYSTEM_PROMPT.format(context=context)}"

    user_messages = [
        {"role": msg.role, "content": msg.content} for msg in request.messages
    ]
    messages = []
    messages.append({"role": "system", "content": base_system})
    # 插入历史对话（在 system 之后、当前用户消息之前）
    if history:
        messages.extend(history)
    messages.extend(user_messages)
    return messages


def _agent_event_to_sse(event: AgentEvent) -> dict | None:
    """将 AgentEvent 转换为 SSE JSON 格式

    返回格式：
    {"type": "reasoning_delta", "content": "...", "iteration": 0}
    {"type": "text_delta", "content": "...", "iteration": 0}
    {"type": "tool_call", "tool_name": "...", "tool_call_id": "...", "iteration": 0}
    {"type": "tool_result", "tool_call_id": "...", "tool_name": "...", "success": true, "duration_ms": 350}
    # MCP structuredContent 合法时额外带：
    {"type": "tool_result", ..., "structured_content": {"kind": "...", "data": {}}}
    {"type": "turn_end", "finish_reason": "stop"}
    """
    if event.type == EventType.REASONING_DELTA:
        return {
            "type": "reasoning_delta",
            "content": event.data.get("content", ""),
            "iteration": event.data.get("iteration", 0),
        }
    elif event.type == EventType.TEXT_DELTA:
        return {
            "type": "text_delta",
            "content": event.data.get("content", ""),
            "iteration": event.data.get("iteration", 0),
        }
    elif event.type == EventType.TOOL_CALL:
        return {
            "type": "tool_call",
            "tool_name": event.data.get("tool_name", ""),
            "tool_call_id": event.data.get("tool_call_id", ""),
            # 持久化调用参数（如检索 query），用于后续轮次将历史还原为结构化
            # tool_calls 消息。参数仅进入 LLM 协议字段，不在前端答案中展示。
            "arguments": event.data.get("arguments", {}),
            "iteration": event.data.get("iteration", 0),
        }
    elif event.type == EventType.TOOL_RESULT:
        sse_data = {
            "type": "tool_result",
            "tool_call_id": event.data.get("tool_call_id", ""),
            "tool_name": event.data.get("tool_name", ""),
            "success": event.data.get("success", False),
            "duration_ms": event.data.get("duration_ms", 0),
            # 本次工具读到的文件（检索类工具带出 doc_id→文件名/来源），供前端在步骤行内联
            # 展示可点击预览的文件。落库进 agent_steps，历史回放可还原。
            "files": event.data.get("files", []),
        }
        structured_content = event.data.get("structured_content")
        if structured_content is not None:
            # MCP 声明的结构化结果经通用校验后透传；原始工具正文仍不外发。
            sse_data["structured_content"] = structured_content
        return sse_data
    elif event.type == EventType.TURN_END:
        return {
            "type": "turn_end",
            "finish_reason": event.data.get("finish_reason", ""),
        }
    elif event.type == EventType.TOKEN_USAGE:
        return {
            "type": "token_usage",
            "prompt_tokens": event.data.get("prompt_tokens", 0),
            "completion_tokens": event.data.get("completion_tokens", 0),
            "total_tokens": event.data.get("total_tokens", 0),
            "max_context_tokens": event.data.get("max_context_tokens", 0),
            "current_context_tokens": event.data.get("current_context_tokens", 0),
        }
    elif event.type == EventType.ERROR:
        return {
            "type": "error",
            "content": event.data.get("error", ""),
        }
    return None


async def _register_mcp_tools(
    tool_registry: ToolRegistry,
    preset_allowed: list[str] | None,
) -> None:
    """把预设显式白名单中的外部 MCP 工具注册进 Agent 运行时

    MCP 工具由远端 MCP server 声明（DB mcp_configs 表，超管维护），对 Artoo
    而言是外部工具：**default-off**，必须显式出现在预设 allowed_tools 中才注册，
    避免注入未启用的预设（如内置 quick-qa / smart-reasoning）。工具定义带模块级
    缓存，未配置任何 server 时 get_mcp_tools 返回空列表，行为与接线前一致。
    """
    if not preset_allowed:
        return
    try:
        for mcp_tool in await get_mcp_tools():
            if mcp_tool.name in preset_allowed:
                tool_registry.register(mcp_tool)
    except Exception as e:
        logger.warning("[Agent][Runtime] MCP tool discovery failed: %s", e)


async def _has_whitelisted_mcp_tools(preset_allowed: list[str] | None) -> bool:
    """预设白名单里是否存在已启用的外部 MCP 工具（供路由判定是否必须走 ReAct）。

    与 :func:`_register_mcp_tools` 共用同一份带 TTL 缓存的发现结果，故不产生额外
    远端请求；未配置 MCP server / 预设无白名单时恒为 False，路由行为与改造前一致。
    发现失败按 False 处理（fail-safe：不因远端抖动把闲聊请求拖进 ReAct）。
    """
    if not preset_allowed:
        return False
    try:
        return any(t.name in preset_allowed for t in await get_mcp_tools())
    except Exception as e:  # noqa: BLE001 - 发现失败不阻塞主流程
        logger.warning("[Chat] MCP 工具发现失败，本次按无外部工具路由: %s", sanitize_for_log(e))
        return False


async def _build_agent_runtime(
    kb_ids: list[str],
    llm: LLMProvider,
    preset_cfg: dict,
    max_context_tokens: int | None,
    thinking_enabled: bool,  # noqa: ARG001 - Agent 链路已不使用；思考由预设独占控制（见下方 AgentConfig）。保留形参仅为与调用方签名兼容。
    tenant_id: str | None,
    session_id: str | None,
    timezone_name: str | None = None,
    include_session_source: bool = False,
    attachments: list[dict] | None = None,
    custom_skills: list | None = None,
    hybrid_retriever: HybridRetriever | None = None,
    caller_ctx: ToolContext | None = None,
    max_output_tokens: int | None = None,
) -> tuple[AgentEngine, AgentState, EventBus]:
    """构建 Agent 运行时（工具注册 + 配置 + 引擎），流式与非流式共用。

    统一两条链路的 Agent 编排，消除「流式 / 非流式各自重建一套」导致的行为分叉
    （预设 allowed_tools 过滤、thinking、temperature、system_prompt 等过去仅流式生效）。

    检索源装配（agent-session-source-unification）：把全部所选知识库与（可选）会话文件源
    组装为 ``KnowledgeSearchTool`` 的 ``search_targets``，让会话文件成为 Agent 可多轮检索的
    普通数据源，而非靠"换检索路径"接入：
    - 每个 ``kb_id`` → ``SearchTarget(kb_id, expr=None)``（正式知识库，无 session 概念）。
    - ``include_session_source and session_id`` → 追加
      ``SearchTarget(SESSION_FILES_KB_ID, expr=session_id 标量过滤)``（会话级隔离，Property 1）。
    ``grep_chunks`` / ``list_knowledge_chunks`` 仅接主库 ``kb_ids[0]``，不接会话源
    （design C2 有意决策：会话 chunk 在独立 session_chunks 表，跨表接入成本高、收益低；
    knowledge_search 的混合检索已覆盖会话文件召回）。

    Args:
        kb_ids: 全部所选知识库 ID（取代旧的单个 kb_id）。
        include_session_source: 是否把会话文件源加入 knowledge_search 检索（由装配层据
            session_has_files 决定，不暴露给 LLM；Property 5）。
        attachments: 本条消息绑定的附件快照列表（来自 request.attachments，每项含
            file_id / filename）。非空时注册 read_attachment 工具，让 Agent 能确定性地
            整篇直读本次附件，而非靠 knowledge_search 语义检索去和知识库文档竞争召回。
        caller_ctx: 本次请求的调用方上下文（会话 / 租户 / 主体），由路由层从 identity
            构造。仅外部 MCP 工具消费——它需要把"哪个终端用户在调"如实带给第三方
            server，第三方才能做自己的权限隔离。内置工具不受影响。

    Returns:
        (engine, state, event_bus)。其中 ``state`` 是传给工具的 AgentState，
        ``knowledge_refs`` / ``degraded`` 由工具写入此对象；引擎 ``execute()`` 返回的
        是另一个内部 state，``final_answer`` / ``steps`` 在那上面（与既有约定一致）。
    """
    milvus = _get_milvus_client()
    # 优先使用调用方在 async 上下文构建好的检索器（含按门控注入的图谱第四路）。
    # 兜底：未传入时同步构建三路检索器（不含图谱，graph_retriever 默认 None），保证任何
    # 潜在的同步调用方仍可用且行为与未引入图谱时一致（Property 8）。
    if hybrid_retriever is None:
        _manager = get_model_manager()
        hybrid_retriever = HybridRetriever(
            vector_retriever=VectorRetriever(_manager.embedder, milvus),
            sparse_retriever=SparseRetriever(_manager.embedder, milvus),
            rerank_provider=_manager.reranker,
            db_session_factory=async_session,
            bm25_retriever=BM25Retriever(milvus),
        )
    bm25_retriever = BM25Retriever(milvus)

    state = AgentState()
    tool_registry = ToolRegistry()
    event_bus = EventBus()

    # 主库：grep_chunks / list_knowledge_chunks 这类单库工具的归属库（kb_ids 为空时为 None）。
    primary_kb_id = kb_ids[0] if kb_ids else None

    # 装配 knowledge_search 的检索源：正式 KB 源（expr=None）+ 可选会话文件源（带 session_id expr）。
    search_targets: list[SearchTarget] = [SearchTarget(kb_id=k, expr=None) for k in kb_ids]
    if include_session_source and session_id:
        search_targets.append(
            SearchTarget(kb_id=SESSION_FILES_KB_ID, expr=build_session_id_expr(session_id))
        )

    # 按预设 allowed_tools 过滤；由请求能力动态决定的基础设施工具豁免白名单。
    # 回答不再承载在工具里；native reasoning 由模型通道直接提供。
    _INFRA_TOOLS = {"read_attachment", "read_skill"}
    preset_allowed = preset_cfg.get("allowed_tools")

    def _tool_enabled(tool_name: str) -> bool:
        if tool_name in _INFRA_TOOLS:
            return True
        if not preset_allowed:
            return True
        return tool_name in preset_allowed

    if _tool_enabled("knowledge_search") and search_targets:
        tool_registry.register(
            KnowledgeSearchTool(hybrid_retriever, state=state, tenant_id=tenant_id, search_targets=search_targets)
        )
    # grep_chunks / list_knowledge_chunks 仅作用于主库，不接会话源（design C2 决策，非遗漏）。
    if _tool_enabled("grep_chunks") and primary_kb_id:
        tool_registry.register(GrepChunksTool(bm25_retriever, primary_kb_id, state))
    if _tool_enabled("list_knowledge_chunks"):
        tool_registry.register(ListKnowledgeChunksTool())
    # read_attachment：本条消息绑定附件 → 注册确定性整篇直读工具。file_id 在此锚定
    # （来自 request.attachments），LLM 不能指定/伪造，只能选读哪个 filename 或翻页，
    # 杜绝越权；附件解析不再丢进 knowledge_search 与知识库文档竞争召回（WeKnora 借鉴）。
    anchored_attachments = [a for a in (attachments or []) if a.get("file_id")]
    read_attachment_on = bool(
        _tool_enabled("read_attachment") and anchored_attachments and session_id
    )
    if read_attachment_on:
        tool_registry.register(ReadAttachmentTool(session_id, anchored_attachments))
    # 可选工具：web_search（需配置 searxng_url 且预设允许）
    settings = get_settings()
    web_search_on = bool(settings.searxng_url) and _tool_enabled("web_search")
    if web_search_on:
        tool_registry.register(WebSearchTool(searxng_url=settings.searxng_url))

    # Skills（Progressive Disclosure）：扫描内置技能目录拿到 Level 1 元数据
    # （name+description），注入 system prompt 供模型判断是否需要某个技能；模型按需
    # 调用 read_skill 工具加载 Level 2 完整指令。预设可用 allowed_skills 收敛白名单
    # （None=全部允许）。无任何技能时不注册 read_skill，避免提示与工具列表矛盾。
    preset_allowed_skills = preset_cfg.get("allowed_skills")
    skill_manager = SkillManager(
        skill_dirs=default_skill_dirs(),
        allowed_skills=preset_allowed_skills,
        extra_skills=custom_skills or None,
    )
    skill_metadata = skill_manager.get_all_metadata() if _tool_enabled("read_skill") else []
    if skill_metadata:
        tool_registry.register(ReadSkillTool(skill_manager))

    # 外部 MCP 工具：发现 DB 中启用的 MCP server 工具。
    # 与内置工具 default-on 不同，外部工具 **default-off** —— 必须显式出现在预设
    # allowed_tools 中才会注册，避免把远端声明的工具注入未启用的预设（如内置
    # quick-qa / smart-reasoning）。工具定义带模块级缓存，未配置任何 server 时
    # get_mcp_tools 返回空列表，Agent 行为与接线前完全一致。
    await _register_mcp_tools(tool_registry, preset_allowed)

    # 诊断日志：确认实际注册的工具列表与预设白名单，定位 read_attachment 是否生效。
    logger.info(
        "[Agent][Runtime] registered_tools=%s | preset_allowed=%s | attachments=%d",
        tool_registry.list_tools(),
        preset_allowed,
        len(anchored_attachments),
    )

    # system_prompt：核心 Progressive RAG 模板恒定，预设仅追加用户自定义指令
    # （角色 / 语气 / 工作流 / 边界）。render_system_prompt 同时做占位符替换
    # （{knowledge_base_names} / {available_tools}）。
    # kb_names 用 kb_ids 渲染；含会话源时追加固定显示名，让 LLM 知道有"本会话上传的文件"可检索。
    kb_names = list(kb_ids)
    if include_session_source and session_id:
        kb_names.append("本会话上传的文件")
    # 本条消息附件：仅当 read_attachment 工具确实注册时，才在 prompt 里提示"用
    # read_attachment 直接读取"，避免"提示说有、工具列表没有"的矛盾让模型空转。
    if read_attachment_on:
        att_names = "、".join(a.get("filename", "") for a in anchored_attachments)
        kb_names.append(f"本条消息附件（用 read_attachment 直接读取）：{att_names}")
    custom_instructions = (preset_cfg.get("custom_instructions") or "").strip()
    system_prompt = render_system_prompt(
        AgentConfig(custom_instructions=custom_instructions),
        kb_names=kb_names,
        available_tools=tool_registry.list_tools(),
        web_search_enabled=web_search_on,
        skills=[(m.name, m.description) for m in skill_metadata],
        timezone_name=timezone_name,
    )
    config = AgentConfig(
        max_iterations=preset_cfg.get("max_iterations", settings.agent_max_iterations),
        max_context_tokens=max_context_tokens or AgentConfig.max_context_tokens,
        max_output_tokens=max_output_tokens,
        temperature=preset_cfg.get("temperature", AgentConfig.temperature),
        web_search_enabled=web_search_on,
        # thinking_enabled 现在只表示“是否开启模型原生 thinking/reasoning 通道”，
        # 不再注册 ThinkingTool。小模型没有独立通道时，loop 会在服务端做能力兼容。
        thinking_enabled=preset_cfg.get("thinking_enabled", True),
        system_prompt=system_prompt,
        custom_instructions=custom_instructions,
    )
    # caller_ctx 交给引擎：执行工具时按需下传（仅外部 MCP 工具消费，用于把调用方身份
    # 透传给第三方 server；是否真的发出去由每个 MCP 配置的 forward_context 决定）。
    engine = AgentEngine(config, llm, tool_registry, event_bus, tool_context=caller_ctx)
    return engine, state, event_bus


async def _run_agent_nonstream(
    query: str,
    kb_ids: list[str],
    llm: LLMProvider,
    preset_cfg: dict,
    max_context_tokens: int | None,
    thinking_enabled: bool,
    tenant_id: str | None,
    session_id: str | None,
    history: list[dict] | None,
    timezone_name: str | None = None,
    include_session_source: bool = False,
    attachments: list[dict] | None = None,
    owner_user_id: str | None = None,
    caller_ctx: ToolContext | None = None,
    max_output_tokens: int | None = None,
) -> tuple[str, list[RetrievalResult], bool, list[dict], list[str]]:
    """非流式运行 Agent ReAct 引擎，直接返回其最终答案（不二次走普通 RAG 生成）。

    与流式路径共用 ``_build_agent_runtime``，消除双入口分叉。事件经 handler 收集为
    agent_steps 落库，保证与流式会话历史结构一致（前端可还原思考/工具步骤）。

    Args:
        kb_ids: 全部所选知识库 ID（取代旧的单个 kb_id）。
        include_session_source: 是否把会话文件源加入 agent 检索（由路由层据 session_has_files 决定）。
        attachments: 本条消息绑定的附件快照列表（来自 request.attachments），透传给
            _build_agent_runtime 注册 read_attachment 工具，供 Agent 确定性整篇直读本次附件。

    Returns:
        (final_answer, knowledge_refs, degraded, agent_steps, failed_source_ids)
    """
    engine, state, event_bus = await _build_agent_runtime(
        kb_ids, llm, preset_cfg, max_context_tokens, thinking_enabled, tenant_id, session_id,
        timezone_name=timezone_name,
        include_session_source=include_session_source,
        attachments=attachments,
        custom_skills=await load_user_custom_skills(owner_user_id),
        hybrid_retriever=await _build_hybrid_retriever(),
        caller_ctx=caller_ctx,
        max_output_tokens=max_output_tokens,
    )

    steps_collected: list[dict] = []
    # 步骤面板耗时截止于首个 text_delta 事件（答案开始产出），与流式路径语义一致。
    text_delta_at: float | None = None

    async def _collect(event: AgentEvent):
        nonlocal text_delta_at
        sse = _agent_event_to_sse(event)
        if sse:
            if text_delta_at is None and sse.get("type") == "text_delta":
                text_delta_at = time.time()
            steps_collected.append(sse)

    event_bus.on(None, _collect)

    llm_context = history if history else None
    agent_start_time = time.time()
    result_state: AgentState | None = None
    try:
        result_state = await engine.execute(session_id or "", query, llm_context=llm_context)
        answer = result_state.final_answer or ""
        degraded = state.degraded
    except Exception as e:
        # 引擎内部已兜住 LLM 永久错误 / max_iterations；此处仅捕获其余未预期异常，
        # 返回友好降级文案而非让请求 500，保证非流式链路不中断。
        logger.error("非流式 Agent 执行异常: %s", sanitize_for_log(e))
        answer = "抱歉，处理您的请求时发生了错误，请稍后重试。"
        degraded = True

    total_steps = len(result_state.steps) if result_state else 0
    duration_end = text_delta_at if text_delta_at is not None else time.time()
    steps_collected.append({
        "type": "complete",
        "total_steps": total_steps,
        "total_duration_ms": int((duration_end - agent_start_time) * 1000),
    })
    return answer, state.knowledge_refs, degraded, steps_collected, list(state.failed_source_ids)


async def _stream_response(
    request: ChatCompletionRequest,
    query: str,
    kb_id: str | None,
    mode: str,
    llm: LLMProvider,
    stream_enabled: bool = True,
    max_context_tokens: int | None = None,
    thinking_enabled: bool = False,
    expr: str | None = None,
    kb_ids: list[str] | None = None,
    history: list[dict] | None = None,
    session_id: str | None = None,
    preset_cfg: dict | None = None,
    tenant_id: str | None = None,
    retrieval_query: str | None = None,
    skip_retrieval: bool = False,
    timezone_name: str | None = None,
    attachments: list | None = None,
    requested_kb_ids: list[str] | None = None,
    session_has_files: bool = False,
    multi_kb_requested: bool = False,
    owner_user_id: str | None = None,
    caller_ctx: ToolContext | None = None,
) -> AsyncGenerator[str, None]:
    """生成 SSE 流式响应，包含 Agent 进度事件

    路由统一（agent-session-source-unification）：用 ``_resolve_retrieval_route`` 决定本次
    走 AGENT / MULTI_KB / SINGLE_KB / CHITCHAT / NONE，与非流式入口共用同一决策，
    保证一致性（Req 2.4）。``mode==agent`` 恒走 AGENT（含会话源由 session_has_files 决定），
    不再因有会话文件被降级为单轮 MultiKB（Property 2）。

    Args:
        query: 用户原始问题，用于答案生成与消息保存。
        kb_id / kb_ids: 仅用于消息持久化与单库回退展示（与改造前一致），不参与路由判定。
        requested_kb_ids: 已统一的所选知识库 ID（单选 + 多选合并），路由与检索实际使用。
        session_has_files: 当前会话是否有上传文件，决定 agent 是否引入会话源 / 非 agent 是否走多源。
        retrieval_query: 经查询理解改写后、用于检索的查询（None 时回退到 query）。
            仅单轮检索链路（direct/hybrid）使用；agent 模式自行处理指代。
        skip_retrieval: 查询理解判定为闲聊/纯历史追问时为 True，跳过检索直接作答。
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    preset_cfg = preset_cfg or {}
    if retrieval_query is None:
        retrieval_query = query
    requested_kb_ids = list(requested_kb_ids or [])

    # 统一路由决策（与非流式入口共用纯函数）。
    # 外部 MCP 工具在白名单内时，agent 即使没有任何检索源也必须走 ReAct（否则工具
    # 定义不会下发给模型）。
    route = _resolve_retrieval_route(
        mode, requested_kb_ids, session_has_files, skip_retrieval,
        multi_kb_requested=multi_kb_requested,
        external_tools_available=await _has_whitelisted_mcp_tools(preset_cfg.get("allowed_tools")),
    )

    # 发起对话即入库：在检索/生成之前先保存用户消息并播种标题，使新会话立即出现在
    # 侧栏（侧栏过滤无消息空会话）。AI 答完后再异步精炼标题（仅首轮）。
    is_first_round = False
    if session_id:
        try:
            is_first_round = await _persist_user_message_and_seed_title(
                session_id, query, kb_id=kb_id, kb_ids=kb_ids,
                tenant_id=tenant_id, attachments=attachments,
            )
        except Exception as e:
            logger.warning("入库用户消息失败: %s", e)

    # Agent 模式：边检索边推送进度
    chunks: list[RetrievalResult] = []
    degraded = False
    failed_kb_ids: list[str] = []  # 哪些源失败（含 SESSION_FILES_KB_ID 时为会话源失败），供前端区分提示
    agent_steps_collected: list[dict] = []

    if route == Route.AGENT:
        # Agent 模式：使用 EventBus→SSE 桥接，始终走 ReAct（不因会话文件降级，Property 2）。
        # 创建 asyncio.Queue 接收 AgentEvent
        event_queue: asyncio.Queue = asyncio.Queue()

        # 构建 Agent 运行时（与非流式共用 _build_agent_runtime，消除双入口分叉）。
        # 传入全部所选库 + 会话源标志，让会话文件成为 agent 可检索的普通数据源。
        engine, state, event_bus = await _build_agent_runtime(
            requested_kb_ids, llm, preset_cfg, max_context_tokens, thinking_enabled,
            tenant_id, session_id, timezone_name=timezone_name,
            include_session_source=session_has_files,
            attachments=attachments,
            custom_skills=await load_user_custom_skills(owner_user_id),
            hybrid_retriever=await _build_hybrid_retriever(),
            caller_ctx=caller_ctx,
            max_output_tokens=request.max_tokens,
        )

        async def _event_to_queue(event: AgentEvent):
            await event_queue.put(event)

        event_bus.on(None, _event_to_queue)

        # 构建 LLM 上下文（历史对话）
        llm_context = history if history else None

        # 启动 Agent 执行任务
        agent_start_time = time.time()
        agent_task = asyncio.create_task(
            engine.execute(session_id or "", query, llm_context=llm_context)
        )

        # 从 event_queue 读取事件并转换为 SSE JSON。
        # 用 try/finally 保证：无论 agent_task 是否抛异常，都先排空队列里已产生的事件
        # 再发 complete/meta、落库，避免「result() 重抛异常 → 排空被跳过 → SSE 断流、
        # 事件与会话历史丢失」。
        result_state: AgentState | None = None
        agent_error: Exception | None = None
        # 步骤面板耗时的截止时刻：首个 text_delta 事件触发时（答案开始产出）。
        # 步骤面板统计的是「执行步骤（思考 + 工具调用）」的耗时，不含答案正文的流式输出，
        # 故在此截止，而非等整个引擎跑完（含答案 token 全部流完）。
        text_delta_at: float | None = None
        cancelled = False
        try:
            while not agent_task.done():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    sse_data = _agent_event_to_sse(event)
                    if sse_data:
                        if text_delta_at is None and sse_data.get("type") == "text_delta":
                            text_delta_at = time.time()
                        agent_steps_collected.append(sse_data)
                        yield json.dumps(sse_data, ensure_ascii=False)
                except asyncio.TimeoutError:
                    continue

            # 获取最终状态；agent_task 抛异常时在此捕获，转为友好降级而非断流。
            try:
                result_state = agent_task.result()
            except Exception as e:
                agent_error = e
                logger.error("流式 Agent 执行异常: %s", sanitize_for_log(e))
        except asyncio.CancelledError:
            # 用户点「停止」或客户端断开：SSE 生成器被取消。Agent 在独立 task 中运行，
            # 不会随生成器自动取消，必须显式 cancel 回收，避免空跑浪费 LLM token。
            # 已产出的部分答案落库（被中断的回答也保留在历史里，可继续追问/重试）。
            cancelled = True
            logger.info("[Chat] 流式输出被中断，停止 Agent 执行并保存部分答案")
            if not agent_task.done():
                agent_task.cancel()
            try:
                await asyncio.shield(agent_task)
            except BaseException:  # noqa: BLE001 - 任务已取消/异常，回收即可
                pass
            partial_answer = "".join(
                s.get("content", "")
                for s in agent_steps_collected
                if s.get("type") == "text_delta" and s.get("content")
            )
            if session_id and partial_answer:
                try:
                    refs = await _build_references(state.knowledge_refs)
                    refs_data = [r.model_dump() for r in refs] if refs else None
                    await asyncio.shield(
                        _save_message(
                            session_id, "assistant", partial_answer,
                            references=refs_data,
                            agent_steps=agent_steps_collected or None,
                            kb_id=kb_id, kb_ids=kb_ids, tenant_id=tenant_id,
                        )
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("中断后保存部分答案失败: %s", sanitize_for_log(e))
            raise
        finally:
            # 取消路径下客户端已断开，再 yield 会二次抛错；仅正常路径排空剩余事件。
            if not cancelled:
                while not event_queue.empty():
                    event = event_queue.get_nowait()
                    sse_data = _agent_event_to_sse(event)
                    if sse_data:
                        if text_delta_at is None and sse_data.get("type") == "text_delta":
                            text_delta_at = time.time()
                        agent_steps_collected.append(sse_data)
                        yield json.dumps(sse_data, ensure_ascii=False)

        # 自然停止的 assistant text 就是最终响应；knowledge_refs 是引用。
        # 注意：工具持有的 state 对象和引擎内部的 state 是不同的，
        # knowledge_refs 被 KnowledgeSearchTool 写入到传给工具的 state 中。
        chunks = state.knowledge_refs
        # H3：agent 模式 degraded 取工具写入的真实降级状态（不再恒 False）。
        # KnowledgeSearchTool 持有此 state，检索源失败/路级降级时置 state.degraded=True。
        degraded = state.degraded
        # 失败源透传（agent-session-source-unification）：工具按源记录失败 kb_id（含会话源的
        # SESSION_FILES_KB_ID），据此派生 failed_kb_ids / session_source_failed 供前端区分提示。
        if state.failed_source_ids:
            failed_kb_ids = list(state.failed_source_ids)
        full_response = result_state.final_answer if result_state else ""

        if agent_error is not None:
            # 引擎抛出未兜住的异常：补发降级答案 + 标记 degraded，保证前端有正文、链路不中断。
            degraded = True
            if not full_response:
                full_response = "抱歉，处理您的请求时发生了错误，请稍后重试。"
                answer_event = {"type": "text_delta", "content": full_response, "iteration": 0}
                agent_steps_collected.append(answer_event)
                yield json.dumps(answer_event, ensure_ascii=False)
            turn_end_event = {"type": "turn_end", "finish_reason": "error"}
            agent_steps_collected.append(turn_end_event)
            yield json.dumps(turn_end_event, ensure_ascii=False)
        full_response = full_response or ""

        # 发射 complete 事件（携带整体耗时，供前端步骤统计展示）
        # 耗时截止于首个 text_delta 事件（答案开始产出）；若全程无 text_delta
        # （异常兜底等），退回到当前时刻，保证始终有合理值。
        duration_end = text_delta_at if text_delta_at is not None else time.time()
        total_duration_ms = int((duration_end - agent_start_time) * 1000)
        complete_event = {
            "type": "complete",
            "total_steps": len(result_state.steps) if result_state else 0,
            "total_duration_ms": total_duration_ms,
        }
        agent_steps_collected.append(complete_event)
        yield json.dumps(complete_event, ensure_ascii=False)

        # 发送引用来源和元数据
        references = await _build_references(chunks)
        meta_event = {
            "references": [ref.model_dump() for ref in references],
            "metadata": {
                "retrieval_mode": mode,
                "degraded": degraded,
                "llm_degraded": False,
                **_build_degraded_metadata(failed_kb_ids),
            },
        }
        yield json.dumps(meta_event, ensure_ascii=False)

        # 保存助手消息到会话（用户消息已在生成前入库；不阻塞 SSE 关闭）
        if session_id and full_response:
            try:
                refs_data = [ref.model_dump() for ref in references] if references else None
                steps_data = agent_steps_collected if agent_steps_collected else None
                saved_id = await _save_message(session_id, "assistant", full_response, references=refs_data, agent_steps=steps_data, kb_id=kb_id, kb_ids=kb_ids, tenant_id=tenant_id)
                # 回传已落库的 assistant 消息 ID，供前端做反馈（点赞/踩）与重试定位。
                yield json.dumps({"type": "message_saved", "message_id": saved_id}, ensure_ascii=False)
                # 标题精炼放到后台，不阻塞 SSE 关闭（仅首轮精炼，已有问题标题兜底）
                if is_first_round:
                    asyncio.create_task(_refine_session_title(session_id, query, full_response))
            except Exception as e:
                logger.warning("保存会话消息失败: %s", e)

        # Agent 模式到此结束，不走后续的 LLM 生成流程
        return

    elif route == Route.MULTI_KB:
        # 非 agent 的多源单轮检索（多选库 / 单库+会话文件 / 仅会话文件），现状不变。
        # 用 requested_kb_ids（已统一单选+多选）；仅会话文件场景为 []，由 _retrieve_multi_kb
        # 内部按 session_id 追加单源 cfg。
        try:
            filter_obj = RetrievalFilter(doc_ids=request.filter_doc_ids)
            # H3：完整接收 (results, degraded, failed_kb_ids)，不丢弃失败列表。
            chunks, degraded, failed_kb_ids = await _retrieve_multi_kb(
                retrieval_query, list(requested_kb_ids), filter_obj, tenant_id=tenant_id,
                session_id=session_id,
            )
        except Exception as e:
            # H3：except 分支异常导致结果缺失 → degraded=True（不硬编码 False）。
            logger.error("多知识库联合检索失败: %s", sanitize_for_log(e))
            chunks = []
            degraded = True
            # 整体异常无法区分具体失败源；含会话源（仅会话文件即 requested_kb_ids 为空）时
            # 把会话源标记为失败以便前端区分提示。
            failed_kb_ids = list(requested_kb_ids) if requested_kb_ids else [SESSION_FILES_KB_ID]

    elif route == Route.SINGLE_KB:
        # 非 agent 的单库单轮检索，现状不变。路由保证 requested_kb_ids 恰有一个库。
        single_kb_id = requested_kb_ids[0]
        try:
            chunks, degraded = await _retrieve_chunks(retrieval_query, single_kb_id, mode, llm, expr=expr, tenant_id=tenant_id)
        except Exception as e:
            # H3：检索整体异常导致结果为空 → degraded=True（不硬编码 False）。
            logger.error("检索失败: %s", sanitize_for_log(e))
            chunks = []
            degraded = True
            # 单库 direct/hybrid 路径只有正式知识库源（无会话源），失败即知识库源失败。
            failed_kb_ids = [single_kb_id]

    # else: route 为 CHITCHAT / NONE → 不检索，chunks 保持空，直接走下方 LLM 作答。

    # 构建上下文和消息
    context = _build_context(chunks, max_tokens=max_context_tokens)
    has_context = len(chunks) > 0
    messages = _build_messages(
        request, context, has_context, history=history,
        timezone_name=request.timezone_name,
    )
    llm_degraded = False

    # 发送第一个 chunk（包含 role）
    first_chunk = ChatCompletionChunk(
        id=completion_id,
        choices=[StreamChoice(delta=DeltaContent(role="assistant"))],
    )
    yield json.dumps(first_chunk.model_dump(), ensure_ascii=False)

    # 流式生成内容，LLM 异常时降级为返回检索上下文
    llm_kwargs = {}
    if thinking_enabled:
        llm_kwargs["enable_thinking"] = True
    else:
        llm_kwargs["enable_thinking"] = False
    full_response = ""
    try:
        if stream_enabled:
            def _emit_content(text: str) -> str:
                nonlocal full_response
                full_response += text
                chunk_data = ChatCompletionChunk(
                    id=completion_id,
                    choices=[StreamChoice(delta=DeltaContent(content=text))],
                )
                return json.dumps(chunk_data.model_dump(), ensure_ascii=False)

            async for token in llm.stream(messages, **llm_kwargs):
                yield _emit_content(token)
        else:
            result = await llm.generate(messages, **llm_kwargs)
            full_response = result
            chunk_size = 4
            for i in range(0, len(full_response), chunk_size):
                chunk_data = ChatCompletionChunk(
                    id=completion_id,
                    choices=[StreamChoice(delta=DeltaContent(content=full_response[i:i + chunk_size]))],
                )
                yield json.dumps(chunk_data.model_dump(), ensure_ascii=False)
    except asyncio.CancelledError:
        # 用户点「停止」/客户端断开：保存已生成的部分正文到会话历史后再抛出，
        # 使被中断的回答与 agent 分支一致地保留、可继续追问。
        logger.info("[Chat] 非 Agent 流式输出被中断，保存部分答案")
        if session_id and full_response:
            try:
                await asyncio.shield(
                    _save_message(
                        session_id, "assistant", full_response,
                        kb_id=kb_id, kb_ids=kb_ids, tenant_id=tenant_id,
                    )
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("中断后保存部分答案失败: %s", sanitize_for_log(e))
        raise
    except Exception as e:
        logger.warning("LLM 流式生成失败，降级为纯检索结果: %s", e)
        llm_degraded = True
        full_response = context
        # 降级：直接输出检索上下文
        fallback_chunk = ChatCompletionChunk(
            id=completion_id,
            choices=[StreamChoice(delta=DeltaContent(content=context))],
        )
        yield json.dumps(fallback_chunk.model_dump(), ensure_ascii=False)

    # 发送结束标记
    final_chunk = ChatCompletionChunk(
        id=completion_id,
        choices=[StreamChoice(delta=DeltaContent(), finish_reason="stop")],
    )
    yield json.dumps(final_chunk.model_dump(), ensure_ascii=False)

    # 发送引用来源和元数据（作为额外事件）
    references = await _build_references(chunks)
    meta_event = {
        "references": [ref.model_dump() for ref in references],
        "metadata": {
            "retrieval_mode": mode,
            "degraded": degraded or llm_degraded,
            "llm_degraded": llm_degraded,
            **_build_degraded_metadata(failed_kb_ids),
        },
    }
    yield json.dumps(meta_event, ensure_ascii=False)

    # 保存助手消息到会话（用户消息已在生成前入库）
    if session_id and full_response:
        try:
            refs_data = [ref.model_dump() for ref in references] if references else None
            steps_data = agent_steps_collected if agent_steps_collected else None
            saved_id = await _save_message(session_id, "assistant", full_response, references=refs_data, agent_steps=steps_data, kb_id=kb_id, kb_ids=kb_ids, tenant_id=tenant_id)
            yield json.dumps({"type": "message_saved", "message_id": saved_id}, ensure_ascii=False)
            if is_first_round:
                asyncio.create_task(_refine_session_title(session_id, query, full_response))
        except Exception as e:
            logger.warning("保存会话消息失败: %s", e)


@router.post("/v1/chat/completions")
@router.post("/api/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    identity: IdentityContext = Depends(require_authenticated()),
):
    """Chat Completion 端点（OpenAI 兼容）

    支持流式和非流式两种响应模式，集成三档检索模式调度。
    """
    # 提取用户最后一条消息作为查询
    user_query = ""
    for msg in reversed(request.messages):
        if msg.role == "user":
            user_query = msg.content
            break

    if not user_query:
        raise HTTPException(status_code=400, detail="消息列表中缺少 user 角色消息")

    tenant_id = identity.tenant_id

    # 调用方上下文：仅在开启透传的外部 MCP server 上生效（默认全部关闭），
    # 用于让第三方按"哪个终端用户在调"做自己的权限隔离。见 app/mcp/context.py。
    caller_ctx = ToolContext.from_identity(
        identity, session_id=request.session_id, request_id=uuid.uuid4().hex
    )

    # 会话归属校验：若指定了 session_id，必须为本人会话，否则 404。
    # 防止凭他人 session_id 读取其历史（泄露进本次回答上下文）或向其会话注入消息。
    if request.session_id:
        await _verify_session_owner(request.session_id, identity)

    # 检索范围授权：触达 Milvus 前先校验所有被指定 KB 处于身份可读范围
    # （跨租户/不可读 -> 404；跨库问答 MultiKBRetriever 逻辑不变，仅前置裁剪）。
    requested_kb_ids: list[str] = []
    if request.kb_ids:
        requested_kb_ids = list(request.kb_ids)
    elif request.knowledge_base_id:
        requested_kb_ids = [request.knowledge_base_id]
    if requested_kb_ids:
        async with async_session() as _authz_session:
            await authorize_requested_kbs(_authz_session, identity, requested_kb_ids, KbAccessEnum.READ)

    # 加载生效的 Agent 预设（指定 > 默认 > 内置兜底）。
    # 传入 identity：显式指定的预设须在调用者可见范围内（内置 ∪ 自有 ∪ 本租户已开放），
    # 否则忽略回退默认，防止凭 id 使用他人私有预设。
    preset_cfg = await get_effective_preset_config(request.agent_preset_id, identity)

    # 确定检索模式：显式 retrieval_mode > 预设的 agent_mode > 默认 agent
    # 预设统一承载"快速问答(hybrid 单轮) / 智能推理(agent 多步)"的模式选择
    mode = request.retrieval_mode or preset_cfg.get("agent_mode") or "agent"

    # 获取 LLM 实例（根据 model_config_id 动态选择）
    llm, stream_enabled, max_context_tokens = await _get_llm_for_request(request.model_config_id)
    # 深度思考由智能体预设独占控制（不再来自模型配置）：简单 RAG 链路与 Agent 链路一致，
    # 统一取预设的 thinking_enabled。thinking 写入请求体的字段格式由 LLMConfig.thinking_control
    # 决定（已在构造 VllmLLM 时注入）。
    thinking_enabled = bool(preset_cfg.get("thinking_enabled", False))

    print(f"[Chat] query={user_query!r}, kb={request.knowledge_base_id}, mode={mode}, model_config={request.model_config_id}, preset={request.agent_preset_id}, stream={request.stream}, session={request.session_id}")

    # 加载会话历史上下文
    history: list[dict] | None = None
    if request.session_id:
        try:
            history = await _load_session_history(request.session_id)
        except Exception as e:
            logger.warning("加载会话历史失败: %s", e)
            history = None

    # 判断是否使用多知识库联合检索（包含"仅会话文件"场景，Req 1.4）：
    # - 选了 KB → 走多源链路；
    # - 未选 KB 但当前会话有上传文件 → 走多源链路（会话源单源处理，Req 2.4）；
    # - 否则按既有单库 / 闲聊分支。
    # 探测会话文件存在性失败（DB 抖动等）按"无会话文件"降级，不阻塞主流程（Req 9.2）。
    session_has_files = False
    if request.session_id:
        try:
            session_has_files = await get_session_upload_service().has_files(
                request.session_id
            )
        except Exception as e:
            logger.warning(
                "探测会话文件源失败，本次按无会话文件处理: %s",
                sanitize_for_log(e),
            )
            session_has_files = False
    use_multi_kb = bool(request.kb_ids) or session_has_files

    # 查询理解（仅单轮检索链路 direct/hybrid 需要）：
    # 这类链路无 ReAct 自我修正机会，必须在检索前一次性消解指代、判别闲聊，
    # 否则「它/这个」会直接进检索、闲聊也会触发无关召回。
    # agent 模式由其系统提示词内的 Turn Intent / Context Resolution 自行处理，不在此重复。
    retrieval_query = user_query
    skip_retrieval = False
    if mode != "agent" and (request.knowledge_base_id or use_multi_kb):
        understanding = await understand_query(llm, user_query, history)
        retrieval_query = understanding.rewrite_query
        skip_retrieval = not understanding.needs_retrieval

    # 构造过滤条件
    filter_obj = RetrievalFilter(doc_ids=request.filter_doc_ids)
    expr = filter_obj.to_milvus_expr()

    # 执行检索（未指定知识库时跳过检索）
    chunks: list[RetrievalResult] = []
    degraded = False
    failed_kb_ids: list[str] = []  # 失败源（含 SESSION_FILES_KB_ID 时为会话源），供前端区分提示

    # 流式响应（检索和生成一体化，支持进度推送）
    # use_multi_kb=True 时把"选中的全部库"（单选 knowledge_base_id 或多选 kb_ids，
    # 已由 requested_kb_ids 统一）传入；仅会话文件场景为 []；use_multi_kb=False → None。
    # 注意：此前用 request.kb_ids 会在"单选库 + 会话有临时文件"时丢掉选中的单库
    # （单选走 knowledge_base_id，kb_ids 为空），导致多源检索只查会话文件、漏掉知识库。
    if request.stream:
        stream_kb_ids = list(requested_kb_ids) if use_multi_kb else None
        attachments_data = (
            [a.model_dump() for a in request.attachments] if request.attachments else None
        )
        return EventSourceResponse(
            _stream_response(request, user_query, request.knowledge_base_id, mode, llm, stream_enabled, max_context_tokens, thinking_enabled, expr=expr, kb_ids=stream_kb_ids, history=history, session_id=request.session_id, preset_cfg=preset_cfg, tenant_id=tenant_id, retrieval_query=retrieval_query, skip_retrieval=skip_retrieval, timezone_name=request.timezone_name, attachments=attachments_data, requested_kb_ids=requested_kb_ids, session_has_files=session_has_files, multi_kb_requested=bool(request.kb_ids), owner_user_id=identity.acting_subject_id, caller_ctx=caller_ctx),
            media_type="text/event-stream",
        )

    # 非流式响应
    # 统一路由决策（与流式入口共用纯函数）。
    route = _resolve_retrieval_route(
        mode, requested_kb_ids, session_has_files, skip_retrieval,
        multi_kb_requested=bool(request.kb_ids),
        external_tools_available=await _has_whitelisted_mcp_tools(preset_cfg.get("allowed_tools")),
    )

    # 发起对话即入库：生成回答前先保存用户消息并播种标题（与流式入口一致）。
    is_first_round = False
    nonstream_msg_kb_ids = list(requested_kb_ids) if len(requested_kb_ids) > 1 else None
    nonstream_attachments = (
        [a.model_dump() for a in request.attachments] if request.attachments else None
    )
    if request.session_id:
        try:
            is_first_round = await _persist_user_message_and_seed_title(
                request.session_id, user_query, kb_id=request.knowledge_base_id,
                kb_ids=nonstream_msg_kb_ids, tenant_id=tenant_id,
                attachments=nonstream_attachments,
            )
        except Exception as e:
            logger.warning("入库用户消息失败: %s", e)

    # Agent 模式：跑 ReAct 引擎并直接采用其最终答案（与流式共用 _build_agent_runtime），
    # 不再二次走普通 RAG 生成（既省一次 LLM 调用，也保留 Agent 推理结果）。
    # 始终走 AGENT（含会话源由 session_has_files 决定），不再因会话文件降级（Property 2）。
    if route == Route.AGENT:
        answer, chunks, degraded, agent_steps, failed_source_ids = await _run_agent_nonstream(
            user_query, list(requested_kb_ids), llm, preset_cfg,
            max_context_tokens, thinking_enabled, tenant_id, request.session_id, history,
            timezone_name=request.timezone_name,
            include_session_source=session_has_files,
            attachments=nonstream_attachments,
            owner_user_id=identity.acting_subject_id,
            caller_ctx=caller_ctx,
            max_output_tokens=request.max_tokens,
        )
        references = await _build_references(chunks)
        prompt_tokens = _estimate_tokens(user_query)
        completion_tokens = _estimate_tokens(answer)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        # 会话源失败时 failed_source_ids 含 SESSION_FILES_KB_ID，经 _build_degraded_metadata
        # 派生 session_source_failed / kb_source_failed，让前端区分两类来源的检索失败。
        response = ChatCompletionResponse(
            id=completion_id,
            choices=[ChatChoice(message=ResponseMessage(content=answer))],
            usage=UsageInfo(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            references=references,
            metadata={
                "retrieval_mode": mode,
                "degraded": degraded,
                "llm_degraded": False,
                **_build_degraded_metadata(failed_source_ids),
            },
        )
        if request.session_id and answer:
            try:
                refs_data = [ref.model_dump() for ref in references] if references else None
                steps_data = agent_steps if agent_steps else None
                await _save_message(request.session_id, "assistant", answer, references=refs_data, agent_steps=steps_data, kb_id=request.knowledge_base_id, kb_ids=nonstream_msg_kb_ids, tenant_id=tenant_id)
                if is_first_round:
                    asyncio.create_task(_refine_session_title(request.session_id, user_query, answer))
            except Exception as e:
                logger.warning("保存会话消息失败: %s", e)
        return response

    if route == Route.CHITCHAT or route == Route.NONE:
        # 闲聊/纯历史追问或无检索源：不检索，直接基于历史让 LLM 作答
        chunks = []
    elif route == Route.MULTI_KB:
        # 多知识库联合检索（含"仅会话文件"场景，requested_kb_ids 可能为空列表）。
        try:
            # H3：完整接收 (results, degraded, failed_kb_ids)；失败源经 _build_degraded_metadata 透传前端。
            chunks, degraded, failed_kb_ids = await _retrieve_multi_kb(
                retrieval_query, list(requested_kb_ids), filter_obj, tenant_id=tenant_id,
                session_id=request.session_id,
            )
        except Exception as e:
            logger.error("多知识库联合检索失败: %s", sanitize_for_log(e))
            raise HTTPException(status_code=500, detail=f"多知识库联合检索失败: {e}")
    elif route == Route.SINGLE_KB:
        single_kb_id = requested_kb_ids[0]
        try:
            chunks, degraded = await _retrieve_chunks(retrieval_query, single_kb_id, mode, llm, expr=expr, tenant_id=tenant_id)
        except Exception as e:
            logger.error("检索失败: %s", sanitize_for_log(e))
            raise HTTPException(status_code=500, detail=f"检索失败: {e}")
    context = _build_context(chunks, max_tokens=max_context_tokens)
    has_context = len(chunks) > 0
    messages = _build_messages(
        request, context, has_context, history=history,
        timezone_name=request.timezone_name,
    )

    # 尝试 LLM 生成，失败时降级为纯检索结果
    llm_degraded = False
    llm_kwargs = {}
    if thinking_enabled:
        llm_kwargs["enable_thinking"] = True
    else:
        llm_kwargs["enable_thinking"] = False
    try:
        answer = await llm.generate(messages, **llm_kwargs)
    except Exception as e:
        logger.warning("LLM 生成失败，降级为纯检索结果: %s", e)
        answer = _build_context(chunks)
        llm_degraded = True

    # 计算 token 使用量
    prompt_text = " ".join(m["content"] for m in messages)
    prompt_tokens = _estimate_tokens(prompt_text)
    completion_tokens = _estimate_tokens(answer)

    # 构建响应
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    references = await _build_references(chunks)
    response = ChatCompletionResponse(
        id=completion_id,
        choices=[
            ChatChoice(
                message=ResponseMessage(content=answer),
            )
        ],
        usage=UsageInfo(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        references=references,
        metadata={
            "retrieval_mode": mode,
            "degraded": degraded or llm_degraded,
            "llm_degraded": llm_degraded,
            **_build_degraded_metadata(failed_kb_ids),
        },
    )

    # 保存助手消息到会话（用户消息已在生成前入库）
    if request.session_id and answer:
        try:
            refs_data = [ref.model_dump() for ref in references] if references else None
            await _save_message(request.session_id, "assistant", answer, references=refs_data, kb_id=request.knowledge_base_id, kb_ids=nonstream_msg_kb_ids, tenant_id=tenant_id)
            if is_first_round:
                asyncio.create_task(_refine_session_title(request.session_id, user_query, answer))
        except Exception as e:
            logger.warning("保存会话消息失败: %s", e)

    return response


@router.post("/api/retrieval/agent", response_model=AgentRetrievalResponse, tags=["Retrieval"])
async def retrieval_agent(
    request: AgentRetrievalRequest,
    identity: IdentityContext = Depends(require_authenticated()),
) -> AgentRetrievalResponse:
    """Agent 检索召回（对外开放）。

    与 ``/api/retrieval/search``（单轮召回）互补：本接口跑 ReAct Agent 引擎，围绕问题多步
    检索、反思、改写子查询后汇聚证据，返回其召回的引用来源（``references``）、最终作答
    （``answer``）与推理步骤（``agent_steps``）。复用问答链路的 ``_run_agent_nonstream``，
    召回口径（含图谱第四路）与 ``/v1/chat/completions`` 的 agent 模式完全一致。

    无会话：不落库、不加载历史、不接入会话临时文件；一次请求一个独立推理链。
    鉴权与检索范围授权与其余业务接口一致，可用代理 Key + ``X-External-User-Id`` 调用。
    """
    # 解析检索范围：kb_ids 优先，其次单选 knowledge_base_id；至少提供其一。
    requested_kb_ids: list[str] = []
    if request.kb_ids:
        requested_kb_ids = list(request.kb_ids)
    elif request.knowledge_base_id:
        requested_kb_ids = [request.knowledge_base_id]
    if not requested_kb_ids:
        raise HTTPException(status_code=400, detail="必须指定 knowledge_base_id 或 kb_ids")

    # 检索范围授权：触达 Milvus 前先校验所有被指定 KB 处于身份可读范围（跨租户/不可读 → 404）。
    async with async_session() as _authz_session:
        await authorize_requested_kbs(
            _authz_session, identity, requested_kb_ids, KbAccessEnum.READ
        )

    # 生效预设（指定 > 默认 > 内置兜底），须在调用者可见范围内。
    preset_cfg = await get_effective_preset_config(request.agent_preset_id, identity)
    llm, _stream_enabled, max_context_tokens = await _get_llm_for_request(request.model_config_id)
    thinking_enabled = bool(preset_cfg.get("thinking_enabled", False))

    start = time.time()
    answer, chunks, degraded, agent_steps, _failed = await _run_agent_nonstream(
        request.query,
        requested_kb_ids,
        llm,
        preset_cfg,
        max_context_tokens,
        thinking_enabled,
        identity.tenant_id,
        session_id=None,
        history=None,
        include_session_source=False,
        attachments=None,
        owner_user_id=identity.acting_subject_id,
        # 无会话链路：session_id 为空，其余主体字段照常透传（第三方仍能做用户级隔离）
        caller_ctx=ToolContext.from_identity(identity, request_id=uuid.uuid4().hex),
        max_output_tokens=request.max_tokens,
    )
    references = await _build_references(chunks)
    return AgentRetrievalResponse(
        query=request.query,
        answer=answer,
        references=references,
        agent_steps=agent_steps,
        degraded=degraded,
        elapsed_ms=int((time.time() - start) * 1000),
    )
