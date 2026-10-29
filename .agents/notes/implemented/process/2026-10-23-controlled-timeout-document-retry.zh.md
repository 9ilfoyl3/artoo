# Agent Note: Controlled timeout document retry

Status: implemented

[English](2026-10-23-controlled-timeout-document-retry.md) | 中文

## Problem

批量入库后，部分文档会因 worker pipeline 超过 60 分钟而失败。无差别重试会再次制造
相同的 Milvus 与 worker 压力。实施人员需要一个安全、可传输的流程，只重试超时失败，
走正常 batch-retry 业务路径，并根据当前文档积压控制提交速度。

## Decision

`deploy/retry-timeout-failed-docs.py` 可以汇总 failed 文档的错误原因，并按明确的
LIKE 类别重试。默认仍安全地只选择 `处理超时%`；重试全部失败文档必须显式加
`--allow-all-failed`。脚本通过 `POST /api/documents/batch-retry` 提交，复用该端点的
chunk/向量清理和 MinIO 重新入队语义。每批提交前都会等待
`pending+processing` 降到 `--max-backlog` 以下；同时支持 dry-run、批大小、批次暂停、
总量上限和最大等待时间。API 凭据从环境变量或部署 `.env` 读取。

## Alternatives considered

**直接修改 PostgreSQL 状态再写 Redis。** 拒绝：会重复实现 batch-retry 端点的清理
与一致性职责。

**重试所有失败文档。** 拒绝：非超时失败原因不同，批量重试可能再次造成过载。

**一次提交全部 ID。** 拒绝：绕过背压，可能重新触发 Milvus index/load 故障。

## Consequences

实施人员可以先 dry-run、再小批试跑，确认稳定后继续处理全部超时失败。由于会主动等待
积压下降，重试过程会比一次性提交更慢。脚本依赖宿主机能够执行 `alegal-postgres`
容器命令，并需要有效的维护 API Key。

## Testing

`python3 -m py_compile deploy/retry-timeout-failed-docs.py` 与 `--help` 通过。
状态、积压、ID 解析、SQL 字面量转义和失败原因汇总解析已用假 subprocess 验证；
未修改生产数据库。
