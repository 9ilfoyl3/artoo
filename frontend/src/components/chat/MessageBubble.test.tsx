import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import MessageBubble, { ToolCallArguments, type Message } from './MessageBubble'

describe('MessageBubble tool progress', () => {
  it('renders bounded progress on the matching tool row', () => {
    const message: Message = {
      role: 'assistant',
      content: '',
      segments: [
        {
          type: 'tool_call',
          content: 'search_knowledge',
          toolCallId: 'call-1',
          toolName: 'search_knowledge',
          progress: {
            phase: 'document',
            completed: 4,
            total: 20,
            filename: 'manual.pdf',
            elapsed_ms: 1200,
          },
        },
      ],
    }

    render(
      <MessageBubble
        message={message}
        index={0}
        isStreaming
        isLast
        expandedRefs={new Set()}
        onToggleRef={vi.fn()}
      />
    )

    expect(screen.getAllByText(/4\/20 个文档/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/manual\.pdf/).length).toBeGreaterThan(0)
  })
})

describe('MessageBubble tool-call arguments', () => {
  it('shows normalized arguments when the tool row is expanded', async () => {
    const user = userEvent.setup()
    const message: Message = {
      role: 'assistant',
      content: '',
      segments: [
        {
          type: 'tool_call',
          content: 'list_documents',
          toolCallId: 'call-1',
          toolName: 'list_documents',
          toolArgs: { page: 2, keyword: '合同' },
          success: true,
          durationMs: 12,
        },
      ],
    }

    render(
      <MessageBubble
        message={message}
        index={0}
        isStreaming={false}
        isLast={false}
        expandedRefs={new Set()}
        onToggleRef={vi.fn()}
      />
    )

    await user.click(screen.getAllByText('文档清单')[0])

    expect(screen.getAllByText('调用参数').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/"page": 2/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/"keyword": "合同"/).length).toBeGreaterThan(0)
  })

  it('renders an explicit empty object when optional arguments are omitted', () => {
    render(<ToolCallArguments />)

    expect(screen.getByText('调用参数')).toBeInTheDocument()
    expect(screen.getByText('{}')).toBeInTheDocument()
  })
})
