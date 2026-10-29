# Agent Note: List-documents tool-result files

Status: implemented

## Problem

`list_documents` 把权威文档清单放在 `ToolResult.data["documents"]` 中，因此模型可以正确回答某个知识库有 13 份文档。但 Chat UI 收不到这些文件：引擎的 `tool_result` SSE 载荷只读取 `ToolResult.data["files"]`，而 `list_documents` 从未填充该字段。于是模型描述 13 份文档时，渲染出的“文档清单”工具行却收到 `files: []`。

## Decision

`list_documents` 现在会从每页返回的清单派生轻量 `files` 列表。每一项遵循共享 UI 契约 `{id, filename, source}`，同时覆盖正式 `document` 文件与 `session-file` 条目。更丰富的元数据仍保留在 `documents` 下；分页、摘要状态和模型可见的 XML 输出均不变。

共享契约固定在工具边界。Agent 引擎保持通用，React 客户端继续直接渲染 `tool_result.files`，不需要为 `list_documents` 增加特判。

## Alternatives considered

**让前端直接读取 `documents`。** 拒绝：UI 已让检索、阅读和清单工具共享同一个文件引用渲染器。按工具定制载荷形状会把清单细节扩散到展示代码，并增加另一种持久化事件格式。

**把完整清单对象放进 `files`。** 拒绝：`files` 是工具行使用的轻量预览契约。重复携带 chunk 数、摘要状态和分页元数据会放大每个持久化 `tool_result`，却不会改善渲染。

**在 Agent 引擎中把 `documents` 映射为 `files`。** 拒绝：引擎不应了解每个内置工具的内部结果形状。由工具负责把自身丰富的领域结果转换成共享事件契约。

## Consequences

文档清单工具行现在会显示模型收到的同一页文件名，并允许预览正式文档与会话文件。模型可见的工具输出和文档授权语义不变。

`files` 列表与 `documents` 一样按页返回。只有模型再次调用 `list_documents` 后才会渲染下一页，这保持了现有分页行为。

## Testing

`backend/tests/test_generic_kb_agent_data_plane.py::test_document_catalog_lists_only_authorized_completed_documents` 验证授权清单结果会暴露共享的 `{id, filename, source}` 文件契约。会话文件清单测试继续覆盖两种受支持来源。
