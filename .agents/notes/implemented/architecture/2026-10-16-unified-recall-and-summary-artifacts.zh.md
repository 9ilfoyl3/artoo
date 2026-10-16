# Agent Note: Unified recall and durable summary artifacts

Status: implemented

## Problem

Agent 同时暴露了语义重叠的 `knowledge_search`、`grep_chunks`、
`list_knowledge_chunks`、`list_knowledge_documents` 和
`summarize_documents`。这扩大了提示词与工具选择面，使小模型的工具选择不稳定，
也让全库总结一开始就进入长时间模型调用，下一轮无法复用已有结果。检索结果也缺少
统一的确定性后处理，包括近重复去除、邻块扩展、顺序合并和截断审计。Agent loop
仍依赖静态 token 估算、总时长式流超时、未严格检查的 JSON、统一工具输出预算，以及
对所有工具无区分并行执行。

## Decision

Artoo 现在只向模型暴露一个召回原语：`search_knowledge`。它支持
`hybrid`、`semantic`、`keyword`、`summary` 四种模式。旧召回工具不再注册进
Agent 预设或内置预设。

文档能力保持精简：

- `list_documents` 返回授权范围内的正式文档与会话文件，支持分页、文件名关键词
  过滤和摘要制品状态。
- `read_document` 支持 `doc_id` / `chunk_id`、`offset` / `limit` 顺序读取，以及
  `query` / `regex` 上下文窗口，并与召回工具使用同一套 KB / 会话授权范围。
- `todo_write` 是唯一新增的规划工具。它维护 `AgentState` 中有界的任务列表，因会
  修改 loop 状态，调度器会将其与读取工具之间建立串行屏障。

`search_knowledge` 执行确定性后处理：精确 chunk ID 去重、规范化内容签名、基于
token overlap 的近重复去除、子块到父块扩展、有界邻块扩展、相邻父块合并、来源与
位置稳定排序、FAQ answer 注入，以及挂到工具结果和 SSE 事件上的 retrieval audit。

正式文档与会话文件现在拥有持久化 `DocumentSummary` 制品。索引完成后向
`pipeline:summaries` Redis Stream 投递任务，独立 worker 以关闭思考的方式生成包含
`summary`、`gist`、`topics`、`doc_type`、`typical_question` 的 JSON profile。
制品按父块 ID 与内容版本化。摘要检索读取 PostgreSQL 权威记录，Redis 不是正确性
依赖。

Agent core 还使用 provider usage 校准 token 估算，按 session 与 prompt 指纹持久化
压缩 checkpoint，把总流超时替换为逐 chunk stall watchdog，对 JSON 做保守修复但
拒绝执行截断或非法工具参数，设置按工具独立的输出预算，并将只读并发与 mutation
barrier 分离。未知外部 MCP 工具在未来的可信注释出现前一律视为 mutation。

## Alternatives considered

**保留旧召回工具名作为别名。** 拒绝，因为别名仍保留模型选择问题、重复工具 schema，
并让不同检索路径的后处理继续漂移。

**文档上传时同步生成摘要。** 拒绝，因为模型延迟会拖慢入库，并使入库可靠性依赖
LLM 服务。持久化任务 lane 让源文档先立即可用。

**只在 Redis 保存摘要。** 拒绝，因为缓存逐出或 Redis 重启会丢失制品并触发昂贵
重算。Redis 只承载队列传输与可选加速，PostgreSQL 拥有版本化 profile。

**用第二个 Milvus collection 检索摘要。** 本改动拒绝。profile 是小体量元数据记录，
增加第二套一致性域不会改善主检索路径，却会显著增加运维成本。PostgreSQL 词法排序
足够支持清单与文档发现，精确证据仍由主混合检索器提供。

**把所有工具调用都并行化。** 拒绝，因为外部工具和状态修改可能有副作用。调度器只
并发连续只读调用，并在 mutation 边界串行化。

**修复并执行截断的工具 JSON。** 拒绝，因为语法可修不代表语义完整。截断参数只会生成
工具错误 observation，绝不执行。

## Consequences

显式维护工具白名单的预设必须改为新名称。内置预设会自动同步，用户自定义预设仍由
所有者控制。消费 SSE 工具名的业务系统需要映射旧名到新面。请求 schema 与 MCP
传输不变。

摘要生成新增一条 Redis Stream lane 和 worker 并发，并新增两张 PostgreSQL 表：
`document_summaries` 与 `agent_compaction_checkpoints`。摘要失败与入库隔离。
模型会在检索结果上看到附加的 audit 对象，旧客户端可以忽略。

统一检索器降低了提示词与工具选择歧义；profile 就绪后，全库问题会显著加快。缺失
profile 时退化为现有文档读取，不伪造摘要。邻块扩展与合并会增加单条结果的上下文，
但字符上限会确保工具总输出仍受 per-tool budget 约束。

## Testing

`backend/tests/test_unified_knowledge_agent_core.py` 覆盖 JSON 修复与截断拒绝、
per-tool 输出预算、Todo 校验、确定性检索后处理、token 校准、checkpoint round trip、
统一 summary 模式、mutation barrier、stall watchdog、文档过滤与摘要状态、授权读取，
以及摘要 profile 规范化。既有 Agent、retrieval、structured-result 与 data-plane
套件覆盖兼容敏感 loop 和协议路径。

真实 48 文档 KB A/B 运行
（`backend/benchmarks/results/2026-10-16-unified-agent-ab.json`）显示：search-only
基线 30.5 秒完成，但 4 次召回只覆盖 46 份可读文档中的 1 份；摘要制品路径使用
`list_documents` + 1 次 42ms 的 `search_knowledge(mode="summary")`，覆盖全部 46
份文档，并在 40.2 秒内生成 3,308 字有依据回答。工具调用从 5 次降到 2 次，文件
覆盖增加 45 份；额外答案时间来自模型生成，而非检索。
