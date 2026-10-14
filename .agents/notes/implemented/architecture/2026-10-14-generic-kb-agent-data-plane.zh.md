# Agent Note: Generic knowledge-base Agent data plane

Status: implemented

## Problem

Agent 此前只有搜索与读取工具，没有确定性的文档发现面。集合型任务只能依赖宽泛搜索，有时还会从文件编号推断缺失文件。正式文档读取工具同时统计并返回父子 chunk，重复文本可能进入模型上下文，而且没有任何状态记录文档是否已完整读取。检索结果也没有稳定 evidence 身份，重复的多文档摘要没有可复用缓存，精确计算仍只能放在模型推理里完成。

这是领域无关的问题，会影响任何包含一组文档、需要清单、摘要、审计或覆盖检查的知识库。业务事实、计算、文档锚点和审批规则不属于这一层。

## Decision

通用知识库 Agent 增加确定性的检索与集合数据面。

`list_knowledge_documents` 列出已授权正式 KB 与可选会话文件源，返回稳定的 `doc_id`、文件名、类型、状态、来源、KB ID 和索引 chunk 数，并支持分页。结果写入 `AgentState.document_catalog`。

`read_document` 是首选完整文档读取工具。它只使用父 chunk，与文档预览链路一致，避免父子块重复。它返回 `has_more`、`next_page`、`read_chunks` 和 `fully_read`，并在 `AgentState.document_read_ranges` 记录已读区间。`list_knowledge_chunks` 作为旧版兼容别名保留。

`summarize_documents` 是领域无关的 map-reduce 工具。每份选中文档独立加载并摘要；超大文档按确定字符预算分批、摘要并归并。并发有上限，每份文档与其他文档隔离，结果携带稳定文档身份和完整读取状态。成功摘要按文档内容版本、模型身份和任务指令写入 Redis 缓存；缓存是 best-effort，Redis 不可用时直接摘要。

检索与读取会登记稳定 evidence，其身份为 `evidence_id = hash(doc_id, chunk_id)`。`AgentState` 同时按 ID 和 chunk 建立索引，重复检索不会产生重复 evidence 身份。

`calculate` 提供有界的 Decimal 四则运算、百分比、min/max/sum 和显式舍入，不包含业务公式，也不解释业务语义。

长文档集合任务会通过 engine 流式发送 `tool_progress`，而不是一直阻塞到整个工具返回。逐文档模型调用使用不超过 6 的并发，显式关闭模型 thinking，并在重复的核心摘要任务中复用稳定的 `cache_scope`。进度只包含元数据，不包含文档正文。

对于集合型请求，system prompt 要求模型先列文档再宽泛搜索，只使用返回的 doc_id，绝不从编号推断文件，并报告未读或不可用文档。若检索已用完有界改写机会且仍没有新增证据，loop 会确定性提示模型调用文档目录。

内置 `preset-smart-reasoning` 预设由平台维护，启动同步时会把当前规范
`allowed_tools` 合并进旧数据库行，避免 Agent 永久停留在旧搜索/读取工具集。用户自定义预设不会被改写，需要由所有者显式勾选新工具。预置摘要与文档分析技能也会优先使用文档清单、`read_document` 和 `summarize_documents`，仅在新工具不可用时退化为旧搜索/读取路径。

这些工具都是通用能力，不包含法律、财务、案件管理或文书审批语义。

## Alternatives considered

**继续把语义和关键词搜索当作文档发现。** 已否决，因为搜索召回不等于资产清单。搜索未命中不能证明文档不存在，宽泛搜索还会浪费模型迭代。

**增加语义重复 guard。** 已否决，因为它不确定，而且没有解决缺失的数据面。精确的目录与覆盖状态提供更强的不变量，也不需要猜测意图。

**把所有文档读入主 Agent 上下文。** 已否决，因为文档体积和跨文档干扰会线性增长。逐文档 map-reduce 能让最终综合上下文保持有界。

**把精确计算留在模型推理中。** 已否决，因为小数舍入、百分比和多步运算都是确定性操作。有界计算器能减少推理长度并避免静默出现不同结果；选择输入或业务公式仍由调用方负责。

**把全量摘要保留为一次阻塞工具调用。** 已否决，因为用户会数分钟看不到任何可操作反馈，一个长调用也会推迟同一轮后续结果。按文档上报有界进度并即时发送工具结果，不会改变 map-reduce 边界，但不会再隐藏执行进展。

**把文档清单放入业务 MCP server。** 对通用知识库路径已否决。Artoo 自己拥有 KB 和会话文件清单；业务 MCP 可以增加自己的领域清单，而无需修改 Artoo。

**把每个中间摘要暴露成公开 API 事件。** 已否决，因为工具调用和有界摘要已足够供 Agent loop 使用，不需要改变公开 SSE 或持久化契约。

## Consequences

集合任务现在从权威文档目录开始，可以识别失败或不可用文档，并区分完整读取与部分读取。多文档摘要让每份文档在隔离上下文中处理，只向主循环返回紧凑的逐文档结果。重复摘要可以复用按版本计算的缓存，精确计算也不再占用推理通道。

显式白名单工具的自定义预设需要加入 `list_knowledge_documents`、`read_document`、`summarize_documents` 和 `calculate`。没有 `allowed_tools` 列表的既有预设仍可看到完整工具集。内置智能推理预设会自动同步到该列表；用户自定义预设仍由所有者控制。map-reduce 会在工具内部增加模型调用，但这些调用替代了更大的主循环上下文增长，并避免反复宽泛搜索。Redis 是可选加速路径，不是正确性依赖。

Open API 新增可选的 `tool_progress` 事件与 `complete.first_answer_ms`；`complete.total_duration_ms` 现在表示真实总耗时。旧客户端可以忽略进度事件。自定义预设的所有者仍决定摘要工具是否可见。

Open API 请求和 SSE 事件结构不变。新工具结果仍是普通 Agent tool observation，通过 `ToolResult.data` 携带结构化数据。

## Testing

`backend/tests/test_generic_kb_agent_data_plane.py` 覆盖授权文档清单、分页、会话文件接入、仅父块读取、跨页覆盖、稳定 evidence 身份、有界空检索目录回退、确定性 Decimal 计算、缓存键失效，以及逐文档 map-reduce 摘要。回归测试还会验证旧内置预设行在不丢弃其他配置键的前提下完成升级，验证预置摘要技能优先使用文档清单和读取/摘要工具；摘要测试会断言逐文档模型调用关闭 thinking，并在最终工具结果前发送文档进度。

`backend/benchmarks/generic_kb_agent_ab.py` 在本地 PostgreSQL KB 上运行。对 7 文档本地 KB，仅搜索路径使用 4 次搜索、11 个 query 找到 6 份可读文档；目录路径用 1 次调用识别出 6 份可读和 1 份失败文档，并摘要全部 6 份可读文档。确定性 map-reduce 把 9,977 个父块字符压缩为 1,157 个摘要字符（88.4%），第二次相同摘要产生 6 次缓存命中且没有新增 map 调用。对 48 文档 KB，仅搜索发现 38 份且未暴露 2 份失败；目录识别全部 46 份可用文档，map-reduce 摘要全部 46 份，把父块正文从 154,304 字符压缩到 10,038 字符（93.5%）。第二次相同摘要产生 46 次缓存命中且没有新增 map 调用。
