# Agent Note: DOCX 结构化提取与页级 OCR

Status: implemented

## Problem

DOCX loader 只读取 `Document.paragraphs`，因此不包含表格单元格和其他 WordprocessingML
story。一个包含 15 行表格的法院送达回证因此只剩下标题段落。仅补表格提取也不够：带图片
文档会把图片 OCR 追加到已有文本之后，产生多个竞争文本来源，而且缺少稳定的页边界。

## Decision

`DocxLoader` 使用两条来源选择路径。

主文档 story 中没有图片的文档直接使用原生 DOCX 结构。loader 按正文顺序提取段落、表格、
页眉、页脚、内容控件和文本框。表格转换为 Markdown 行，保留字段和值的对应关系供检索
使用。制表符和软换行会保留。

主文档包含图片时，通过 LibreOffice 渲染为 PDF 以恢复页边界。页面中存在有效正文图片时，
生成一张整页 PNG 快照并标记为 `is_page_snapshot`。`DocumentPipeline._process_page_snapshots`
把这些快照送去 OCR，并用识别结果替换对应页文本。没有正文图片的页面保留 PDF 文本层。
pipeline 不追加局部图片 OCR 文本；整页快照成功后不再执行整文档 OCR；只有页快照没有
识别出文本时，才保留原文本层。

图片资源仍存在于页面资源字典、但没有实际绘制区域时，应视为没有正文图片。LibreOffice
可能在后续纯文本页复用图片 XObject；把它当成可见图片会导致这些页面被错误送去 OCR。

页眉页脚图片、缩略图和仅在主文档 story 之外引用的图片不会选择页级 OCR 路径。
LibreOffice 不可用或渲染失败时，含文本 DOCX 保留原生结构化路径；纯图片 DOCX 回退到
嵌入图片提取。

## Alternatives considered

**所有 DOCX 都转 PDF 后按 PDF 提取。** 已否决，因为每个普通文本文档都会增加渲染成本，
并丢失原生表格与内容控件语义。

**把嵌入图片的 OCR 文本追加到结构化文本。** 已否决，因为同一页同时存在文本层和图片时
会产生两个竞争来源，无法可靠消除表格顺序和标点差异。

**对含图片 DOCX 的所有页面都执行 OCR。** 已否决，因为一个 logo 或签名图片会让所有文本页
承担 OCR 成本，并替换掉准确的原始文本。

**分别使用 `Document.paragraphs` 与 `Document.tables`。** 已否决，因为会丢失二者相对
顺序，也无法覆盖内容控件和文本框。

## Consequences

送达回证等表格型 Word 文档现在能保留字段级内容。纯文本 DOCX 不再调用 LibreOffice 和 OCR。
含图片文档只为正文图片页支付渲染和 OCR 成本，并且每个此类页面只有一个权威结果。

该启发式可能把跨越页眉/页脚区域的正文装饰图视为内容图片；对应页面会改走整页 OCR。图片
只存在于页眉页脚的文档仍保持结构化路径。Markdown 表格会展开合并单元格中的重复值，而不是
复刻 Word 的视觉跨列。

`EmbeddedImage.is_page_snapshot` 是 pipeline 内部状态，不改变 Open API、持久化 chunk schema
或清理契约。

## Testing

`backend/tests/test_docx_loader.py` 覆盖表格与正文顺序、合并单元格、制表符、软换行、
内容控件、正文图片页选择、整页替换、纯图片页快照与回退、未绘制图片资源，以及纯文本
DOCX 不调用 LibreOffice。LibreOffice `.docx → .doc → DocLoader` 往返测试也验证了合并
单元格表格能够保留。真实样本 `11-送达回证_第三人.doc` 会经过 LibreOffice 转换，并验证
能提取完整表格而不只是标题段落。
