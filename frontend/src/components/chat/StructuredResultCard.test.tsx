import { afterEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StructuredResultCard } from './StructuredResultCard'
import {
  registerStructuredResultRenderer,
  unregisterStructuredResultRenderer,
} from './structuredResultRegistry'

const KIND = 'dev.artoo.test.card.v1'

afterEach(() => {
  unregisterStructuredResultRenderer(KIND)
})

describe('StructuredResultCard', () => {
  it('renders a registered component by kind', () => {
    registerStructuredResultRenderer(KIND, {
      Component: ({ content }) => <div>custom:{String(content.data)}</div>,
    })

    render(
      <StructuredResultCard
        content={{ kind: KIND, data: 'value' }}
        toolName="test_tool"
      />
    )

    expect(screen.getByText('custom:value')).toBeInTheDocument()
  })

  it('falls back to escaped JSON for unknown kinds', () => {
    render(
      <StructuredResultCard
        content={{ kind: KIND, data: { value: '<unsafe>' } }}
        toolName="test_tool"
      />
    )

    expect(screen.getByText(KIND)).toBeInTheDocument()
    expect(screen.getByText(/<unsafe>/)).toBeInTheDocument()
  })

  it('shows generic feedback in the fallback card', () => {
    render(
      <StructuredResultCard
        content={{
          kind: KIND,
          data: {},
          feedback: {
            status: 'needs_evidence',
            message: 'Provide a source.',
          },
        }}
        toolName="test_tool"
      />
    )

    expect(screen.getByText('needs_evidence: Provide a source.')).toBeInTheDocument()
  })

})
