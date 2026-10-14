"""BaseTool 抽象接口、ToolResult 数据结构与 ToolContext 调用期上下文"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolResult:
    """工具执行结果"""

    success: bool
    output: str = ""
    data: dict[str, Any] | None = None
    error: str = ""
    # 仅供宿主/前端渲染的通用结构化结果；原始 output 仍只进入模型上下文。
    structured_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolContext:
    """一次工具调用的调用方上下文（**每请求构造，禁止跨请求复用**）。

    为什么需要它：外部 MCP 工具要把"是谁、在哪个会话里"带给第三方 server，而工具实例
    本身是跨请求缓存复用的（见 ``mcp_client._MCPToolCache``）。若把 session_id 写进工具
    实例属性，会造成跨请求、跨用户的上下文污染。因此上下文只走**调用期参数**：
    ``ToolRegistry.execute(name, args, ctx)`` -> ``BaseTool.execute(args, ctx)``。

    字段与 MCP 线上格式（:class:`app.mcp.context.CallerContext`）一一对应，转换在
    ``mcp_client`` 一处完成；Agent 层因此不依赖 MCP 协议层。
    """

    session_id: str | None = None
    tenant_id: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    external_user_id: str | None = None
    api_key_id: str | None = None
    request_id: str | None = None

    @classmethod
    def from_identity(
        cls,
        identity: Any,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> "ToolContext":
        """从 ``IdentityContext`` 构造。

        用 ``getattr`` 取字段而非强类型依赖，既避免 Agent 层反向依赖鉴权实现，也让
        测试可以传轻量替身。``subject_id`` 统一为"行事主体"：注册用户取 user_id、
        外部用户取 external_user_id，第三方只读这一个字段即可做用户级隔离。
        """
        from app.mcp.context import (
            SUBJECT_TYPE_EXTERNAL_USER,
            SUBJECT_TYPE_MACHINE,
            SUBJECT_TYPE_USER,
        )

        if identity is None:
            return cls(session_id=session_id or None, request_id=request_id or None)

        external_user_id = getattr(identity, "external_user_id", None)
        user_id = getattr(identity, "user_id", None)
        if external_user_id:
            subject_type, subject_id = SUBJECT_TYPE_EXTERNAL_USER, external_user_id
        elif user_id:
            subject_type, subject_id = SUBJECT_TYPE_USER, user_id
        else:
            # 租户级 Key：机器身份，无自然人主体
            subject_type, subject_id = SUBJECT_TYPE_MACHINE, None

        return cls(
            session_id=session_id or None,
            tenant_id=getattr(identity, "tenant_id", None),
            subject_type=subject_type,
            subject_id=subject_id,
            external_user_id=external_user_id,
            api_key_id=getattr(identity, "api_key_id", None),
            request_id=request_id or None,
        )


class BaseTool(ABC):
    """工具基类 - 所有 Agent 工具必须继承此类"""

    # 是否需要调用期上下文。默认 False：绝大多数内置工具在构造时就已锚定所需的
    # 请求信息（kb_ids / session_id 等），无需再接 ctx。仅外部 MCP 工具置 True，
    # ToolRegistry 据此决定调用 execute(args) 还是 execute(args, ctx=...)。
    accepts_context: bool = False

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，用于注册和调用"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述，展示给 LLM 用于决策"""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict:
        """工具参数的 JSON Schema 定义"""
        ...

    @abstractmethod
    async def execute(self, args: dict) -> ToolResult:
        """执行工具逻辑，返回 ToolResult

        声明了 ``accepts_context = True`` 的工具改用
        ``async def execute(self, args: dict, ctx: ToolContext | None = None)``。
        """
        ...
