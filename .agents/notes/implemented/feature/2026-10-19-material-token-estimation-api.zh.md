# Agent Note: Material token estimation API

Status: implemented

## Problem

第三方系统在把知识库材料送入其他模型、计费系统或工作流前，需要估算材料长度。它们
可以枚举文档并逐份调用原文文本接口，但这会形成 N+1 HTTP 调用，并把 tokenizer
选择和父块拼接规则复制到每个调用方。全库估算还必须遵守与文档列表相同的知识库读
权限，同时避免重复计入子块文本。

## Decision

Artoo 在现有受保护 API 面上新增
`POST /api/materials/token-estimate`。请求必须且只能选择一种范围：

- `knowledge_base_id`：估算当前调用方在该知识库中可见的全部文档；
- `file_ids`：估算最多 1000 个显式选择的知识库文档 ID，按请求顺序去重。

`file_ids` 仅限正式 `Document.id`。会话临时文件继续使用其专用读取接口。

服务复用 `GET /api/files/{file_id}/content` 的父块投影：父块按
`chunk_index` 排序并以 `\n\n` 拼接，不计入会重复上下文的子块。结果报告解析后
Python 字符数和 `tiktoken` `cl100k_base` token 数。若 `tiktoken` 无法加载，
则使用现有 `TokenEstimator` 降级估算，并在响应中设置 `fallback=true`。

所有响应都稳定放在 `result` 信封中，包含汇总值和逐文件明细。每个请求知识库都通过
`KbAccessEnum.READ` 授权；不存在、跨租户或无权限统一返回 `404`。只读身份看不到
文档列表同样不可见的非 `completed` 文档；有写权限者会收到请求或知识库列表中的
文档，但其解析内容尚不存在时长度为 0。

## Alternatives considered

**要求客户端调用现有原文文本接口并在本地计算 token。** 拒绝，因为这会形成 N+1
请求模式、复制父块拼接规则，并让不同集成方选择不一致的 tokenizer。

**按原始文件字节数或 `file_size` 估算。** 拒绝，因为 PDF、Office 文件、图片和
音频的二进制大小与下游使用的解析材料长度无关。

**分别暴露知识库级和文件级两个接口。** 拒绝，因为一个按范围分派的请求更易文档化、
更便于第三方调用，也能保持汇总响应语义一致。

**允许同时传 `knowledge_base_id` 与 `file_ids` 作为过滤条件。** 拒绝，因为这会让
"知识库全部文档"与"仅这些文件"语义模糊，并新增一条价值有限的授权路径。

## Consequences

客户端获得一个确定性的估算请求，以及适合程序消费的显式汇总对象。接口不持久化
估算结果，也不触发解析，因此结果对应当前已建索引文本，重试或重建索引后可能变化。

Token 化属于 CPU 工作。实现会流式读取父块，并在任一时刻只保留一份文档文本，但
全库响应仍会为每份可见文档返回一行明细。超大知识库可能产生较大的 JSON 响应，并
按已索引文本长度消耗 CPU。`fallback=true` 表示环境只能提供基于字符数的近似结果。

## Testing

`backend/tests/test_material_token_estimate_api.py` 覆盖全库估算、指定文件排序与
去重、仅父块文本拼接、未完成文档、范围校验和不存在语义。
