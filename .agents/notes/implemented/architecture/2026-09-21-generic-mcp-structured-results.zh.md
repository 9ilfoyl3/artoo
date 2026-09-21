# Agent Note: Generic MCP structured result channel

Status: implemented

## Problem

此前 Artoo 会把 outbound MCP `tools/call` 的结果统一压平为
`ToolResult.output`。面向模型的 `content[].text` 会进入 LLM 上下文，但标准 MCP
`structuredContent` 与 `tools/list` 声明的 `outputSchema` 都被丢弃。因此工具专属
前端无法渲染安全的结构化卡片，实时 `tool_result` 与历史回放也没有统一的 UI 数据通道。

通过 SSE 直接返回原始工具文本不是可接受的修复。外部 MCP 输出不可信，可能包含提示注入
或超大载荷，而且原本就被明确排除在 SSE 与 `agent_steps` 持久化之外。

## Decision

Artoo 提供通用的 MCP 结构化结果通道，但不解释任何业务字段。

- `MCPRemoteClient.call_tool` 返回结构化的 `MCPCallResult`，分别承载模型文本、
  可选 `structuredContent` 和错误标记。
- `tools/list` 会把校验后的 MCP `outputSchema` 保存在
  `MCPToolWrapper` 上。该 schema 只用于本地校验，不会注入模型的 function definitions。
- 远端 `structuredContent` 必须使用通用 envelope：
  `{"kind": "<globally-namespaced-id>", "data": <JSON>}`。`kind` 是对 Artoo 不透明的
  renderer key，例如 `io.law-agent-lite.legal-provision-search.v1`。
- Artoo 会校验 JSON 兼容性、`outputSchema`、64 KiB 字节上限、最大深度 8、最多 100 个数组项、
  16 KiB 字符串、object key 上限和禁止的 prototype pollution 字段。结构化数据不合法时只丢弃该数据，
  模型仍会收到 `content[].text`。
- 合法结构化数据经 `ToolResult.structured_content`、SSE
  `tool_result.structured_content` 和 `agent_steps` 中同一事件流转。原始工具文本仍不进入 SSE
  或持久化。
- 内置前端维护 `kind` 到 renderer 的注册表。未知或非法 `kind` 回退到安全的纯 JSON 卡片。
  工具专属 renderer 放在消费方应用，不放入 Artoo 核心。
- 结构化内容不会自动合并进 `references`。引用身份仍由独立的通用契约负责。

## Alternatives considered

**通过 `tool_result` 暴露原始 MCP `content[].text`。** 已拒绝。这会把不可信第三方文本变成公开且
持久化的 API 载荷，增加历史存储，并要求每个客户端自行处理提示注入与渲染风险。

**在 Artoo 侧解析 `search_legal_provisions` 等已知工具。** 已拒绝。这会让核心平台耦合单一业务域，
并导致每新增一种 renderer 就要增加后端分支。

**只在 `content[].text` 中承载 JSON envelope。** 已拒绝作为主契约。它无法与模型文本投影独立校验，
而且 prompt 或文本整形一变化就可能破坏 UI。该方案仅保留为可能的旧版兼容退路。

**自动把 structured content 转成 `references`。** 本次已拒绝。结构化 UI 数据不定义文档/chunk
授权或引用身份，把任意 MCP 数据静默当作证据会削弱可溯源性。

## Consequences

需要丰富宿主渲染的 MCP server 必须显式声明 `outputSchema` 并返回 `structuredContent`；旧 server
继续以纯文本模式工作。SSE 与 `agent_steps` 契约只新增可选的 `structured_content` 字段，旧客户端
可以忽略。

Artoo 新增 `jsonschema` 依赖，用于通用远端 schema 校验。非法或超限的 schema/result 会在不记录内容的
前提下记日志，并降级为既有文本行为。前端注册表必须把结构化数据当作不可信输入，以纯文本而非 HTML
渲染，并为未知 `kind` 提供 fallback。

## Testing

后端测试覆盖 output schema 保留、结构化结果成功透传、schema 不匹配与超限拒绝、文本截断时保留结构化字段、
engine 事件发射和 typed SSE 序列化。前端测试覆盖 envelope 防护、renderer 注册、summary 选择、
非法 key 和未知 `kind` fallback。前端生产构建验证 React 类型与 import 集成。
