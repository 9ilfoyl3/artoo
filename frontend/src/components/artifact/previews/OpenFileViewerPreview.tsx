import { AlertCircle, Loader2 } from 'lucide-react'
import { FileViewer } from '@open-file-viewer/react'
import {
  archivePlugin,
  audioPlugin,
  emailPlugin,
  epubPlugin,
  imagePlugin,
  ofdPlugin,
  officePlugin,
  pdfPlugin,
  textPlugin,
  videoPlugin,
  xpsPlugin,
  type PreviewFit,
} from '@open-file-viewer/core'
import '@open-file-viewer/core/style.css'
import './openFileViewerTheme.css'
import pdfWorkerSrc from 'pdfjs-dist/build/pdf.worker.mjs?url'

interface OpenFileViewerPreviewProps {
  /** 已带鉴权拉取的 blob objectURL；为 null 时显示加载中 */
  objectUrl: string | null
  /** 原始文件名，open-file-viewer 按扩展名匹配插件 */
  fileName: string
  /** 上层拉取原件失败时的错误信息 */
  error: string | null
}

/**
 * 统一预览器：open-file-viewer 官方 React 适配层。
 * 插件数组保持在模块级（稳定引用，避免 viewer 反复重建）；
 * 语法高亮 / Markdown / 邮件解析器由插件内部按需异步加载。
 */
const plugins = [
  imagePlugin(),
  videoPlugin(),
  audioPlugin(),
  pdfPlugin({ workerSrc: pdfWorkerSrc }),
  epubPlugin(),
  xpsPlugin(),
  officePlugin(),
  ofdPlugin(),
  archivePlugin(),
  emailPlugin(),
  textPlugin(),
]

/* 精简预览失败文案；其余提示沿用 OFV 内置中文 */
const previewMessages = {
  pdfPreviewFailedTitle: '无法预览',
  pdfDownload: '下载原件',
}

/* Word 类文档默认按宽度铺满（fit: width），其余类型沿用 viewer 默认策略 */
const WIDTH_FIT_EXTENSIONS = new Set(['doc', 'docx', 'docm', 'dot', 'rtf', 'odt'])

function resolveFit(fileName: string): PreviewFit | undefined {
  const extension = fileName.split('.').pop()?.toLowerCase() ?? ''
  return WIDTH_FIT_EXTENSIONS.has(extension) ? 'width' : undefined
}

function OpenFileViewerPreview({ objectUrl, fileName, error }: OpenFileViewerPreviewProps) {
  if (error) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-3 text-muted-foreground">
        <AlertCircle className="h-10 w-10 text-destructive/60" />
        <p className="text-sm">{error}</p>
      </div>
    )
  }

  if (!objectUrl) {
    return (
      <div className="h-full flex items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-primary/60" />
      </div>
    )
  }

  return (
    <div className="artoo-ofv h-full w-full">
      <FileViewer
        file={objectUrl}
        fileName={fileName}
        width="100%"
        height="100%"
        fit={resolveFit(fileName)}
        /* 调色板已由 openFileViewerTheme.css 整体映射到 artoo 主题变量，
           固定 light 仅作为 OFV 内部少量硬编码样式的确定性基底 */
        theme="light"
        locale="zh-CN"
        messages={previewMessages}
        toolbar
        plugins={plugins}
      />
    </div>
  )
}

export default OpenFileViewerPreview
