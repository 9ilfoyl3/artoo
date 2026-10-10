import { lazy, Suspense, useEffect, useState } from 'react'
import { X, Download, FileText, Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { documentApi, sessionFileApi } from '@/lib/api'
import { useArtifactStore, type ArtifactTarget } from '@/stores/artifactStore'

// 预览引擎整体懒加载：open-file-viewer 与 pdf worker 只在首次打开面板时下载。
const OpenFileViewerPreview = lazy(() => import('./previews/OpenFileViewerPreview'))

const PANEL_WIDTH = 'w-[clamp(420px,42vw,860px)]'

function PanelSpinner() {
  return (
    <div className="h-full flex items-center justify-center">
      <Loader2 className="h-6 w-6 animate-spin text-primary/60" />
    </div>
  )
}

/**
 * Artifact 公用预览面板。
 *
 * 设计要点：
 * - 外层职责不变：右侧滑入、占用布局空间（非浮层）、统一按来源（document /
 *   session-file）带鉴权拉取原件为 blob objectURL，切换 / 卸载时 revoke，
 *   头部提供下载与关闭。
 * - 内层预览能力统一下放给 open-file-viewer（React 适配层）：按插件匹配渲染
 *   PDF / 图片 / 文本 / Markdown / CSV / Office / 邮件 / 压缩包等格式，
 *   本组件不再按 fileType 维护各自的预览器。
 *   数据流单向：store(target) → fetch → objectURL → OpenFileViewerPreview。
 */

function ArtifactPanel() {
  const { open, target, closeArtifact } = useArtifactStore()

  // 关闭动画期间保留内容，动画结束后再清空，避免内容在滑出过程中闪烁/塌陷。
  const [mountedTarget, setMountedTarget] = useState<ArtifactTarget | null>(target)
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // 同步 target：打开或切换文件时立即更新挂载内容
  useEffect(() => {
    if (target) setMountedTarget(target)
  }, [target])

  // 拉取原件为 blob objectURL；切换/卸载时 revoke，避免内存泄漏
  useEffect(() => {
    if (!open || !target) return
    let revoked = false
    let createdUrl: string | null = null
    setObjectUrl(null)
    setError(null)

    const fetchRaw =
      target.source === 'session-file' && target.sessionId
        ? sessionFileApi.rawFile(target.sessionId, target.id)
        : documentApi.rawFile(target.id)

    fetchRaw
      .then((url) => {
        if (revoked) {
          URL.revokeObjectURL(url)
          return
        }
        createdUrl = url
        setObjectUrl(url)
      })
      .catch((e) => {
        if (!revoked) setError(e instanceof Error ? e.message : '加载失败')
      })

    return () => {
      revoked = true
      if (createdUrl) URL.revokeObjectURL(createdUrl)
    }
  }, [open, target])

  // 关闭动画结束后清理挂载内容（与 transition 时长一致）
  function handleTransitionEnd() {
    if (!open) {
      setMountedTarget(null)
      setObjectUrl(null)
      setError(null)
    }
  }

  function renderPreview() {
    if (!mountedTarget) return null
    return (
      <Suspense fallback={<PanelSpinner />}>
        <OpenFileViewerPreview
          objectUrl={objectUrl}
          fileName={mountedTarget.filename}
          error={error}
        />
      </Suspense>
    )
  }

  const canDownload = !!objectUrl && !!mountedTarget

  return (
    <div
      className={cn(
        'shrink-0 h-full overflow-hidden border-l border-border bg-card',
        'transition-[width] duration-300 ease-in-out',
        open ? PANEL_WIDTH : 'w-0'
      )}
      onTransitionEnd={handleTransitionEnd}
      aria-hidden={!open}
    >
      {/* 固定宽度内层：面板收起时不被压缩内容，保证滑动平滑 */}
      <div className={cn('h-full flex flex-col', PANEL_WIDTH)}>
        {/* 头部 */}
        <div className="flex items-center gap-2 px-4 h-12 border-b border-border shrink-0">
          <FileText className="h-4 w-4 text-muted-foreground shrink-0" />
          <span className="flex-1 truncate text-sm font-medium" title={mountedTarget?.filename}>
            {mountedTarget?.filename ?? '预览'}
          </span>
          {canDownload && (
            <a
              href={objectUrl!}
              download={mountedTarget!.filename}
              className="h-7 w-7 rounded-md flex items-center justify-center text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              title="下载原件"
            >
              <Download className="h-4 w-4" />
            </a>
          )}
          <button
            onClick={closeArtifact}
            className="h-7 w-7 rounded-md flex items-center justify-center text-muted-foreground hover:bg-muted hover:text-foreground transition-colors cursor-pointer"
            title="关闭预览"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* 预览内容区 */}
        <div className="flex-1 min-h-0">{renderPreview()}</div>
      </div>
    </div>
  )
}

export default ArtifactPanel
