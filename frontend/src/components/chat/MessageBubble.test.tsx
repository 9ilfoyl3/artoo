import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import MessageBubble, { type Message } from './MessageBubble'

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
