import { BookOpen, Search } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { registerStructuredResultRenderer } from '@/components/chat/structuredResultRegistry'
import type { StructuredToolContent } from '@/lib/structuredResult'

export const LEGAL_PROVISION_SEARCH_RESULT_KIND =
  'legal_provision_search_result.v1'

interface LegalProvisionItem {
  lawName: string
  articleLabel: string
  content: string
  validityStatus?: string
  source?: string
}

interface LegalProvisionSearchData {
  searchScope?: string
  query?: string
  total?: number
  hasMore?: boolean
  items: LegalProvisionItem[]
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function readString(
  source: Record<string, unknown>,
  ...keys: string[]
): string | undefined {
  for (const key of keys) {
    const value = source[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return undefined
}

function readNumber(
  source: Record<string, unknown>,
  ...keys: string[]
): number | undefined {
  for (const key of keys) {
    const value = source[key]
    if (typeof value === 'number' && Number.isFinite(value)) return value
  }
  return undefined
}

function parseLegalProvisionData(value: unknown): LegalProvisionSearchData {
  const data = asRecord(value) || {}
  const rawItems = Array.isArray(data.items) ? data.items : []
  const items = rawItems.flatMap((value): LegalProvisionItem[] => {
    const item = asRecord(value)
    if (!item) return []
    const lawName = readString(item, 'lawName', 'law_name', 'law') || ''
    const articleLabel = readString(
      item,
      'articleLabel',
      'article_label',
      'article'
    ) || ''
    const content = readString(item, 'content', 'text', 'provision') || ''
    if (!lawName && !articleLabel && !content) return []
    return [{
      lawName,
      articleLabel,
      content,
      validityStatus: readString(
        item,
        'validityStatus',
        'validity_status',
        'status'
      ),
      source: readString(item, 'source', 'sourceType', 'source_type'),
    }]
  })

  return {
    searchScope: readString(data, 'search_scope', 'searchScope', 'scope'),
    query: readString(data, 'query'),
    total: readNumber(data, 'total'),
    hasMore: data.has_more === true || data.hasMore === true,
    items,
  }
}

function scopeLabel(scope?: string): string {
  if (!scope) return '法条库'
  if (scope === 'global') return '全局库'
  if (scope === 'personal') return '个人库'
  return scope
}

function isValidStatus(status?: string): boolean {
  return Boolean(status && /有效|现行/.test(status))
}

function LegalProvisionSearchResult({
  content,
}: {
  content: StructuredToolContent
}) {
  const result = parseLegalProvisionData(content.data)
  const total = result.total ?? result.items.length

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <Search className="h-3 w-3" />
          {result.query || '法条检索'}
        </span>
        <span className="text-border">·</span>
        <span>{scopeLabel(result.searchScope)}</span>
        <span className="text-border">·</span>
        <span>{total} 条</span>
        {result.hasMore && <span className="text-primary/80">还有更多结果</span>}
      </div>

      {result.items.length === 0 ? (
        <div className="flex items-center gap-2 rounded-md border border-dashed border-border/60 bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
          <BookOpen className="h-3.5 w-3.5" />
          未找到匹配法条
        </div>
      ) : (
        <div className="space-y-2">
          {result.items.map((item, index) => (
            <article
              key={`${item.lawName}-${item.articleLabel}-${index}`}
              className="rounded-md border border-border/60 bg-background/70 p-2.5"
            >
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-xs font-medium text-foreground">
                  {item.lawName || '未命名法律'}
                </span>
                {item.articleLabel && (
                  <Badge variant="secondary" className="px-1.5 py-0 text-[10px]">
                    {item.articleLabel}
                  </Badge>
                )}
                {item.validityStatus && (
                  <Badge
                    variant={isValidStatus(item.validityStatus) ? 'secondary' : 'outline'}
                    className="px-1.5 py-0 text-[10px]"
                  >
                    {item.validityStatus}
                  </Badge>
                )}
              </div>
              {item.content && (
                <p className="mt-1.5 whitespace-pre-wrap text-xs leading-relaxed text-foreground/80">
                  {item.content}
                </p>
              )}
              {item.source && (
                <div className="mt-1.5 text-[10px] text-muted-foreground/70">
                  来源：{item.source}
                </div>
              )}
            </article>
          ))}
        </div>
      )}
    </div>
  )
}

registerStructuredResultRenderer(LEGAL_PROVISION_SEARCH_RESULT_KIND, {
  Component: LegalProvisionSearchResult,
  summarize: (content) => {
    const result = parseLegalProvisionData(content.data)
    return `检索法条 · ${result.query || '全部'} · ${scopeLabel(result.searchScope)}`
  },
})
