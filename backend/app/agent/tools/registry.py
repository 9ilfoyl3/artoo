"""ToolRegistry - Agent 工具注册表

管理所有已注册的工具，提供 OpenAI function calling 格式的工具定义，
以及按名称查找和执行工具的能力。
- 执行后自动截断超长输出（TruncateToolOutput）
- 失败时追加 error hint 引导 LLM 换策略
"""

import logging

from app.agent.memory.context_manager import truncate_tool_output
from app.agent.tools.base import BaseTool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

# 工具执行失败时追加的提示，引导 LLM 换策略重试
_TOOL_ERROR_HINT = "\n\n[Analyze the error above and try a different approach.]"


class ToolRegistry:
    """工具注册表 - 注册、查找、执行工具"""

    def __init__(self, max_tool_output_chars: int = 16000) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._max_tool_output_chars = max_tool_output_chars

    def register(self, tool: BaseTool) -> None:
        """注册一个工具实例，以 tool.name 为键存储（first-wins 策略）"""
        if tool.name in self._tools:
            logger.warning(
                "[ToolRegistry] Duplicate tool registration rejected: %s (first-wins policy)",
                tool.name,
            )
            return
        self._tools[tool.name] = tool

    def get_function_definitions(self) -> list[dict]:
        """返回 OpenAI function calling 格式的 tools 列表

        格式:
        [
            {
                "type": "function",
                "function": {
                    "name": "...",
                    "description": "...",
                    "parameters": {...}
                }
            }
        ]

        返回前按工具名称（function.name）字母序稳定排序，保证相同工具集合
        多次调用产生字节级相同的 JSON 序列化结果，最大化 LLM API 的
        prompt prefix caching 命中率。
        """
        definitions = []
        for tool in self._tools.values():
            definitions.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            })
        # 按 function.name 字母序稳定排序，保证 JSON 字节级稳定（Prompt Caching）
        definitions.sort(key=lambda item: item["function"]["name"])
        return definitions

    async def execute(
        self, name: str, args: dict, ctx: ToolContext | None = None
    ) -> ToolResult:
        """按名称查找工具并执行

        执行后自动截断超长输出，防止上下文窗口溢出。
        失败时追加 error hint 引导 LLM 换策略。

        Args:
            name: 工具名称
            args: 工具参数字典
            ctx: 本次请求的调用方上下文（会话 / 租户 / 主体）。只传给声明了
                ``accepts_context = True`` 的工具——目前仅外部 MCP 工具需要，用于把
                调用方身份透传给第三方 server。走调用期参数而非工具实例属性，是因为
                MCP 工具实例跨请求缓存复用，写进实例会造成跨用户上下文污染。

        Returns:
            ToolResult: 执行结果，工具不存在时返回 success=False
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                success=False,
                error=f"Tool '{name}' not found" + _TOOL_ERROR_HINT,
            )

        if getattr(tool, "accepts_context", False):
            result = await tool.execute(args, ctx=ctx)
        else:
            result = await tool.execute(args)

        # 截断超长工具输出，防止上下文窗口溢出
        if result.output and len(result.output) > self._max_tool_output_chars:
            result = ToolResult(
                success=result.success,
                output=truncate_tool_output(result.output, self._max_tool_output_chars),
                data=result.data,
                error=result.error,
                structured_content=result.structured_content,
            )

        # 失败时追加 error hint 引导 LLM 换策略
        if not result.success and result.error and _TOOL_ERROR_HINT not in result.error:
            result = ToolResult(
                success=result.success,
                output=result.output,
                data=result.data,
                error=result.error + _TOOL_ERROR_HINT,
                structured_content=result.structured_content,
            )

        return result

    def list_tools(self) -> list[str]:
        """返回所有已注册工具的名称列表"""
        return list(self._tools.keys())
