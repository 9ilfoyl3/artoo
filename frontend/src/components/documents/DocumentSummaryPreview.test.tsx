import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { documentApi } from '@/lib/api'

import DocumentSummaryPreview from './DocumentSummaryPreview'

vi.mock('@/lib/api', () => ({
  documentApi: { summary: vi.fn() },
}))

const mockedSummary = vi.mocked(documentApi.summary)

function renderPreview() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
  return render(
    <QueryClientProvider client={client}>
      <DocumentSummaryPreview
        docId="doc-1"
        filename="manual.pdf"
        uploadedAt="2026-10-16T03:39:00Z"
      />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('DocumentSummaryPreview', () => {
  it('renders filename, upload time and the summary artifact', async () => {
    mockedSummary.mockResolvedValue({
      doc_id: 'doc-1',
      status: 'completed',
      summary: '这是一份操作手册，介绍安装、配置与故障排查。',
      gist: '操作手册摘要。',
      topics: ['安装', '配置'],
      doc_type: '操作手册',
      typical_question: '如何完成安装？',
      content_version: 'v1',
      chunk_count: 3,
      generated_at: '2026-10-16T00:00:00Z',
    })

    renderPreview()

    expect(screen.getByText('manual.pdf')).toBeInTheDocument()
    expect(screen.getByText(/上传时间/)).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByText('操作手册摘要。')).toBeInTheDocument()
    )
  })

  it('shows the explicit empty state when no artifact exists', async () => {
    mockedSummary.mockResolvedValue({
      doc_id: 'doc-1',
      status: 'missing',
      summary: null,
      gist: null,
      topics: [],
      doc_type: null,
      typical_question: null,
      content_version: null,
      chunk_count: 0,
      generated_at: null,
    })

    renderPreview()

    await waitFor(() =>
      expect(screen.getByText('该内容暂未生成摘要')).toBeInTheDocument()
    )
  })
})
