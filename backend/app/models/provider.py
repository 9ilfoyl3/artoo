"""模型 Provider 抽象基类

定义 LLM、Embedding、Rerank 三类模型的统一接口，
具体实现由各子模块提供（如 ollama、vllm、bge-m3 等）。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


# ============================================================
# Function Calling 数据结构
# ============================================================


@dataclass
class LLMToolCall:
    """LLM 返回的工具调用"""

    id: str
    function_name: str
    arguments: str  # JSON string


@dataclass
class TokenUsage:
    """Token 用量统计"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ChatResponse:
    """一次模型响应的完整、类型化内容"""

    content: str = ""
    reasoning_content: str = ""
    # 展示用推理：native reasoning 之外，还包含无 native thinking 模型在
    # tool-call 轮写出的普通 content（由引擎按 finish/tool calls 归类）。
    display_reasoning: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: TokenUsage | None = None
    # content 通道最终语义：text=用户正文；reasoning=本轮随后的 tool call 前规划。
    content_channel: str = "text"


@dataclass
class StreamChunk:
    """Provider 流式响应片段"""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[LLMToolCall] | None = None
    finish_reason: str = ""
    usage: TokenUsage | None = None
    response_type: str = "content"  # "content" | "thinking" | "tool_call"


# ============================================================
# Provider 抽象基类
# ============================================================


class LLMProvider(ABC):
    """大语言模型 Provider 抽象基类"""

    @abstractmethod
    async def generate(self, messages: list[dict], **kwargs) -> str:
        """同步生成完整回复"""
        ...

    @abstractmethod
    async def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """流式生成回复，逐 token 返回。

        Provider implementations may consume ``stream_idle_timeout`` from
        ``kwargs`` and must enforce it at the upstream transport boundary,
        because semantic output chunks are not a reliable liveness signal.
        """
        ...

    async def chat_with_tools(
        self, messages: list[dict], tools: list[dict], **kwargs
    ) -> ChatResponse:
        """Function Calling: 发送消息和工具定义，获取可能包含 tool_calls 的响应"""
        raise NotImplementedError("This provider does not support function calling")

    async def stream_with_tools(
        self, messages: list[dict], tools: list[dict], **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """Streaming Function Calling: 流式返回包含工具调用的响应片段。

        ``stream_idle_timeout`` is an upstream transport budget, not a total
        generation limit. Every raw provider event must reset it, including
        comments and tool-call deltas that do not become ``StreamChunk`` values.
        """
        raise NotImplementedError(
            "This provider does not support streaming function calling"
        )


class EmbedProvider(ABC):
    """向量嵌入模型 Provider 抽象基类"""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """生成稠密向量"""
        ...

    @abstractmethod
    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        """生成稀疏向量（用于 BM25 等稀疏检索）"""
        ...


class RerankProvider(ABC):
    """重排序模型 Provider 抽象基类"""

    @abstractmethod
    async def rerank(
        self, query: str, documents: list[str], top_k: int = 10
    ) -> list[tuple[int, float]]:
        """对候选文档重排序，返回 (原始索引, 分数) 列表"""
        ...
