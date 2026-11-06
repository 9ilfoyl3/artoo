# Agent Note: Deterministic Agent harness P0-P3 policies

Status: implemented

## Problem

ReAct loop 会接受完全重复的工具调用，检索工具会接受重复或无上限的 query 列表，低质量检索没有有界改写路径，历史工具输出在进入压缩前没有确定性裁剪，Agent 路径也绕过了现有检索缓存。动态时钟文本仍留在稳定 system prompt 中，导致多轮请求无法复用前缀。

这些是 harness 问题，不是模型能力问题。继续依赖 prompt 文字会让行为依赖模型遵从度，并增加 token 消耗，却无法保证进展。

## Decision

Agent harness 负责四层确定性策略。

P0 loop 卫生与身份：

- 检索 query 使用 Unicode NFKC 和空白折叠规范化。
- query 列表去重并保持首次出现顺序。
- `knowledge_search` 与 `grep_chunks` 共用 `seen_chunk_ids`，并从模型可见 XML 中省略重复 chunk 正文。
- 完全相同且连续的 tool call 使用 canonical 参数签名跟踪。第 3、5、8 次注入 advisory reminder，绝不阻断调用。

P1 有界查询修复：

- 单次 `knowledge_search` 最多接受三个唯一 query。
- 零结果或零新增 chunk 时，在工具元数据中设置 `suggest_rewrite`。
- engine 每轮最多给模型一次明确改写机会。

P2 上下文经济与 reasoning effort：

- 上下文达到裁剪阈值时，先用 4096 头部字符、裁剪标记和 1024 尾部字符确定性缩短旧工具结果，再考虑 LLM 摘要。
- 本项裁剪永不触碰当前轮。
- 可选 `reasoning_effort` 只在预设显式配置时透传，不支持该字段的 provider 保持原请求形态。

P3 检索与前缀缓存：

- 正式知识库检索复用现有 Redis 缓存，key 包含 query、mode、scope、top-k、tenant 和可选 config version。
- 失败结果与路级降级结果不缓存。
- 在拥有独立 generation key 之前，会话文件源永不缓存，因为其内容变化不一定触发 KB 级失效。
- 动态时钟追加到当前用户轮，而不是稳定 system prompt，与单轮输出语言契约保持一致。

所有策略由 `AgentConfig` 开关表示，除可选 reasoning effort 外默认开启。这样无需修改公开 API 或 SSE payload 即可做确定性 A/B。

## Alternatives considered

**硬停止重复工具。** 已否决，因为部分重复调用是合法的；更合理的做法是先给模型可见建议，再依据证据决定是否升级 guard。

**始终摘要工具输出。** 已否决，因为每步额外调用模型成本高，而且会比必要时机更早改写可复用历史。确定性裁剪只在实际压力出现时运行。

**让会话文件检索与正式 KB 共用缓存 key。** 已否决，因为会话上传物独立于 KB 失效机制变化，会产生过期回答。

**增加语义检索缓存。** 已否决，因为相似 query 可能带有不同过滤器、意图或时点要求。当前阶段只有规范化精确 key 是安全的。

**继续把时间放在稳定 system prompt。** 已否决，因为每轮都会改变前缀。时钟本来就是单轮事实，应放在可复用历史之后。

## Consequences

Agent loop 会减少完全重复调用和重复 query 带来的浪费，在考虑 LLM 摘要前先裁剪工具结果体积，并复用正式 KB 的缓存结果，同时不缓存可变会话数据。system prompt 前缀不再仅因时间或输出语言变化而失效。

缓存继承现有 Redis TTL 和 KB 失效生命周期。若未提供 config version，检索配置变化可能缓存到 TTL 到期。reasoning 语言仍是尽力而为，重复工具 guard 也是 advisory，不是硬执行上限。Open API、SSE、持久化事件和存储 schema 均不变。

## Testing

`backend/tests/test_agent_harness_p0_p3.py` 覆盖 query 规范化、canonical 签名、重复提醒、上下文裁剪、正式 KB 精确缓存命中、缓存 key 隔离和会话源缓存排除。

`backend/benchmarks/agent_harness_ab.py` 输出确定性 A/B 指标。当前 fixture 结果为：去重后 query 从 6 降到 3；两个检索目标下，逻辑底层检索单元从 12 降到 6；连续调用第 3 次出现首个提醒；两次相同缓存检索从 2 次底层检索降到 1 次；六个历史工具结果裁剪后，估算上下文 token 从 9,114 降到 4,014。基线会跨过摘要阈值，而裁剪后不会，因此避免一次 LLM 摘要调用。
