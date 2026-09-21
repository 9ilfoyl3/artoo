export interface StructuredToolContent {
  kind: string
  data: unknown
}

const STRUCTURED_RESULT_KIND_RE = /^[a-z][a-z0-9._-]{2,127}$/

export function isValidStructuredResultKind(kind: string): boolean {
  return STRUCTURED_RESULT_KIND_RE.test(kind)
}

export function isStructuredToolContent(value: unknown): value is StructuredToolContent {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const candidate = value as Record<string, unknown>
  return (
    typeof candidate.kind === 'string' &&
    isValidStructuredResultKind(candidate.kind) &&
    Object.prototype.hasOwnProperty.call(candidate, 'data')
  )
}
