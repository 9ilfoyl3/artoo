import type { ComponentType } from 'react'
import { isValidStructuredResultKind, type StructuredToolContent } from '@/lib/structuredResult'

export interface StructuredResultRendererProps {
  content: StructuredToolContent
  toolName: string
  success?: boolean
}

export interface StructuredResultRenderer {
  Component: ComponentType<StructuredResultRendererProps>
  summarize?: (content: StructuredToolContent) => string | null
}

const rendererRegistry = new Map<string, StructuredResultRenderer>()

export function registerStructuredResultRenderer(
  kind: string,
  renderer: StructuredResultRenderer
): void {
  if (!isValidStructuredResultKind(kind)) {
    throw new Error(`Invalid structured result kind: ${kind}`)
  }
  rendererRegistry.set(kind, renderer)
}

export function unregisterStructuredResultRenderer(kind: string): void {
  rendererRegistry.delete(kind)
}

export function getStructuredResultRenderer(
  kind: string
): StructuredResultRenderer | undefined {
  return rendererRegistry.get(kind)
}

export function summarizeStructuredResult(content: StructuredToolContent): string {
  return (
    getStructuredResultRenderer(content.kind)?.summarize?.(content) ||
    `结构化结果 · ${content.kind}`
  )
}
