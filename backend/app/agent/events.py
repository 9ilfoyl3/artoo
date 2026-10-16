"""EventBus 事件系统 - 定义事件类型、数据结构和事件总线"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """Agent 事件类型枚举"""

    REASONING_DELTA = "reasoning_delta"
    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"
    TOOL_PROGRESS = "tool_progress"
    TOOL_RESULT = "tool_result"
    TODO_UPDATE = "todo_update"
    TOKEN_USAGE = "token_usage"  # 每轮 LLM 调用后的 token 用量事件，供前端上下文进度条消费
    TURN_END = "turn_end"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class AgentEvent:
    """Agent 事件数据结构"""

    type: EventType
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)
    done: bool = False


# 事件处理器类型别名：接收 AgentEvent，返回 Awaitable[None]
EventHandler = Callable[[AgentEvent], Awaitable[None]]


class EventBus:
    """事件总线 - 同步发射事件，保证顺序"""

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[EventHandler]] = {}
        self._global_handlers: list[EventHandler] = []

    def on(self, event_type: EventType | None, handler: EventHandler) -> None:
        """注册事件处理器

        Args:
            event_type: 事件类型，None 表示监听所有事件
            handler: 异步事件处理函数
        """
        if event_type is None:
            self._global_handlers.append(handler)
        else:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            self._handlers[event_type].append(handler)

    async def emit(self, event: AgentEvent) -> None:
        """发射事件，按注册顺序调用所有匹配的 handler

        先调用特定类型的 handler，再调用全局 handler。
        """
        # 先调用特定类型的 handler
        handlers = self._handlers.get(event.type, [])
        for handler in handlers:
            await handler(event)
        # 再调用全局 handler
        for handler in self._global_handlers:
            await handler(event)
