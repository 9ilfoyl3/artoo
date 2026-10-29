# Agent Note: Replayable tool-call arguments

Status: implemented

## Problem

Agent loop 会拒绝模型生成的不完整或截断工具参数，并给模型返回“未执行”的观察结果，
但仍把 provider 返回的原始参数片段原样写入 assistant 消息。下一轮请求中，vLLM 在渲染
chat template 时用 `json.loads()` 解析历史
`assistant.tool_calls[].function.arguments`，于是返回 HTTP 400。该 turn 随后退化为基于
已有观察合成答案，而不是让模型重试失败的工具调用。

当历史 `agent_steps` 中保存的是字符串参数片段、而不是运行时通常写入的规范化对象时，
也存在同样风险。

## Decision

所有回放到 assistant 历史中的工具调用都经过
`canonicalize_tool_arguments_for_replay`。合法 JSON object 会被规范化为紧凑 JSON；
非法、截断或非 object 参数只在历史消息中替换为 `{}`。紧随其后的 tool observation 仍
记录原始失败，并指示模型以更小的调用重试；工具执行仍然被阻止。

规范化同时应用于实时 Agent loop 和从持久化 `agent_steps` 重建历史。事件载荷与
`ToolCallRecord.args` 保持现有的规范化 object 行为不变。

## Alternatives considered

**从模型上下文中丢弃非法工具调用及其对应工具观察。** 拒绝：只删除工具调用交换的一部分
可能留下孤立的 tool 消息，并形成更复杂的第二种协议形状。把 `arguments` 替换为 `{}`
可以保留完整的调用/结果配对，并且对 OpenAI 兼容服务保持合法。

**回放本地修复后的部分 object。** 拒绝：`truncated=True` 表示模型可能已经丢失某个值的
边界。把部分参数展示为已接受状态可能误导重试，或暗示副作用已经发生。

**要求 vLLM 接受非法的历史 JSON。** 拒绝：OpenAI 工具协议要求
`function.arguments` 是 JSON 字符串，vLLM 的校验是合理的服务端检查。Artoo 不应发送
自己已经知道非法的请求。

**工具参数非法后立即终止 turn。** 拒绝：该失败通常可以通过要求模型改用更小或形状不同
的调用恢复。现有有界 Agent loop 和工具观察提供了这条恢复路径。

## Consequences

非法工具调用不再让下一次模型请求变成 vLLM HTTP 400。模型会看到历史参数值 `{}`，以及
明确的 incomplete-call 观察，因此可以重试，而平台不会凭空补写缺失字段。

历史表示会有意规范化为协议安全形式，而不是与 provider 响应逐字节相同。该处理仅限协议
回放，不改变公开 SSE 工具调用参数、持久化工具结果或授权语义。

## Testing

`backend/tests/test_unified_knowledge_agent_core.py` 覆盖合法、截断和非法参数的规范化，
被拒绝工具调用后的实时 loop 回放，以及从非法字符串参数重建历史。
