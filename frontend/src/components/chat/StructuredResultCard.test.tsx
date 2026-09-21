import { afterEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StructuredResultCard } from './StructuredResultCard'
import {
  registerStructuredResultRenderer,
  unregisterStructuredResultRenderer,
} from './structuredResultRegistry'
import { LEGAL_PROVISION_SEARCH_RESULT_KIND } from './structured-results/legalProvisionSearchResult'

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

  it('renders an empty legal provision search result', () => {
    render(
      <StructuredResultCard
        content={{
          kind: LEGAL_PROVISION_SEARCH_RESULT_KIND,
          data: {
            search_scope: 'global',
            query: '民法典第146条',
            total: 0,
            has_more: false,
            items: [],
          },
        }}
        toolName="search_legal_provisions"
        success
      />
    )

    expect(screen.getByText('民法典第146条')).toBeInTheDocument()
    expect(screen.getByText('全局库')).toBeInTheDocument()
    expect(screen.getByText('未找到匹配法条')).toBeInTheDocument()
  })

  it('renders legal provision items with snake_case fields', () => {
    render(
      <StructuredResultCard
        content={{
          kind: LEGAL_PROVISION_SEARCH_RESULT_KIND,
          data: {
            search_scope: 'global',
            query: '民法典第146条',
            total: 1,
            items: [{
              law_name: '中华人民共和国民法典',
              article_label: '第一百四十六条',
              content: '行为人与相对人以虚假的意思表示实施的民事法律行为无效。',
              validity_status: '现行有效',
              source: 'global',
            }],
          },
        }}
        toolName="search_legal_provisions"
        success
      />
    )

    expect(screen.getByText('中华人民共和国民法典')).toBeInTheDocument()
    expect(screen.getByText('第一百四十六条')).toBeInTheDocument()
    expect(screen.getByText(/行为人与相对人/)).toBeInTheDocument()
    expect(screen.getByText(/现行有效/)).toBeInTheDocument()
  })
})
