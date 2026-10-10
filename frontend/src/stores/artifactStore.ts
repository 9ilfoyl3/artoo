import { create } from 'zustand'

/**
 * Artifact 面板全局状态。
 *
 * Artifact 是承载不同类型文件预览的公用侧栏：从右侧滑入、占用布局空间（非浮层）。
 * 通过全局 store 暴露 open/close，任意页面（知识库文档、会话附件等）都能触发预览，
 * 而无需层层透传 props，保持数据流清晰。
 */

/** 预览来源：知识库文档 / 会话附件。决定取原件的接口。 */
export type ArtifactSource = 'document' | 'session-file'

export interface ArtifactTarget {
  /** 文档 / 会话文件 ID */
  id: string
  /** 原始文件名（用于标题与下载名） */
  filename: string
  /** 文件类型扩展名（小写，无点），如 pdf / docx */
  fileType: string
  /** 预览来源 */
  source: ArtifactSource
  /** session-file 来源时必填：所属会话 ID */
  sessionId?: string
}

interface ArtifactState {
  open: boolean
  target: ArtifactTarget | null
  /** 打开并预览某文件（替换当前内容） */
  openArtifact: (target: ArtifactTarget) => void
  /** 关闭面板（保留 target 以便关闭动画期间内容不闪烁，由组件在动画结束后清理） */
  closeArtifact: () => void
}

export const useArtifactStore = create<ArtifactState>((set) => ({
  open: false,
  target: null,
  openArtifact: (target) => set({ open: true, target }),
  closeArtifact: () => set({ open: false }),
}))

/**
 * 当前可被 artifact 预览的文件类型。
 * 与 OpenFileViewerPreview 注册的插件能力保持一致（媒体 / 3D / GIS 类暂不开放入口）。
 */
const PREVIEWABLE_TYPES = new Set([
  'pdf',
  // 图片
  'jpg',
  'jpeg',
  'png',
  'gif',
  'webp',
  'bmp',
  'svg',
  // 文本
  'txt',
  'md',
  'csv',
  // Office 文档
  'doc',
  'docx',
  'docm',
  'dot',
  'rtf',
  'odt',
  // 表格
  'xls',
  'xlsx',
  'xlsm',
  'xlsb',
  // 演示文稿
  'ppt',
  'pps',
  'pptx',
  'pptm',
  'odp',
  // 邮件 / 压缩包 / 电子书
  'eml',
  'msg',
  'zip',
  'epub',
])

export function isPreviewable(fileType: string | undefined | null): boolean {
  if (!fileType) return false
  return PREVIEWABLE_TYPES.has(fileType.toLowerCase())
}
