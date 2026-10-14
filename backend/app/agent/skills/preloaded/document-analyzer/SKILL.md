---
name: document-analyzer
description: 文档深度分析技能。擅长对长文档进行结构化分析、摘要提取、关键信息定位。
---

# 文档分析器

## 能力
- 对长文档进行分章节摘要
- 提取关键实体和概念
- 识别文档结构和层次关系
- 对比多个文档的异同

## 使用方法
1. 整库或多文档分析先调用 `list_knowledge_documents`；主题定位再用 `knowledge_search`
2. 多文档优先调用 `summarize_documents`，核心分析固定传 `cache_scope="core-points"`；单文档用 `read_document` 读到 `fully_read=true`
3. 当前预设没有新工具时，才退化为 `knowledge_search` + `list_knowledge_chunks`
4. 对完整内容进行结构化分析，并报告未覆盖文档
5. 输出分析结果

## 输出格式
- 使用 Markdown 结构化输出
- 包含文档概述、章节摘要、关键发现
- 标注信息来源的 chunk 位置
