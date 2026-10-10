import { describe, expect, it } from 'vitest'
import { isPreviewable } from './artifactStore'

describe('isPreviewable', () => {
  it('keeps the legacy previewable types', () => {
    const legacyTypes = ['pdf', 'jpg', 'jpeg', 'png', 'txt', 'md', 'csv']
    for (const fileType of legacyTypes) {
      expect(isPreviewable(fileType)).toBe(true)
    }
  })

  it('supports office, email, archive and ebook types via open-file-viewer', () => {
    const addedTypes = [
      'doc',
      'docx',
      'xlsx',
      'xls',
      'pptx',
      'ppt',
      'rtf',
      'odt',
      'odp',
      'eml',
      'msg',
      'zip',
      'epub',
    ]
    for (const fileType of addedTypes) {
      expect(isPreviewable(fileType)).toBe(true)
    }
  })

  it('is case-insensitive and rejects unknown or missing types', () => {
    expect(isPreviewable('DOCX')).toBe(true)
    expect(isPreviewable('exe')).toBe(false)
    expect(isPreviewable(undefined)).toBe(false)
    expect(isPreviewable(null)).toBe(false)
    expect(isPreviewable('')).toBe(false)
  })
})
