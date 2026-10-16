import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import TaskList from './TaskList'

describe('TaskList', () => {
  it('renders a compact progress bar and expands to show every step', async () => {
    const user = userEvent.setup()
    render(
      <TaskList
        todos={[
          { content: '收集资料', status: 'completed' },
          { content: '分析证据', status: 'in_progress' },
          { content: '生成答案', status: 'pending' },
        ]}
      />
    )

    expect(screen.getByText('任务')).toBeInTheDocument()
    expect(screen.getByText('1/3')).toBeInTheDocument()
    expect(screen.getByText('分析证据')).toBeInTheDocument()
    expect(screen.queryByText('收集资料')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { expanded: false }))

    expect(screen.getByText('收集资料')).toBeInTheDocument()
    expect(screen.getByText('生成答案')).toBeInTheDocument()
    expect(screen.getByRole('button', { expanded: true })).toBeInTheDocument()
  })
})
