import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import TaskList from './TaskList'

const SAMPLE_TODOS = [
  { content: '收集资料', status: 'completed' as const },
  { content: '分析证据', status: 'in_progress' as const },
  { content: '生成答案', status: 'pending' as const },
]

describe('TaskList', () => {
  it('shows the step pill and reveals the floating checklist on hover', async () => {
    const user = userEvent.setup()
    render(<TaskList todos={SAMPLE_TODOS} />)

    // Pill: Codex "Step N/M" semantics — currently on step 2 of 3.
    // The active step content is only a title hint, not visible text.
    expect(screen.getByText('任务 2/3')).toBeInTheDocument()
    expect(screen.getByTitle('正在执行：分析证据')).toBeInTheDocument()
    expect(screen.queryByText('收集资料')).not.toBeInTheDocument()

    await user.hover(screen.getByText('任务 2/3'))

    // Pill and floating panel render together; the panel overlays without
    // displacing the pill.
    expect(screen.getByText('任务清单')).toBeInTheDocument()
    expect(screen.getByText('收集资料')).toBeInTheDocument()
    expect(screen.getByText('生成答案')).toBeInTheDocument()
    expect(screen.getByText('任务 2/3')).toBeInTheDocument()

    // Leaving closes only after the short grace delay.
    await user.unhover(screen.getByText('任务 2/3'))
    await waitFor(
      () => expect(screen.queryByText('收集资料')).not.toBeInTheDocument(),
      { timeout: 1000 }
    )
    expect(screen.getByText('任务 2/3')).toBeInTheDocument()
  })

  it('keeps the panel open while the pointer travels from pill to panel', async () => {
    const user = userEvent.setup()
    render(<TaskList todos={SAMPLE_TODOS} />)

    await user.hover(screen.getByText('任务 2/3'))
    expect(screen.getByText('收集资料')).toBeInTheDocument()

    // Move from the pill across the gap into the panel header: the delayed
    // close must be cancelled, not fired.
    await user.hover(screen.getByText('任务清单'))
    await new Promise((resolve) => setTimeout(resolve, 300))
    expect(screen.getByText('收集资料')).toBeInTheDocument()

    // Leaving the panel region entirely closes it after the grace delay.
    await user.unhover(screen.getByText('任务清单'))
    await waitFor(
      () => expect(screen.queryByText('收集资料')).not.toBeInTheDocument(),
      { timeout: 1000 }
    )
  })

  it('shows the finished pill state once every step is completed', () => {
    render(
      <TaskList
        todos={[
          { content: '收集资料', status: 'completed' },
          { content: '生成答案', status: 'completed' },
        ]}
      />
    )

    expect(screen.getByText('任务 2/2')).toBeInTheDocument()
    expect(screen.getByTitle('全部任务已完成')).toBeInTheDocument()
  })

  it('survives todos flipping from empty to populated mid-stream', () => {
    // Hooks must run unconditionally: the pill appears only after the first
    // todo_update arrives while the component stays mounted.
    const { rerender } = render(<TaskList todos={[]} />)
    expect(screen.queryByText('任务 2/3')).not.toBeInTheDocument()

    rerender(<TaskList todos={SAMPLE_TODOS} />)

    expect(screen.getByText('任务 2/3')).toBeInTheDocument()
  })
})
