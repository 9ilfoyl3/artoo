# Agent Note: Visible tool-call arguments

Status: implemented

## Problem

工具调用参数链路本身是完整的：provider 流式累积 `arguments`，Agent 引擎将其解析为
object，`tool_call.arguments` 经 SSE 和持久化 `agent_steps` 传递，Chat 客户端也保存为
`toolArgs`。但展开后的工具行没有渲染该字段，用户无法区分合法的空对象和参数丢失。
`list_documents` 尤其容易产生歧义，因为它的 `page`、`page_size`、`status`、`keyword`
与 `kb_ids` 参数全部可选。

## Decision

展开后的工具调用行现在显示“调用参数”区域，以格式化 JSON 展示规范化参数对象；模型未提供
参数时明确显示 `{}`。当前与 legacy transcript renderer 共用同一个
`ToolCallArguments` 组件。

UI 展示的是运行时实际执行的解析后 object，而不是 provider 原始 JSON 片段。非法或截断的
原始片段仍由现有拒绝结果与规范化回放行为表示；事件和持久化契约不变。

## Alternatives considered

**在所有工具调用行的折叠态直接内联参数。** 拒绝：检索 query、文档 ID 和较大的 MCP 载荷
会增加噪声，并挤占工具名、状态和文件结果摘要的空间。

**展示 provider 原始参数字符串。** 拒绝：provider 片段在本地解析前可能已经损坏，而规范化
object 才是实际校验并执行的调用。传输损坏诊断应留在日志层，而不是在常规 transcript 中混用
两种表示。

**要求用户检查 `agent_steps` 或服务端日志。** 拒绝：这些信息已经存在于浏览器并会持久化用于
回放。一个可折叠诊断视图已经足够，不需要新增端点或 debug mode。

## Consequences

用户可以核对 Agent 实际执行了哪个参数对象，包括可选参数工具显式传入的空参数。信息仍位于
现有行展开之后，因此正常 transcript 密度不变。

工具参数可能包含用户输入的 query 文本或文档标识。它们只展示给本来就能访问会话及其持久化
`agent_steps` 的同一批已认证 transcript 查看者；SSE 契约不会额外暴露数据。

## Testing

`backend/tests/test_unified_knowledge_agent_core.py` 验证非空参数 object 会经过引擎事件、SSE
转换、持久化步骤记录和 assistant 历史回放而不丢失。
`frontend/src/components/chat/MessageBubble.test.tsx` 验证展开后的参数 JSON，以及可选参数
省略时显式显示 `{}`。
