import { useQuery } from '@tanstack/react-query'

import { documentApi } from '@/lib/api'

interface DocumentSummaryPreviewProps {
  docId: string
  filename: string
  /** 上传时间（由文件列表项透传，卡片第二行展示） */
  uploadedAt?: string | null
}

// 格式化上传时间：MM/DD HH:mm
function formatUploadTime(iso?: string | null) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/**
 * Structured, read-only summary artifact preview.
 *
 * 简洁卡片：文件名 / 上传时间 / 摘要内容（或生成中、未生成占位），悬浮与
 * 摘要预览对话框共用。This component only reads ``/documents/{id}/summary``.
 * Generation is scheduled by the ingestion pipeline after parsing/indexing
 * completes and is consumed by a separate worker lane, so opening or hovering
 * this preview can never trigger or block upload work.
 */
export default function DocumentSummaryPreview({
  docId,
  filename,
  uploadedAt,
}: DocumentSummaryPreviewProps) {
  const { data, isLoading } = useQuery({
    queryKey: ['document-summary', docId],
    queryFn: () => documentApi.summary(docId),
    staleTime: 30_000,
    retry: false,
  })

  const summaryText =
    data?.status === 'pending' || data?.status === 'processing'
      ? '摘要生成中…'
      : data?.gist?.trim() || data?.summary?.trim() || '该内容暂未生成摘要'

  return (
    <div className="w-full px-3.5 py-3">
      <p className="text-sm font-medium text-foreground truncate" title={filename}>
        {filename}
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        上传时间：{formatUploadTime(uploadedAt)}
      </p>
      <p className="mt-1 text-xs leading-relaxed text-muted-foreground line-clamp-3">
        {isLoading ? '正在读取摘要…' : summaryText}
      </p>
    </div>
  )
}
