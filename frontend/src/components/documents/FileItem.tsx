import { useState, useEffect } from 'react'
import {
  Loader2,
  FileText,
  File,
  FileSpreadsheet,
  Presentation,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { documentApi } from '@/lib/api'

// 文档数据类型
export interface DocumentItem {
  id: string
  kb_id: string
  filename: string
  file_type: string
  file_size: number
  status: string
  error_message: string | null
  chunk_count: number
  progress: number
  progress_message: string | null
  created_at: string
  folder_id?: string | null
}

// 本地上传中的文件
export interface UploadingFile {
  id: string
  filename: string
  file_size: number
  status: 'uploading' | 'uploaded'
}

// 合并后的文件类型
export interface MergedFile {
  id: string
  filename: string
  file_type?: string
  file_size: number
  status: string
  error_message: string | null
  chunk_count: number
  progress: number
  progress_message: string | null
  isLocal: boolean
  created_at?: string | null
}

interface FileItemProps {
  doc: MergedFile
  isSelected: boolean
  onSelect: (id: string) => void
  onRetry?: (id: string) => void
}

// 根据文件类型返回对应图标
function FileIcon({ filename, className }: { filename: string; className?: string }) {
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  const iconClass = className || 'h-6 w-6'

  if (['pdf'].includes(ext)) {
    return <FileText className={`${iconClass} text-red-400`} />
  }
  if (['doc', 'docx'].includes(ext)) {
    return <FileText className={`${iconClass} text-blue-400`} />
  }
  if (['xls', 'xlsx', 'csv'].includes(ext)) {
    return <FileSpreadsheet className={`${iconClass} text-green-500`} />
  }
  if (['ppt', 'pptx'].includes(ext)) {
    return <Presentation className={`${iconClass} text-orange-400`} />
  }
  if (['md', 'txt'].includes(ext)) {
    return <FileText className={`${iconClass} text-muted-foreground`} />
  }
  return <File className={`${iconClass} text-muted-foreground`} />
}

// 状态标签
export function statusLabel(status: string) {
  switch (status) {
    case 'completed': return '已完成'
    case 'processing': return '解析中'
    case 'pending': return '排队中'
    case 'failed': return '失败'
    case 'uploading': return '上传中'
    default: return status
  }
}

// 状态颜色
export function statusColor(status: string) {
  switch (status) {
    case 'completed': return 'bg-green-100 text-green-700 border-green-200'
    case 'processing': return 'bg-yellow-100 text-yellow-700 border-yellow-200'
    case 'pending': return 'bg-blue-100 text-blue-700 border-blue-200'
    case 'failed': return 'bg-red-100 text-red-700 border-red-200'
    case 'uploading': return 'bg-orange-100 text-orange-700 border-orange-200'
    default: return ''
  }
}

// 格式化文件大小
export function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// 截断文件名
function truncateFilename(name: string, maxLen = 18) {
  if (name.length <= maxLen) return name
  const ext = name.lastIndexOf('.') > 0 ? name.slice(name.lastIndexOf('.')) : ''
  const base = name.slice(0, name.lastIndexOf('.') > 0 ? name.lastIndexOf('.') : name.length)
  const keep = maxLen - ext.length - 3
  if (keep <= 0) return name.slice(0, maxLen - 3) + '...'
  return base.slice(0, keep) + '...' + ext
}

// 获取文件扩展名
function getFileExt(filename: string) {
  return filename.split('.').pop()?.toLowerCase() || ''
}

// 文件缩略图预览
function FileThumbnail({ filename, status, docId }: { filename: string; status: string; docId?: string }) {
  const [thumbUrl, setThumbUrl] = useState<string | null>(null)
  const [imgFailed, setImgFailed] = useState(false)

  // 是否需要加载缩略图：图片/PDF/Markdown/TXT（md/txt 的缩略图为封面图或文字卡片），
  // 且非本地上传项、非上传中/排队中状态
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  const canPreview =
    ['jpg', 'jpeg', 'png', 'pdf', 'md', 'txt'].includes(ext) &&
    !!docId &&
    !docId.startsWith('local_') &&
    status !== 'uploading' &&
    status !== 'pending'

  // 通过 fetch 带 token 拉取缩略图，转为 blob objectURL 供 <img> 使用；
  // 卸载或 docId 变化时释放上一个 objectURL，避免内存泄漏。
  useEffect(() => {
    if (!canPreview || !docId) return
    let revoked = false
    let url: string | null = null
    documentApi
      .preview(docId)
      .then((objectUrl) => {
        if (revoked) {
          URL.revokeObjectURL(objectUrl)
          return
        }
        url = objectUrl
        setThumbUrl(objectUrl)
      })
      .catch(() => setImgFailed(true))
    return () => {
      revoked = true
      if (url) URL.revokeObjectURL(url)
    }
  }, [canPreview, docId])

  if (status === 'uploading') {
    return (
      <div className="w-full h-full flex items-center justify-center">
        <Loader2 className="h-6 w-6 text-primary/60 animate-spin" />
      </div>
    )
  }

  // 缩略图加载成功则展示
  if (canPreview && thumbUrl && !imgFailed) {
    return (
      <div className="w-full h-full">
        <img
          src={thumbUrl}
          alt={filename}
          className="w-full h-full object-cover rounded-sm"
          onError={() => setImgFailed(true)}
        />
      </div>
    )
  }

  // 降级：骨架线在上，图标在下
  return (
    <div className="w-full h-full flex flex-col items-center p-2.5 pt-3">
      {/* 骨架线条 */}
      <div className="flex flex-col gap-[3px] w-full px-1">
        <div className="h-[2px] bg-muted-foreground/12 rounded-full w-full" />
        <div className="h-[2px] bg-muted-foreground/12 rounded-full w-[80%]" />
        <div className="h-[2px] bg-muted-foreground/12 rounded-full w-[90%]" />
        <div className="h-[2px] bg-muted-foreground/12 rounded-full w-[60%]" />
        <div className="h-[2px] bg-muted-foreground/12 rounded-full w-full" />
      </div>
      {/* 文件类型图标 - 线条下方 */}
      <div className="flex items-center justify-center mt-auto pb-0.5">
        <FileIcon filename={filename} className="h-5 w-5" />
      </div>
    </div>
  )
}

// Finder 风格文件项
function FileItem({ doc, isSelected, onSelect, onRetry }: FileItemProps) {
  const ext = getFileExt(doc.filename)

  return (
    <div
      className={`group relative flex flex-col items-center rounded-lg px-2 py-3 transition-all duration-150 cursor-default select-none ${
        isSelected ? 'bg-primary/8 ring-1 ring-primary/30' : 'hover:bg-muted/40'
      }`}
      onClick={(e) => { e.stopPropagation(); onSelect(doc.id) }}
    >
      {/* 文件缩略图 */}
      <div className="w-16 h-20 rounded bg-white border border-border/60 flex items-center justify-center mb-2.5 relative overflow-hidden shadow-sm">
        <FileThumbnail filename={doc.filename} status={doc.status} docId={doc.id} />

        {/* 文件类型角标 - 右上角外侧 */}
        {doc.status !== 'uploading' && (
          <div className="absolute -top-0.5 -right-0.5">
            <span className="text-[9px] font-semibold uppercase px-1 py-0.5 rounded bg-muted text-muted-foreground leading-none border border-border/40">
              {ext}
            </span>
          </div>
        )}

        {/* 处理中：底部进度条 + 旋转图标，简洁直观 */}
        {doc.status === 'processing' && (
          <>
            <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 pointer-events-none">
              <Loader2 className="h-4 w-4 text-primary/70 animate-spin" />
            </div>
            <div className="absolute bottom-0 inset-x-0">
              <div className="h-1.5 bg-muted/60 rounded-b overflow-hidden">
                <div
                  className="h-full bg-primary transition-all duration-500 ease-out"
                  style={{ width: `${doc.progress || 0}%` }}
                />
              </div>
            </div>
          </>
        )}
        {doc.status === 'pending' && (
          <div className="absolute bottom-1 inset-x-1 flex justify-center">
            <Badge variant="outline" className={`text-[8px] px-1.5 py-0 leading-tight ${statusColor(doc.status)}`}>
              {statusLabel(doc.status)}
            </Badge>
          </div>
        )}

        {/* 失败状态 - 图标中间显示失败+重试（hover 显示失败原因） */}
        {doc.status === 'failed' && (
          <div
            className="absolute inset-0 flex flex-col items-center justify-center bg-white/80 rounded"
            title={doc.error_message || doc.progress_message || '处理失败'}
          >
            <span className="text-[9px] text-red-500 font-medium">失败</span>
            {onRetry && (
              <button
                className="text-[9px] text-red-500 hover:text-red-700 cursor-pointer underline mt-0.5"
                onClick={(e) => { e.stopPropagation(); onRetry(doc.id) }}
              >
                点击重试
              </button>
            )}
          </div>
        )}
      </div>

      {/* 文件名 + 切片数 tag */}
      <p className="text-[11px] text-center text-foreground leading-tight w-full px-0.5 line-clamp-2" title={doc.filename}>
        {truncateFilename(doc.filename)}
        {doc.status === 'completed' && doc.chunk_count > 0 && (
          <span className="inline-block ml-1 text-[9px] font-medium text-primary/80 bg-primary/8 rounded px-1 py-0 leading-normal align-middle">
            {doc.chunk_count}片
          </span>
        )}
      </p>

      {/* 文件大小 */}
      <p className="text-[9px] text-muted-foreground mt-0.5">
        {formatSize(doc.file_size)}
      </p>
    </div>
  )
}

export default FileItem
