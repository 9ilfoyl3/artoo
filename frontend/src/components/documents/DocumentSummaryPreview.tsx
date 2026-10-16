import { useQuery } from '@tanstack/react-query'
import { Clock3, FileText, Loader2, Sparkles } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { documentApi } from '@/lib/api'

interface DocumentSummaryPreviewProps {
  docId: string
  filename: string
  documentStatus: string
}

function PendingSummary() {
  return (
    <div className="flex items-start gap-3 rounded-md border border-dashed bg-muted/25 p-3">
      <Clock3 className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0">
        <p className="text-sm font-medium text-foreground">该文件暂未生成摘要内容</p>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
          摘要由后台独立异步生成，不影响文件解析、入库、检索和其他操作。
        </p>
      </div>
    </div>
  )
}

/**
 * Structured, read-only summary artifact preview.
 *
 * This component only reads ``/documents/{id}/summary``. Generation is
 * scheduled by the ingestion pipeline after parsing/indexing completes and is
 * consumed by a separate worker lane, so opening or hovering this preview can
 * never trigger or block upload work.
 */
export default function DocumentSummaryPreview({
  docId,
  filename,
  documentStatus,
}: DocumentSummaryPreviewProps) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['document-summary', docId],
    queryFn: () => documentApi.summary(docId),
    staleTime: 30_000,
    retry: false,
  })

  const ready =
    data?.status === 'completed' &&
    (data.summary?.trim() || data.gist?.trim())

  return (
    <div className="w-[360px] max-w-[calc(100vw-2rem)] overflow-hidden">
      <div className="flex items-start gap-2.5 border-b bg-muted/25 px-3.5 py-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border bg-background text-primary shadow-sm">
          <FileText className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold leading-5" title={filename}>
            {filename}
          </p>
          <p className="mt-0.5 text-[11px] text-muted-foreground">
            文档摘要 · 异步生成
          </p>
        </div>
        {ready && (
          <Badge variant="outline" className="shrink-0 gap-1 border-primary/20 bg-primary/5 text-primary">
            <Sparkles className="h-3 w-3" />
            已生成
          </Badge>
        )}
      </div>

      <div className="max-h-[min(420px,70vh)] overflow-auto px-3.5 py-3">
        {isLoading ? (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            正在读取摘要…
          </div>
        ) : isError || !ready ? (
          <PendingSummary />
        ) : (
          <div className="space-y-3">
            {(data.doc_type || data.topics.length > 0) && (
              <div className="flex flex-wrap items-center gap-1.5">
                {data.doc_type && (
                  <Badge variant="secondary" className="font-normal">
                    {data.doc_type}
                  </Badge>
                )}
                {data.topics.slice(0, 5).map((topic) => (
                  <span
                    key={topic}
                    className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground"
                  >
                    {topic}
                  </span>
                ))}
              </div>
            )}

            {data.gist && data.gist !== data.summary && (
              <section>
                <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                  一句话摘要
                </h4>
                <p className="text-xs leading-relaxed text-foreground/85">
                  {data.gist}
                </p>
              </section>
            )}

            <section>
              <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                核心内容
              </h4>
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-foreground">
                {data.summary || data.gist}
              </p>
            </section>

            {data.typical_question && (
              <section className="rounded-md bg-muted/35 px-2.5 py-2">
                <h4 className="mb-1 text-[11px] font-semibold text-muted-foreground">
                  典型问题
                </h4>
                <p className="text-xs leading-relaxed text-foreground/80">
                  {data.typical_question}
                </p>
              </section>
            )}
          </div>
        )}

        {documentStatus !== 'completed' && (
          <p className="mt-3 border-t pt-2 text-[11px] text-muted-foreground">
            文件当前状态：{documentStatus}
          </p>
        )}
      </div>
    </div>
  )
}
