import { afterEach, describe, expect, it } from 'vitest'
import {
  getStructuredResultRenderer,
  registerStructuredResultRenderer,
  summarizeStructuredResult,
  unregisterStructuredResultRenderer,
} from '@/components/chat/structuredResultRegistry'
import {
  isStructuredToolContent,
  isValidStructuredResultKind,
} from './structuredResult'

const KIND = 'dev.artoo.test.result.v1'

afterEach(() => {
  unregisterStructuredResultRenderer(KIND)
})

describe('structured result contract', () => {
  it('accepts the generic kind/data envelope', () => {
    expect(isValidStructuredResultKind(KIND)).toBe(true)
    expect(isStructuredToolContent({ kind: KIND, data: { value: 1 } })).toBe(true)
  })

  it('rejects malformed or unsafe content', () => {
    expect(isValidStructuredResultKind('Invalid Kind')).toBe(false)
    expect(isStructuredToolContent({ kind: KIND })).toBe(false)
    expect(isStructuredToolContent({ kind: KIND, data: null, extra: true })).toBe(true)
    expect(isStructuredToolContent(null)).toBe(false)
  })
})

describe('structured result renderer registry', () => {
  it('resolves a renderer and its summary by kind', () => {
    registerStructuredResultRenderer(KIND, {
      Component: () => null,
      summarize: (content) => `summary:${content.kind}`,
    })
    const content = { kind: KIND, data: { value: 1 } }

    expect(getStructuredResultRenderer(KIND)).toBeDefined()
    expect(summarizeStructuredResult(content)).toBe(`summary:${KIND}`)
  })

  it('falls back to a generic summary for unknown kinds', () => {
    expect(summarizeStructuredResult({ kind: KIND, data: null })).toBe(
      `结构化结果 · ${KIND}`
    )
  })

  it('rejects invalid registry keys', () => {
    expect(() =>
      registerStructuredResultRenderer('Invalid Kind', { Component: () => null })
    ).toThrow('Invalid structured result kind')
  })
})
