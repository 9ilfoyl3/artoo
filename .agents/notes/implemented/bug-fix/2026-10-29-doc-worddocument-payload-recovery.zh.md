# Agent Note: DOC WordDocument 内嵌负载恢复

Status: implemented

## Problem

部分办公系统会把 `.doc` 导出成 OLE Compound File，但其中唯一的
`WordDocument` 流实际是 HTML，而不是 Word 二进制正文。该流没有有效的 Word
FIB 魔数，因此 LibreOffice 会以 `source file could not be loaded` 拒绝加载。
原 `DocLoader` 只返回通用的 DOC 转 DOCX 失败信息，尽管文件里已有完整的机器可读
文本负载。

对这类原生文本文件直接执行整篇 OCR 既更慢，也更容易在案号、姓名、金额和标点上
产生错误。新增独立 HTML loader 或修改 pipeline 路由，又会与现有 DOCX 的文本、
表格、图片和页级 OCR 链路重复。

## Decision

`DocLoader` 继续把 LibreOffice 原生 DOC 转 DOCX 作为主路径。只有该转换失败后，
才检查 OLE 根存储中的 `WordDocument` 流。恢复必须同时满足：

- 文件以 OLE Compound File 魔数开头；
- 根存储中存在 `WordDocument` 流，且大小不超过 64 MiB；
- 流开头不是有效 Word FIB 魔数 `0xA5EC`；
- 流前缀可识别为 HTML 或 RTF 内容。

识别出的负载会写入带对应扩展名的临时文件。HTML 使用 LibreOffice 的
`HTML (StarWriter)` 输入过滤器，HTML 和 RTF 都显式使用 `Office Open XML Text`
输出过滤器。生成的 DOCX 继续交给现有 `DocxLoader`，因此表格、嵌入图片和页级
OCR 行为保持不变。

恢复转换的子进程使用阻断 HTTP(S) 出站的代理环境。恢复出的 HTML 会在转换前
移除 UTF-8 BOM，避免其成为可见正文。恢复成功后仍保留原始文件名、`file_type=doc`
和文件大小，并增加 `doc_recovery` 与 `doc_recovery_stream` 元数据。

LibreOffice 转换失败现在会保留有界的 stderr/stdout 与超时诊断。
`source file could not be loaded` 被视为确定性失败，不再无意义地重试三次。

## Alternatives considered

**直接让原始 OLE 字节走 HTML 输入过滤器。** 已否决，因为 LibreOffice 会把容器
字节当作正文，产生损坏的开头段落。

**新增 HTML loader 并修改 pipeline 路由。** 已否决，因为转换是旧版 DOC 加载器
内部的恢复职责，而现有 DOCX 路径已经负责文本、表格、图片和页级 OCR。

**无条件 OCR 原始 DOC 或渲染页。** 已否决，因为这些文件已有精确文本；OCR 会
增加成本，并可能在案号、姓名、金额和标点上引入错误。

**仅为该回退引入 Tika 或其他 JVM 解析器。** 已否决，因为会增加更大的运行时和
第二套解析架构。值得借鉴的原则是容器感知的负载嗅探，本变更在本地实现该原则。

**每次 DOC 加载前都先检查内嵌负载。** 已否决，因为合法 DOC 仍是绝大多数，
原生转换已是其最佳路径。

## Consequences

错误标记的 OLE-HTML 与 OLE-RTF DOC 可以恢复到正常 DOCX 链路，不需要特殊上传
接口或 OCR 绕行。未知或真正损坏的 DOC 仍会失败，但会带有转换诊断。恢复路径只在
原生转换失败后额外检查一次 OLE，并受 64 MiB 流大小上限约束。

后端新增无传递依赖的 Python 包 `olefile`。恢复逻辑保持保守：不扫描任意 OLE 流，
不尝试任意文本解码，也不添加未经验证的输入过滤器。后续负载格式必须同时提供有
文档的检测器和经过测试的 LibreOffice 过滤器映射。

## Testing

`backend/tests/test_doc_loader.py` 覆盖原生成功、原生失败诊断、确定性失败不重试、
带过滤器的转换命令、网络阻断环境、HTML/RTF 识别、FIB 拒绝、BOM 移除、元数据保留
和恢复失败路径。

两个外部钉钉导出的 DOC 样本已通过完整 `DocLoader` 路径解析。两者均按内嵌 HTML
恢复，分别得到 152 和 8335 个字符，保持 `file_type=doc`，且无需 OCR。另一个包含
base64 PNG 的 LibreOffice HTML 夹具在生成的 DOCX 中产生了 `word/media`；提取出的
RTF 负载也能转换为干净文本。
