# Agent Note: open-file-viewer artifact preview engine

Status: implemented

## Problem

Artifact 面板此前为每种文件类型维护一个定制预览器（PDF iframe、图片标签、纯
文本、Markdown、CSV），并明确不支持 Office 文档、邮件和压缩包。知识库文档和
会话附件中的这些格式无法预览，而且每新增一种格式都需要新增面板侧组件和新的
数据流分支（文本内容与 objectURL 分流）。

## Decision

Artifact 面板内的预览能力统一到 `@open-file-viewer/react` 0.1.44，配合
`@open-file-viewer/core` 0.1.44 与 `pdfjs-dist` ^4.10.38：

- `OpenFileViewerPreview` 遵循官方 React 示例：模块级注册插件（image / video /
  audio / pdf / epub / xps / office / ofd / archive / email / text），pdf.js
  worker 通过 Vite `?url` 导入，`locale: "zh-CN"`，并启用内置工具栏。
- `openFileViewerTheme.css`（作用域 `.artoo-ofv`）把 viewer 的 `--ofv-*`
  调色板整体映射到 artoo 的主题变量，颜色跟随应用主题：搜索命中从默认不透明
  高亮（会盖住字形）改为半透明主题色；PDF 画布底色、Office 面板底色和工具栏
  搜索框统一共用 muted 表面色（Word 视图因此与 PDF 底色一致，而不是 accent
  色）；下载兜底卡片弱化为轻边框卡面；并通过 `pdfPreviewFailedTitle` /
  `pdfDownload` 精简失败文案。viewer 基础主题固定为 `light`，深色表面由
  artoo 变量提供。
- Word 类文档（`doc`、`docx`、`docm`、`dot`、`rtf`、`odt`）默认
  `fit: "width"`，页面铺满面板宽度；其余格式沿用 viewer 默认策略。
- 预览组件通过 `React.lazy` 懒加载，引擎 chunk 与 worker 仅在首次预览文件时
  下载。
- `ArtifactPanel` 保留外层职责（右侧滑入容器、带鉴权拉取原件、objectURL
  生命周期管理与 revoke、下载、关闭），现在统一把 blob objectURL 和原始文件名
  传给预览器。按类型抽取文本的分支和五个按类型定制的预览器组件全部移除。
- `PREVIEWABLE_TYPES` 扩展到已注册的预览面：PDF、常见图片、txt/Markdown/CSV、
  Word/Excel/PowerPoint（含旧版与 OpenDocument 格式）、RTF、EML/MSG、ZIP 与
  EPUB。媒体、3D、CAD、GIS 插件仍在 viewer 中注册，但暂不开放预览入口。

## Alternatives considered

**在现有按类型注册的预览器体系中逐格式引入依赖库（mammoth、SheetJS 等）。**
否决：每种格式都会增加面板侧代码和数据流分支，且依旧缺少统一的容器、工具栏、
状态模型与 fallback 路径。

**全部走服务端转 PDF（例如 LibreOffice）。** 否决作为主路径：每次预览都会增加
一个常驻服务和上传环节。viewer 的可选 `officePlugin({ convert })` 钩子保留了
这一能力，未来可零改动接入，用于高保真 Office 渲染。

**保留定制预览器、只新增 Office 预览。** 否决：本次目标是单一容器契约；两套
预览栈会让不同文件类型的状态、工具栏和 fallback 行为不一致。

## Consequences

可预览类型从 7 种增加到 30 种，且无需后端改动。引擎以单个懒加载 chunk 发布
（当前构建约 624 KB，gzip 约 203 KB），pdf worker 按需加载，首屏体积不变。
DOCX/PPTX 渲染是 HTML 级还原而非像素级；复杂文档未来可能需要服务端转换钩子。
Vite 会为 MSG 解析器的传递依赖将 `buffer` 外部化，因此 `.msg` 预览可能降级，
而 `.eml`（postal-mime）不受影响。该库处于 0.1.x：`@open-file-viewer/react`
锁定精确的 core 版本，升级时两个包必须同步升版。`OpenFileViewerPreview` 是
调整插件或 viewer 选项的唯一集成点。

## Testing

`frontend/` 下 `npm test` 通过（34 个测试，含新增的 `artifactStore`
可预览类型覆盖），`npm run build`（tsc + vite build）成功。
