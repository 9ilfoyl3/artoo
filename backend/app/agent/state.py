"""Agent 状态数据结构：AgentState / AgentStep / ToolCallRecord"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agent.tools.base import ToolResult
    from app.retrieval.base import RetrievalResult


@dataclass
class ToolCallRecord:
    """单次工具调用记录"""

    id: str
    name: str
    args: dict[str, Any]
    result: ToolResult | None = None
    duration_ms: int = 0


@dataclass
class AgentStep:
    """Agent 单轮执行步骤"""

    iteration: int
    reasoning: str = ""
    content: str = ""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class AgentState:
    """Agent 执行状态"""

    current_round: int = 0
    steps: list[AgentStep] = field(default_factory=list)
    is_complete: bool = False
    final_answer: str = ""
    knowledge_refs: list[RetrievalResult] = field(default_factory=list)
    seen_chunk_ids: set[str] = field(default_factory=set)
    query_rewrite_attempts: int = 0
    # 检索是否发生降级（H3）：任一次 knowledge_search 工具调用出现源失败或三路路级降级时
    # 由工具置 True（只增不减），供 chat 层据此填充 SSE meta 的真实 degraded（不再恒 False）。
    degraded: bool = False
    # 失败的检索源 kb_id 集合（agent-session-source-unification）：某源检索抛异常或路级降级时
    # 由 knowledge_search 工具记入其 kb_id（含会话源的 SESSION_FILES_KB_ID）。chat 层据此派生
    # failed_kb_ids / session_source_failed 元数据，让前端区分"会话文件检索失败"与"知识库检索失败"。
    failed_source_ids: set[str] = field(default_factory=set)
