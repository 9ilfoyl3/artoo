"""API 请求/响应 Pydantic 模型

定义 Chat API 的 OpenAI 兼容请求和响应结构。
"""

from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, Field


# ============================================================
# 通用分页响应模型
# ============================================================

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    """统一分页响应结构

    用于知识库、文件夹、文档等列表接口的滚动加载（infinite scroll）。
    """

    items: list[T] = Field(default_factory=list, description="当前页数据列表")
    total: int = Field(default=0, description="总记录数")
    page: int = Field(default=1, description="当前页码，从 1 开始")
    page_size: int = Field(default=20, description="每页数量")
    has_more: bool = Field(default=False, description="是否还有下一页")


# ============================================================
# Chat API 请求模型
# ============================================================


class ChatMessage(BaseModel):
    """对话消息"""

    role: str = Field(..., description="角色: system / user / assistant")
    content: str = Field(..., description="消息内容")


class MessageAttachment(BaseModel):
    """用户消息携带的会话文件附件（发送时绑定的已上传文件快照）。"""

    file_id: str = Field(..., description="会话文件 ID（= SessionFile.id）")
    filename: str = Field(..., description="原始文件名")
    file_size: Optional[int] = Field(default=None, description="文件字节数")
    file_type: Optional[str] = Field(default=None, description="文件类型扩展名（小写，无点）")


class ChatCompletionRequest(BaseModel):
    """Chat Completion 请求体（OpenAI 兼容 + RAG 扩展字段）"""

    model: str = Field(default="rag", description="模型名称")
    messages: list[ChatMessage] = Field(..., description="对话消息列表")
    stream: bool = Field(default=False, description="是否流式返回")
    knowledge_base_id: Optional[str] = Field(default=None, description="知识库 ID，为空时使用全部知识库")
    retrieval_mode: Optional[str] = Field(
        default=None, description="检索模式: direct / hybrid / agent，为空时使用 Agent 预设配置"
    )
    model_config_id: Optional[str] = Field(
        default=None, description="LLM 模型配置 ID，为空时使用系统默认模型"
    )
    max_tokens: Optional[int] = Field(default=None, description="单次生成最大 token 数")
    agent_preset_id: Optional[str] = Field(
        default=None, description="Agent 预设 ID，为空时使用默认预设"
    )
    filter_doc_ids: Optional[list[str]] = Field(
        default=None, description="限定文档范围过滤，仅在指定文档中检索"
    )
    kb_ids: Optional[list[str]] = Field(
        default=None, description="多知识库联合检索，指定多个知识库 ID 列表"
    )
    session_id: Optional[str] = Field(
        default=None, description="会话 ID，传入后自动加载历史上下文并保存消息"
    )
    attachments: Optional[list[MessageAttachment]] = Field(
        default=None,
        description="本次用户消息绑定的会话文件附件（发送时从已上传文件中选取），随用户消息存入历史",
    )
    timezone_name: Optional[str] = Field(
        default=None,
        description="调用方 IANA 时区名称（如 Asia/Shanghai），用于回答当前日期/时间",
    )
    temperature: Optional[float] = Field(default=None, description="生成温度")


# ============================================================
# Chat API 响应模型
# ============================================================


class UsageInfo(BaseModel):
    """Token 使用量统计"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ReferenceItem(BaseModel):
    """引用来源"""

    doc_id: str
    chunk_id: str
    filename: str = ""
    # 命中来源类型，供调用方选对原件预览接口：
    #   "knowledge_base" → 知识库文档，原件走 /api/documents/{doc_id}/raw
    #   "session"        → 会话附件，原件走 /api/sessions/{session_id}/files/{doc_id}/raw
    source_type: str = "knowledge_base"
    content: str
    child_content: str = ""
    score: float


class AgentRetrievalRequest(BaseModel):
    """Agent 检索召回请求（对外开放）。

    区别于 ``/api/retrieval/search`` 的单轮召回：Agent 会围绕 ``query`` 多步检索、反思、
    改写子查询后汇聚证据，返回其召回的引用来源（``references``）与最终作答（``answer``）。
    无会话概念（不落库、不加载历史），一次请求一个独立的推理链。
    """

    query: str = Field(..., min_length=1, description="查询文本")
    knowledge_base_id: Optional[str] = Field(
        default=None, description="单知识库 ID（与 kb_ids 二选一，至少提供其一）"
    )
    kb_ids: Optional[list[str]] = Field(
        default=None, description="多知识库联合检索的知识库 ID 列表"
    )
    agent_preset_id: Optional[str] = Field(
        default=None, description="Agent 预设 ID，为空时使用默认预设"
    )
    model_config_id: Optional[str] = Field(
        default=None, description="LLM 模型配置 ID，为空时使用系统默认模型"
    )


class AgentRetrievalResponse(BaseModel):
    """Agent 检索召回响应。"""

    query: str
    answer: str = ""
    references: list["ReferenceItem"] = Field(default_factory=list)
    agent_steps: list[dict] = Field(
        default_factory=list, description="Agent 推理/工具调用步骤，供还原召回过程"
    )
    degraded: bool = False
    elapsed_ms: int = 0


class ResponseMessage(BaseModel):
    """响应消息"""

    role: str = "assistant"
    content: str = ""


class ChatChoice(BaseModel):
    """响应选项"""

    index: int = 0
    message: ResponseMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    """Chat Completion 非流式响应（OpenAI 兼容 + RAG 扩展）"""

    id: str
    object: str = "chat.completion"
    choices: list[ChatChoice]
    usage: UsageInfo
    references: list[ReferenceItem] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


# ============================================================
# SSE 流式响应中的 delta 结构
# ============================================================


class DeltaContent(BaseModel):
    """流式 delta 内容"""

    role: Optional[str] = None
    content: Optional[str] = None


class StreamChoice(BaseModel):
    """流式响应选项"""

    index: int = 0
    delta: DeltaContent
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    """流式响应单个 chunk"""

    id: str
    object: str = "chat.completion.chunk"
    choices: list[StreamChoice]
