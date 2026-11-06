# Agent Note: Bounded parallel tool calls

Status: implemented

## Problem

`AgentConfig.parallel_tool_calls` 已存在，但生产聊天运行链路从未把预设值传进去，因此正常 Agent 请求都会串行执行工具。表面上的“并行开始”来自执行前一次性发送全部 `tool_call` 事件；`tool_result` 仍然逐个到达。

休眠的并行分支本身也是坏的。它对 `asyncio.as_completed()` 普通迭代产生的对象直接查询元数据字典，但普通迭代产生的是结果协程，不是原始 task。因此打开开关会抛 `KeyError`，无法执行工具。

## Decision

并行工具执行是预设能力，并受平台持有的并发上限约束。

`preset-smart-reasoning` 显式开启 `parallel_tool_calls`。自定义与遗留预设缺少该键时默认关闭，升级不会静默改变有副作用工具集的执行顺序。Agent 预设编辑器提供布尔开关，并在编辑预设时保留该字段。

`AGENT_TOOL_CALL_MAX_CONCURRENCY` 默认值为 4。运行链路把它传给 `AgentConfig.tool_call_max_concurrency`，使其至少为 1，并将它排除在租户可编辑的 preset JSON 之外，避免预设抬高平台资源上限。

当并行执行开启且模型响应包含多个工具调用时，engine 为每个调用创建一个 wrapper task，并用 `asyncio.Semaphore` 限制活跃 wrapper task 数。每个 wrapper 随结果返回原始索引和元数据，从而移除无效的 task 到元数据映射。

工具结果继续按完成顺序发送 `tool_result` 事件。`AgentState.steps[*].tool_calls` 与模型可见的 `tool` 消息仍保持模型原始工具调用顺序，因此即使完成顺序不同，持久化和协议回放仍然确定。

工具异常仍按调用隔离，并转成失败的 `ToolResult` observation。Agent 回合取消时，未完成的工具 task 会被取消并等待清理。并发值为 1 也有效，表现为有界串行执行。

该决定只使用 Agent 运行时并发，不增加或改变 provider 请求参数、SSE payload 字段或持久化事件结构。

## Alternatives considered

**为所有预设开启并行执行。** 已否决，因为此前运行时始终串行。静默启用会让自定义预设中的依赖型或有副作用 MCP 工具发生重排。预设应显式开启，而内置智能推理预设默认开启。

**让每个预设选择并发上限。** 已否决，因为 preset JSON 由租户可编辑。否则租户可以抬高平台级 task、线程、检索或远端服务压力。并发上限由平台设置持有。

**使用 `asyncio.gather` 并在全部完成后记录结果。** 已否决，因为这会保留实际重叠，却失去按完成顺序流式发送 `tool_result` 和进度可见性。

**通过扫描已完成 task 映射完成结果。** 已否决，因为这会重复簿记，并继续依赖 Python `asyncio.as_completed()` 的 yield 语义。wrapper task 随结果携带元数据，可以从结构上消除该问题。

**让结果记录保持完成顺序。** 已否决，因为模型回放、历史重建和调试不应依赖网络时序。只有公开完成事件按完成顺序，状态和协议消息保持源顺序。

## Consequences

内置智能推理 Agent 现在会并发执行独立工具调用，并受平台上限约束。自定义预设所有者可以通过预设表单开启或关闭该行为。缺少该键的既有预设继续保持串行。

这里的并行是适合检索、模型、数据库和远端 MCP I/O 的协作式 asyncio 并发。CPU 密集或同步阻塞工作仍会阻塞事件循环，必须移到 worker 线程或进程。

单个工具的副作用和顺序假设仍由运维方判断。暴露依赖型或非幂等工具的预设应关闭并行执行。

公开 SSE 事件名和字段不变。开启并行后，完成顺序可能与调用顺序不同；由于每个结果都以 `tool_call_id` 关联，现有客户端已经支持这种形式。

## Testing

`backend/tests/test_structured_result.py` 覆盖串行回归、slow/fast 调用重叠、按完成顺序发送 `tool_result`、按源顺序持久化记录、逐调用异常隔离、配置的并发上限、运行时预设 opt-in，以及非法并发值的归一化。`backend/tests/test_agent_config_default.py` 验证内置智能推理预设开启该能力。前端生产构建验证预设编辑器契约。

目标后端测试 26/26 通过；完整后端套件通过 818 项，另有 `test_kb_list_count_filter.py` 中 3 个既有的 `client` fixture setup 错误。Python 3.12 wrapper task smoke test 也确认结果按完成顺序返回。
