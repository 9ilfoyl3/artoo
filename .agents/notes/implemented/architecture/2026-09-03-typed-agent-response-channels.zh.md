# Agent Note: 类型化 Agent 响应通道与自然停止 ReAct

Status: implemented

[English](2026-09-03-typed-agent-response-channels.md) | 中文

## 问题

旧 Agent 协议要求模型通过 `final_answer` 工具提交可见回答，并通过 `thinking` 工具提交推理。Provider 适配器从增量流式的 tool-call JSON 中抽取这些字段，前端则根据是否处于工具调用状态来判断普通文本是思考还是答案。这个设计让回答 payload 成为工具调用协议内部的第二种协议格式。

这种耦合有多种失败方式：工具参数被截断后可能得到无法解析或不完整的回答；弱 function-calling 模型会忘记调用 `final_answer`、提前输出答案式文本，或在 thinking 和 final answer 中重复相同内容。Provider 级 router 很难区分模型原生推理和用户正文，除非把每种模型差异编码到前端。真实 `finish_reason` 也可能被通用流结束处理覆盖，导致超长截断看起来像正常完成。分离的思考面板和正文面板还会让实时流与历史回放分叉。

## 决策

**Agent 使用类型化模型通道和自然停止 ReAct loop。** 模型需要证据时调用工具；否则，普通 assistant text 就是最终答案。不再存在承载答案的工具，也没有独立 thinking 工具。

Provider 暴露三个语义通道：`reasoning`、`content`、`tool_calls`，并附带 usage 和上游 `finish_reason`。`ChatResponse` 额外携带 `display_reasoning` 和 `content_channel`，让引擎在原生 reasoning 模型和使用 `<think>` 标记的模型之间提供统一视图。`PlainContentClassifier` 负责增量解析 think 标记，并缓冲无标记普通 content，直到 loop 可以根据工具调用意图归类。

公开 Agent SSE 契约使用事件类型：

| 事件 | 含义 |
|---|---|
| `reasoning_delta` | 用户可见推理/规划增量。 |
| `tool_call` | 模型发起的工具调用及参数。 |
| `tool_progress` | 按 tool call ID 关联的长工具边界内进度。 |
| `tool_result` | 安全的执行元数据；原始工具输出不外发。 |
| `text_delta` | 用户可见最终答案或兜底答案增量。 |
| `token_usage` | 当前步骤的上下文用量。 |
| `turn_end` | 带类型化 `finish_reason` 的回合结束。 |
| `complete` | 汇总步骤数量、真实总耗时与首答延迟。 |
| `error` | 用户可读的失败说明。 |

废弃的 `thought`、`final_answer` 事件和工具不属于新契约。旧会话中的历史 agent steps 仍可读取，但新事件按实时 SSE 相同的顺序和结构持久化并回放。前端渲染一条有序 transcript，而不是分离的思考和回答面板。

长工具可以发送 `tool_progress`，不需要新增 transcript 段落。进度只携带有界元数据。`complete.total_duration_ms` 现在表示完整请求耗时；`complete.first_answer_ms` 保留首个 `text_delta` 的延迟，客户端不再把首答延迟与总执行时间混为一谈。

在支持 thinking 的 provider 上，assistant tool-call 轮产生的原生 reasoning 会在后续请求中作为 `reasoning_content` 回喂，符合 DeepSeek 官方多轮工具调用规则。Provider 请求保留上游 `finish_reason`；`turn_end.finish_reason` 按情况暴露 `stop`、`length`、`max_iterations`、`empty` 和 `error`。

这对第三方消费方是 breaking change。`artoo-open-api.md` 是迁移契约，记录了事件字段、顺序、兜底答案行为和历史回放规则。

## 备选方案

**保留 `final_answer`，继续改进解析器。** 否决。解析器可以恢复部分截断 JSON，但该设计仍然让普通答案文本非法，增加 token 开销，并迫使弱模型穿过两层协议。Provider adapter 也仍要承担答案语义。

**保留旧事件，由前端分类 provider content。** 否决。每个前端都要复刻 native reasoning、`<think>`、兜底答案和 provider 方言规则。历史回放仍需要第二条推断路径。

**在旧 SSE 契约旁运行 v2 endpoint。** 否决。旧协议无法在不给引擎和前端增加兼容分支的情况下表达目标语义。Artoo 的第三方可以按文档做事件映射迁移；同时保留两套契约会给生产环境留下两种回合结束模型。

## 后果

Loop 现在只有一条终止规则：模型响应没有 tool calls 就结束回合，其 text channel 就是答案。这移除了“推理完成但答案工具没有被调用”这一最常见静默失败。Provider 拥有原生 reasoning；engine 拥有能力归一化和工具策略；SSE bridge 拥有协议转换；前端只负责表现。

截断通过 `finish_reason=length` / `turn_end.finish_reason=length` 显式表达。缺失答案是 loop 控制问题，由重试或兜底合成处理，而不是畸形工具参数解析问题。原生 reasoning 回喂保留了多轮工具上下文，也避免 thinking assistant 轮后接工具观察时 DeepSeek 官方端点返回 400。

代价是破坏性 API 变更：第三方必须消费 `reasoning_delta` 和 `text_delta`，停止寻找 `thought`/`final_answer`，并用 `turn_end` 取代 final-answer done 标记。更简单的 loop 也把停止时机的更多自由交给模型；重复工具抑制和重复检索防护仍是后续工作。

## 测试

后端测试覆盖原生 reasoning 与自然文本终止、后接 tool calls 的普通 content 归类为 reasoning、跨 chunk `<think>` 解析、类型化 SSE 事件名、工具进度、真实总耗时与首答耗时、历史结构重建，以及 tool calls 场景下的原生 reasoning 回喂。后端套件结果为 800 passed、13 skipped；`test_kb_list_count_filter.py` 中 3 个 fixture setup 错误与本决策分开跟踪。前端构建和测试覆盖交错 transcript 渲染路径。
