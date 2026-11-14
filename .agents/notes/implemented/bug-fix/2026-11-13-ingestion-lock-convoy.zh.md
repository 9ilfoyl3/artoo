# Agent Note: Bounded ingestion slots and short pipeline transactions

Status: implemented

## Problem

文档 Pipeline 在读取文档元数据后，把 SQLAlchemy Session 事务一直保持到 OCR、
Embedding、Milvus 清理和向量写入之后。同时，Worker 为每条消费消息创建无上限 task，
并在获取处理 semaphore 之前执行文档状态查询。

一次重启期间，backend DDL 请求 `documents` 的 `ACCESS EXCLUSIVE` 锁，而四个索引事务
持有 `ACCESS SHARE`。后续状态查询与列表查询排在 DDL 后面；Worker 耗尽 30 个连接，
摘要任务在 180 秒后超时，毒消息回调也无法持久化失败状态。

## Decision

Pipeline 先在一个短事务中快照 `KnowledgeBase.tenant_id` 和 `Document.filename`，并在
任何 CPU、OCR、Embedding 或 Milvus 工作之前提交。向量索引完成后，再用第二个事务写入
chunks 与 completed 文档状态。

Worker 按 `PIPELINE_MAX_CONCURRENT` 限制派发；所有处理槽都被占用时不再创建新 task。
每个 task 先获取慢道与全局处理 semaphore，再执行幂等状态查询、打印 processing 日志或
进入 Pipeline。残留 `processing` 文档恢复只在 Worker 启动时执行。

## Alternatives considered

**扩大数据库连接池。** 否决作为修复，因为更多连接仍会排在同一个 DDL 锁后面，并增加
PostgreSQL 压力。

**只降低 `PIPELINE_MAX_CONCURRENT`。** 否决，因为它只能缩小窗口，不能移除长事务或
无上限 task 累积。

**只要求运维每次重启前停止 Worker。** 否决作为唯一方案，因为直接 Compose 部署和崩溃
恢复仍然不安全。

## Consequences

长时间文档处理期间不再占用数据库连接。Worker 积压保留在 Redis/PEL 中，直到处理槽
可用，使 semaphore 成为权威准入边界。若 Milvus 写入成功但最终数据库提交失败，仍可能
留下向量；现有的索引前孤儿清理继续作为恢复机制。

## Testing

`backend/tests/test_pipeline_empty_document.py` 验证 `process_to_vectors` 执行时没有
未提交事务。`backend/tests/test_ingestion_lock_safety.py` 验证状态查询等待处理槽、
槽位满时派发等待，以及成功任务仍会 ACK。
