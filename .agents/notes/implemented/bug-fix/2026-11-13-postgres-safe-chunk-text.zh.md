# Agent Note: PostgreSQL-safe chunk text

Status: implemented

## Problem

PDF 提取和 OCR 即使输出整体合法的 UTF-8 文本，也可能夹带 U+0000。Python、Embedding
和 Milvus 可以接收该字符，但 PostgreSQL 的 `text` 与 JSON 参数会拒绝它。故障只在
SQLAlchemy 查询触发 autoflush 时以
`CharacterNotInRepertoireError: invalid byte sequence for encoding "UTF8": 0x00`
暴露，此时昂贵的解析、切分、Embedding 和向量写入都已完成。大型工具书文档可稳定
复现该问题。

## Decision

`DocumentPipeline.process_to_vectors` 将 U+0000 视为存储安全约束，而不是可选的文本
清洗质量偏好。在 loader、OCR、ASR、嵌入图片 OCR 合并和 cleaner 全部汇合之后，并在
空文档判断、切分、Embedding 与索引构建之前统一移除 NUL 字符。

pipeline 还会在切分和 enrichment 之后清理父块、子块、面包屑标题及 enrichment 子块。
写入 PostgreSQL 的 Chunk 内容和写入 Milvus 的 content 再执行一次同一清理，作为最终
防御边界。换行、制表符及其他合法字符保持不变。每次移除都会记录 source kind、source
ID 和数量，使上游提取器缺陷保持可观测，而不是静默污染文本。

## Alternatives considered

**只在 SQLAlchemy INSERT 路径丢弃 NUL。** 已否决，因为非法字符仍会进入 Embedding
输入和元数据提取，而且会话路径或未来存储路径可能绕过同一段插入代码。

**拒绝包含 NUL 的文档并标记失败。** 已否决，因为周边文本合法且有价值，单个提取器
伪影不应让本可检索的文档永久不可用。

**把所有控制字符都替换为空白。** 已否决，因为 PostgreSQL 接受其他控制字符，而且
这会改变超出本次故障范围的合法文档结构。

**依赖知识库的 `enable_cleaner` 开关。** 已否决，因为关闭去噪不能同时关闭所有
pipeline 路径共享的存储安全不变量。

## Consequences

此前失败的文档无需修改源文件即可重试成功。移除 NUL 可能让提取器伪影两侧的文本直接
相连，但这是破坏性最小的安全表示，且 WARNING 提供了审计线索。检索与 Embedding 看到
的内容和持久化内容保持一致。新的 loader、OCR Provider 和 enrichment 步骤仍受切分后
检查与存储边界检查保护。

## Testing

`backend/tests/test_pipeline_text_safety.py` 验证只移除 NUL，同时保留换行与制表符，
验证仅含 NUL 的文档按空文档完成，并验证 chunker 注入的 NUL 会从父块、子块、
面包屑标题、元数据和 Embedding 输入中移除。

验证通过：

- `../.venv/bin/python -m pytest tests/test_pipeline_text_safety.py -q`（3 passed）
- `../.venv/bin/python -m pytest tests/test_pipeline_empty_document.py tests/test_pdf_scan_text_layer.py tests/test_pipeline_text_safety.py -q`（23 passed）
- `../.venv/bin/python -m pytest tests/test_session_upload_process_task.py tests/test_session_upload_integration.py tests/test_pipeline_text_safety.py -q`（19 passed）
- `../.venv/bin/python -m compileall -q app/pipeline/cleaner.py app/pipeline/pipeline.py tests/test_pipeline_text_safety.py`
