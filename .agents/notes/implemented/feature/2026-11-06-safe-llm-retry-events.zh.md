# Agent Note: Safe LLM retry events and structured failure settlement

Status: implemented

## Problem

LLM 临时失败此前会在没有任何用户可见状态的情况下重试。重试资格依赖对渲染后错误
消息的子串匹配，而且 loop 可能在 reasoning 已经发给客户端后继续重试，导致多次
尝试重复展示 reasoning。若工具已经执行后的后续重试仍失败，引擎会调用
`_synthesize_answer`，用第二次模型请求掩盖 transport failure。

## Decision

LLM 失败在重试前先归类为稳定事实。每次自动 backoff 前都会发出 `llm_retry` Agent
事件，携带失败 attempt、下一次 attempt、最大 attempt、delay、code、stage 和
retryable 标记。该事件会写入 `agent_steps`，并投影到 Agent SSE。

自动重试仍限制为总共 3 次尝试，使用指数 backoff 与 jitter。只有失败尝试没有产生
可见 reasoning/text，也没有形成完整 tool call 时才允许重试。不可重试失败、已有可见
输出的尝试，以及已经形成 tool call 的尝试都会直接失败，不再发起另一个 provider
请求。

最终的模型失败会发送带 code、stage、retryable 和 attempts 的结构化 `error` 事件，
随后发送 `turn_end(finish_reason="error")`。引擎不再在 transport failure 时调用
`_synthesize_answer`。用户可见错误文本仍是保存的 assistant body，安全的部分输出由
其已持久化的 stream event 表示。

Web 客户端把重试尝试和最终失败渲染为独立状态行。历史回放从
`agent_steps` 还原相同状态行，因此刷新或切换会话不会丢失重试历史。旧客户端可以
忽略新事件和可选 error 字段。

## Alternatives considered

**在部分生成后重试并替换前一次 attempt。** 当前否决，因为公开 SSE 和持久化
`agent_steps` 契约没有 attempt revision 或客户端回滚机制；没有该机制的重试会重复
展示可见内容。

**对所有失败 code 都重试。** 否决，因为认证、非法请求、上下文超限和其他确定性失败
无法通过重复同一请求恢复。

**保留基于子串的重试分类。** 否决，因为 provider 文案不是稳定契约，也可能把 HTTP
状态文本与无关数字混淆。

**transport failure 后继续合成答案。** 否决，因为第二次模型请求可能以相同方式失败，
会掩盖真实错误并增加成本与延迟。synthesis 只保留给 max iterations 等明确的 loop
终止场景。

**只在日志中发送 retry 事件。** 否决，因为用户只会看到停滞的 assistant 消息而没有
解释，前端也无法区分正常等待与自动重试。

## Consequences

Agent SSE 和持久化 `agent_steps` 新增 `llm_retry`；`error` 新增可选结构化字段。前端
按流顺序渲染重试和失败状态。

重试更安全但不再激进：已有可见输出后的失败会交给用户显式重试，而不是自动重试。这
避免重复 reasoning 和意外的重复副作用。

## Testing

`backend/tests/test_llm_retry_events.py` 覆盖 retry event 的发送与顺序、可见输出后的
重试抑制、结构化 SSE 投影、不可重试 HTTP 分类，以及重试耗尽后不再执行 tool 后
synthesis。
`frontend/src/components/chat/MessageBubble.test.tsx` 验证 retry 与最终 error 行会
带上 attempt 和稳定失败元数据渲染。
