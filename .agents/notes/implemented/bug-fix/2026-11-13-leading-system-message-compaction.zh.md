# Agent Note: Leading system message after context compaction

Status: implemented

## Problem

记忆压缩把压缩后的历史表示为原始 system prompt 之后的第二条 `system` 消息。
vLLM 等 OpenAI 兼容 provider 要求 `system` 只能出现在会话开头，因此下一次模型调用
会因为 `System message must be at the beginning.` 失败。部分 agent compaction
checkpoint 已经持久化了这种形状，所以只修改 consolidator 无法修复已有长会话。

## Decision

所有 system 消息统一折叠为一条位于开头的 system 消息。压缩摘要追加到原始 system
prompt 中，在保留摘要优先级的同时保证索引零处是唯一的 `system` role。归一化既在
consolidation 构造结果时执行，也在每次 Agent 模型调用前执行，因此旧 checkpoint 会在
下一轮被修复。

## Alternatives considered

**把摘要改成 `user` 消息。** 否决，因为历史模型输出会获得新的用户级指令权限，可能与
当前请求冲突。

**只修 consolidator 输出。** 否决，因为旧行为写入的 checkpoint 在恢复原始 prompt 后
仍可能包含第二条 system 消息。

**在 vLLM adapter 内归一化。** 否决，因为 leading-system 约束是 OpenAI 兼容 provider
共同遵守的协议；provider 专用绕过需要重复实现，且其他 provider 路径仍会暴露。

## Consequences

压缩后的会话会同时保留摘要并满足 provider 合约。首条 system prompt 会增加摘要文本，
所有非 system 消息顺序保持不变。若存在多条 system 消息，后续 system 消息的内容会追加
到第一条，其独立消息元数据会被丢弃。公开 API 与 SSE 事件合约不变。

## Testing

`backend/tests/test_agent_harness_p0_p3.py` 验证 consolidation 只产生一条开头的
system 消息、保留摘要内容和当前轮，并验证旧版重复 system 历史可被归一化且不修改输入，
以及 Agent 在调用 provider 前修复重复 system 历史。
