import { getStructuredResultRenderer } from '@/components/chat/structuredResultRegistry'
import '@/components/chat/structured-results/legalProvisionSearchResult'
import type { StructuredToolContent } from '@/lib/structuredResult'

interface StructuredResultCardProps {
  content: StructuredToolContent
  toolName: string
  success?: boolean
}

function StructuredResultFallback({
  content,
}: {
  content: StructuredToolContent
}) {
  let formatted = 'null'
  try {
    formatted = JSON.stringify(content.data, null, 2) ?? 'null'
  } catch {
    formatted = '[unserializable data]'
  }

  return (
    <div className="space-y-1.5">
      <div className="font-mono text-[10px] text-muted-foreground">{content.kind}</div>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border/50 bg-muted/30 p-2 text-[11px] leading-relaxed text-foreground/80">
        {formatted}
      </pre>
    </div>
  )
}

export function StructuredResultCard({
  content,
  toolName,
  success,
}: StructuredResultCardProps) {
  const renderer = getStructuredResultRenderer(content.kind)
  if (!renderer) {
    return <StructuredResultFallback content={content} />
  }

  const Renderer = renderer.Component
  return <Renderer content={content} toolName={toolName} success={success} />
}
