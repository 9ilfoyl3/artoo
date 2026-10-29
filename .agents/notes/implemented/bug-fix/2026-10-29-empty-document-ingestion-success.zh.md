# Agent Note: Empty document ingestion success

Status: implemented

## Problem

文件经过加载、OCR、ASR 与清洗后没有文本时，会以 `EmptyDocumentContentError` 进入普通
异常路径。知识库入库会重试这些文档，最终写入 `status=failed` 和面向 OCR 的错误，
但空文件本身是合法输入，并不是处理失败。

## Decision

提取流程结束并确认文档没有文本后，`DocumentPipeline.process_to_vectors` 返回合法的零
chunk `ProcessedDocument`，并写入内部元数据 `empty_content=True`。该分支在切分、向量化
和索引之前返回，因此不会生成占位文本或向量，也不会占用容量或污染检索。

知识库入库随后正常完成，状态为 `status=completed`、`chunk_count=0`、
`progress=100`，且没有 `error_message`。配置可用时仍会尝试 OCR 和 ASR；音频文件没有
ASR Provider 时仍会失败，因为此时无法判断文件是否为空。

会话文件入库只接受由 pipeline 明确标记为空文档的零 chunk 结果。非空文档意外切出零
chunk 时仍抛出 `EmptyDocumentContentError`，保留切分完整性校验。零 chunk 文档没有可
处理的正文，因此跳过图谱抽取和摘要调度。

## Alternatives considered

**继续以 HTTP 422 拒绝空文档。** 拒绝：这会把合法空输入当作处理失败，造成不必要的重试
和运维噪音。

**插入占位 chunk。** 拒绝：合成文本会占用 chunk/向量容量，并可能作为真实文档内容出现在
检索结果中。

**新增持久化的 `empty` 文档状态。** 拒绝：`completed` 加 `chunk_count=0` 已能表达该
结果，无需修改数据库或 Open API 契约。

**不加 `empty_content` 标记而接受所有零 chunk 结果。** 拒绝：这会掩盖本来包含文本的
文档上的切分器缺陷。

## Consequences

空 PDF、Office 文件、文本文件、图片和转写后确实无声的音频都会显示为正常完成且零 chunk
的文档。它们不会产生检索命中、图谱产物或摘要产物。该标记属于 pipeline 内部元数据，
不改变公开的文档响应结构。

已经失败的存量文档不会自动迁移；若需要改变状态，用户必须主动重试。缺少提取依赖时仍可
失败，因为 pipeline 尚未确认源文件为空。

## Testing

`backend/tests/test_pipeline_empty_document.py` 覆盖配置及未配置 OCR 的空提取、零 chunk
处理、知识库状态落库，以及会话文件以零 chunk 正常完成并在完成事件中返回
`chunk_count=0`。完整会话上传测试组加新增 pipeline 回归测试的命令结果为 `79 passed`。
