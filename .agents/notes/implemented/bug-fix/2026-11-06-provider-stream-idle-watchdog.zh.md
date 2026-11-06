# Agent Note: Provider-owned stream idle watchdog

Status: implemented

## Problem

Agent loop 把 stream-stall timeout 加在类型化 `StreamChunk` 上，但这不是真正的
provider transport 边界：vLLM 在内部累积 tool-call delta，SSE comment 也会被跳过，
因此这两种上游活动都可能让 Agent 收不到 chunk。健康的长时间工具调用生成因此会被
误判为 provider 静默，进入重试并最终失败。

## Decision

流空闲计时改由每个流式 provider 负责。共享的
`iter_with_stream_idle_timeout` 只统计等待原始 provider 行迭代器的未完成读取；每返回
一行都会重新计时，包括不会变成 Agent-facing chunk 的 SSE comment 和 tool-call
delta。vLLM 与 Ollama 的普通流式调用和工具流式调用都使用该 helper。

默认空闲预算是 300 秒。`AgentConfig.llm_stall_timeout` 作为
`stream_idle_timeout` 传给 provider；新预设键 `llm_stream_idle_timeout` 优先，旧的
`llm_stall_timeout` 仍兼容。Agent loop 不再对每个 chunk 应用超时，因此下游处理时间
不会被误判为 provider 无活动。

## Alternatives considered

**在 vLLM adapter 中发送合成的 activity chunk。** 否决作为长期设计，因为它把传输层
活性混入语义流事件，而且每个 provider 都要重复实现。它只能作为紧急缓解，不应拥有
超时判定。

**保留 Agent watchdog 同时增加 provider watchdog。** 否决，因为外层计时仍会在
provider 消费 tool-call delta 但不 yield chunk 时触发。

**只把 `data:` 事件算作活动。** 否决，因为代理可能使用 SSE comment 作为 keep-alive；
计时器属于原始传输读取，而不是单一种协议载荷形态。

**把空闲预算应用于整个生成过程。** 否决，因为只要上游持续活动，长响应就是健康的；
只有上游读取缺失才应消耗该 deadline。

## Consequences

长时间 tool-call 生成不再触发误报。真正静默的传输仍会抛出带 code
`LLM_STREAM_IDLE_TIMEOUT`、预算和最后观测语义阶段的 `LlmStreamIdleTimeout`。该
超时也覆盖 vLLM 的非流式降级请求。

provider 负责传输活性，Agent loop 保留编排职责。300 秒默认值有意保持可配置，因为
本地模型的 prefill 延迟随部署环境变化。

## Testing

`backend/tests/test_llm_stream_idle_watchdog.py` 验证 300 秒默认值、tool-call delta
与 SSE comment 会刷新 deadline，以及真正静默的传输会产生 stage 为
`tool_call_generation` 的类型化超时。
`backend/tests/test_unified_knowledge_agent_core.py` 验证 Agent 不再应用语义 chunk
超时，而是信任 provider 边界。
