# Agent Note: Agent reference source type

Status: implemented

## Problem

Agent 引用可能同时包含正式知识库文档和当前会话上传的附件，但引用项此前只暴露
`doc_id`、`chunk_id` 和 `filename`。两类来源在检索结果中都由 `doc_id` 标识，
而原件预览接口不同：

- 知识库文档使用 `GET /api/documents/{doc_id}/raw`；
- 会话附件使用 `GET /api/sessions/{session_id}/files/{doc_id}/raw`。

缺少来源判别字段时，引用消费方无法安全选择正确的预览接口。公共多源检索响应已经
通过 `source_type` 解决该问题，但对话和 Agent 引用没有暴露同样信息。

## Decision

每个 `ReferenceItem` 现在都包含 `source_type`，取值只有两种：

- `doc_id` 属于 `documents` 时为 `knowledge_base`；
- `doc_id` 属于 `session_files` 时为 `session`。

`_build_references` 复用文件名校验所用的同一轮表查询来判定该值：先查正式文档，
再对未命中的 ID 查询 `session_files`。该字段会出现在非流式响应、SSE
`references` 帧、`/api/retrieval/agent` 以及落库后的 assistant 消息引用中，取值
词表与 `/api/retrieval/search` 保持一致。

为兼容旧客户端，响应模型中的 `source_type` 默认值为 `knowledge_base`。会话 ID
不会重复写入每个引用项，因为调用方发起对话时已经提供该标识，历史消费方也可从
会话接口获得。

## Alternatives considered

**根据是否传入 `session_id` 推断来源。** 已否决，因为同一会话可以在同一轮同时检索
知识库和附件，请求形状无法区分单条引用的来源。

**为每个引用返回可直接使用的预览 URL。** 已否决，因为 URL 会把响应契约绑定到部署
路由，还需要编码本不属于引用项的鉴权或有效期语义。

**用新的统一原件接口隐藏两类来源。** 已否决，因为现有文档与会话附件原件接口已经
分别拥有不同的鉴权和存储路径；再增加一层代理并不会消除客户端理解来源的需要。

**沿用工具文件词表（`document` / `session-file`）。** 对引用已否决，因为
`/api/retrieval/search` 已公开 `knowledge_base` / `session`；保持统一的公共引用
词表可以避免每个集成边界再做一次映射。

## Consequences

客户端可以直接根据引用选择正确的预览接口，无需额外查询元数据。知识库与会话附件
混合引用按单条分类，而不是按整个请求分类。来源判定复用现有文件名解析查询，不会
新增数据库往返。

新响应始终携带 `source_type`。本次变更前落库的引用可能没有该字段，消费方应将缺失
值按 `knowledge_base` 处理。该字段是增量字段，忽略它的客户端仍可继续读取原有引用
字段。

## Testing

`backend/tests/test_chat_reference_sources.py` 验证：来自 `documents` 的引用标记为
`knowledge_base`，来自 `session_files` 的引用标记为 `session`，且两者都能保留解析
后的文件名。
