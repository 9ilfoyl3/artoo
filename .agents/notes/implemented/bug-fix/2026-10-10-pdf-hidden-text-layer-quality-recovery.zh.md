# Agent Note: PDF hidden text-layer quality recovery

Status: implemented

## Problem

扫描 PDF 可能同时包含整页图片和隐藏文本层。既有的
[PDF scan text-layer deduplication](./2026-09-23-pdf-scan-text-layer-deduplication.zh.md)
决策正确地避免在文本层可用时重复 OCR 整页图片。但 `TextCleaner` 假设每页都是
A4 高度（`842pt`）。宽页或其他非标准页面因此可能把大部分正文当成页脚并删除。

`20_机动车违法查询.pdf` 事件同时暴露了这两个问题。该页为 `3798 x 2483pt`，
但清洗器使用 `842pt` 高度，把 `799.9pt` 以下所有短文本块删除，并将 `474`
个字符缩减成错误残文：
`机动车违法查询\n号牌种类0 2 小型汽车只脑三部A 2 8 8 W B6 0 3 1`。
隐藏文本层还把 ASCII 标识符拆成单字符 token。按页 Paddle OCR 比原生文本层更准确，
但 pipeline 认为文本层可用，因此从未调用 OCR。

## Decision

PDF loader 现在在 `LoadResult.page_heights` 中记录每页真实高度，`TextCleaner`
使用这些高度过滤页眉和页脚。如果页面高度不可用，cleaner 跳过 bbox 过滤，而不是
套用 A4 假设。当清洗后保留的非空白字符少于 30% 时，它还返回原始内容。

对于混合扫描页，当隐藏文本层同时具有整页背景图片和单字符 ASCII 字母数字 token
的碎化特征时，pipeline 优先执行 OCR。如果 OCR 没有返回文本，则保留原始 PDF
文本层作为回退；如果 OCR 成功，其结果成为文档内容。PaddleOCR 仍由 `input_prep`
按页准备图片并接收输入。

## Alternatives considered

**对所有混合文本层都执行 OCR。** 否决，因为既有去重决策有意避免对可用文本层重复
OCR，无条件 OCR 会为正常扫描 PDF 增加成本和延迟。

**始终保留原生文本层，只修复页面高度问题。** 对比输出后否决：原生文本层保留了
字段，但数字被拆开，包含 `黄波区`、`非警运` 等错误字符，标签顺序混乱，并带有法院
水印文本。按页 Paddle OCR 得到的字段更干净、更准确。

**OCR 为空时将原生文本层作为第一回退。** 接受。优先使用 OCR 以获得质量，但原生
文本层比用空的 OCR 结果替换非空文档更安全。

**保留固定 A4 高度假设。** 否决，因为源 PDF 的页面尺寸直接否定了这个假设，而且
同一缺陷会影响其他非标准扫描件。

## Consequences

只有当整页图片和碎化 ASCII 文本同时存在时，低质量隐藏文本层才会触发按页 OCR。
对于缺失或异常尺寸，cleaner 会更保守，可能保留部分页眉或页脚噪声，但不会冒数据
丢失风险。`page_heights` 默认空列表，因此既有 `LoadResult` 构造方式保持兼容。

## Testing

回归测试覆盖非 A4 真实页面高度、缺失高度时的安全路径、30% 内容损失保护、正常 A4
页眉页脚移除、碎化文本检测，以及整页背景图前提。受影响 PDF 加载后页面高度为
`2483.0`，会被识别为低质量混合文本层，清洗后保留 `441` 个字符，而不是此前
42 个字符的错误残文。
