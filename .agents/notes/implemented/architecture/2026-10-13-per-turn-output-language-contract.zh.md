# Agent Note: Per-turn output-language contract

Status: implemented

## Problem

智能体预设可以在 `custom_instructions` 中写入语言要求，但引擎会把动态语言指令追加到完整 system prompt 之后。因此预设文本之后还有一段同属 system message 的语言指令，而所有对话历史又排在 system message 之后。上一轮若产生了英文 reasoning，工具调用轮次中的语言指令就可能被历史内容压过。

语言检测此前也只检查当前 query。`继续`、`ok`、`1`、路径或纯符号等短追问会落到 English；即使当前对话是中文、日文或韩文也不例外。

## Decision

system prompt 继续作为核心行为和预设定制的稳定位置。预设定制被明确限制为角色、语气、领域词汇和工作流；它不覆盖 response contract、证据规则或工具语义。

Agent 根据当前 query 和最近的用户消息构造单轮输出契约。当前 query 可识别时以它为准；`继续`、数字等模糊短轮次继承最近一次可识别的用户语言；没有可用信号时不添加语言契约。契约以仅模型可见的 `<response_contract>` 块追加到当前 user message，位置在 query 和此前历史之后。它约束最终回答语言，并将简短 reasoning 的语言作为尽力而为：技术标识、代码、路径、工具名和引用原文可以保留原语言。

fallback synthesis 路径接收同一份契约。预设编辑器和提示词润色助手都会提示用户不要在自定义指令中重复输出语言规则。

## Alternatives considered

**把动态契约放在预设定制之后，但仍留在 system prompt。** 已否决，因为对话历史仍会排在契约之后，上一轮英文 reasoning 仍可能压过当前轮语言。

**语言检测失败时改写或重试最终回答。** 已否决，因为这会增加一次模型调用、复杂化流式输出，并让确定性语言分类进入每条回答链路。

**强制思维链使用用户语言。** 已否决，因为 provider reasoning 由模型控制，可能必须原样回放，强制语言还可能降低推理质量或增加 reasoning token。契约因此只在可行时偏好用户语言，不承诺控制隐藏推理。

**新增独立的预设语言字段。** 本次已否决，因为现有契约已经跟随当前用户语言，而新增配置轴会引入 UI、API、存储和迁移成本，却没有修复消息顺序与模糊短轮次的实际缺陷。

## Consequences

动态语言契约现在相对对话历史具有更近的位置，模糊追问会继承当前对话语言。稳定的 system prompt 不再随每轮语言变化，provider prefix 仍有机会复用。

模型仍可能违反契约，reasoning 语言没有保证；最终回答语言才是硬目标。公开 API、SSE 事件、持久化消息和存储格式均不变。

## Testing

聚焦后端测试覆盖中文与短追问语言识别、历史语言继承、中性轮次不强制 English、预设优先级边界、system prompt 稳定放置、原生 reasoning 回放和 fallback synthesis。前端生产构建验证预设编辑器文案。
