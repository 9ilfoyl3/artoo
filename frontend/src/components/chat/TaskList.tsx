import { useEffect, useRef, useState } from 'react'
import {
  Check,
  Circle,
  LoaderCircle,
} from 'lucide-react'

import { cn } from '@/lib/utils'

export type TaskStatus = 'pending' | 'in_progress' | 'completed'

export interface TaskListItem {
  content: string
  status: TaskStatus
}

export function normalizeTaskList(value: unknown): TaskListItem[] {
  if (!Array.isArray(value)) return []
  const normalized: TaskListItem[] = []
  for (const item of value) {
    if (!item || typeof item !== 'object') continue
    const record = item as Record<string, unknown>
    const content = typeof record.content === 'string' ? record.content.trim() : ''
    const status = record.status
    if (
      !content ||
      (status !== 'pending' && status !== 'in_progress' && status !== 'completed')
    ) {
      continue
    }
    normalized.push({ content, status })
  }
  return normalized
}

function StatusIcon({ status }: { status: TaskStatus }) {
  if (status === 'completed') {
    return (
      <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-emerald-500 text-white shadow-sm">
        <Check className="h-2.5 w-2.5 stroke-[3]" />
      </span>
    )
  }
  if (status === 'in_progress') {
    return (
      <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-primary/35 bg-primary/10 text-primary shadow-sm">
        <LoaderCircle className="h-2.5 w-2.5 animate-spin" />
      </span>
    )
  }
  return (
    <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-border bg-background text-muted-foreground/40">
      <Circle className="h-1.5 w-1.5 fill-current" />
    </span>
  )
}

interface TaskListProps {
  todos: TaskListItem[]
}

/**
 * Codex-style live task pill pinned above the composer.
 *
 * The pill always occupies layout space ("任务 n/m" tracking the current step);
 * hovering the pill itself reveals a floating checklist panel anchored directly
 * above. The panel overlays the transcript without pushing content, and pill
 * and panel render together, matching the Codex reference.
 *
 * Hover handlers live on the pill-width wrapper (not the full-width centering
 * row) so only the pill is a hover target. Closing is delayed: the pointer can
 * cross the gap between pill and panel without dismissing it, and the panel
 * unmounts shortly after the pointer leaves both regions.
 */
export default function TaskList({ todos }: TaskListProps) {
  const [hovered, setHovered] = useState(false)
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(
    () => () => {
      if (closeTimerRef.current) clearTimeout(closeTimerRef.current)
    },
    []
  )

  // Early return must stay below every hook: todos flips from empty to
  // populated mid-stream, and a conditional hook count crashes the tree.
  if (todos.length === 0) return null

  const openPanel = () => {
    if (closeTimerRef.current) {
      clearTimeout(closeTimerRef.current)
      closeTimerRef.current = null
    }
    setHovered(true)
  }
  const scheduleClose = () => {
    if (closeTimerRef.current) clearTimeout(closeTimerRef.current)
    closeTimerRef.current = setTimeout(() => {
      closeTimerRef.current = null
      setHovered(false)
    }, 200)
  }

  const completed = todos.filter((todo) => todo.status === 'completed').length
  const allCompleted = completed === todos.length
  const active =
    todos.find((todo) => todo.status === 'in_progress') ||
    todos.find((todo) => todo.status === 'pending')
  // Codex "Step N/M" semantics: 1-based index of the step being worked on now
  // (falling back to the total once everything is completed).
  const currentStep = active ? todos.indexOf(active) + 1 : todos.length

  return (
    <div className="mb-2 flex justify-center animate-in fade-in-0 duration-200">
      <div
        className="relative"
        onMouseEnter={openPanel}
        onMouseLeave={scheduleClose}
      >
        {hovered && (
          <div className="absolute bottom-full left-1/2 z-30 mb-2.5 w-80 max-w-[calc(100vw-2rem)] -translate-x-1/2 overflow-hidden rounded-xl border border-border/80 bg-card shadow-lg animate-in fade-in-0 slide-in-from-bottom-1 duration-150">
            <div className="flex items-center gap-2 px-3.5 py-2.5">
              <span className="text-xs font-semibold text-foreground">任务清单</span>
              <span className="text-[11px] tabular-nums text-muted-foreground">
                {currentStep}/{todos.length}
              </span>
            </div>

            <div className="max-h-64 overflow-y-auto overscroll-contain px-3.5 pb-1 pt-1.5">
              {todos.map((todo, index) => {
                const isLast = index === todos.length - 1
                return (
                  <div key={`${index}:${todo.content}`} className="relative flex gap-2.5">
                    {!isLast && (
                      <span
                        className={cn(
                          'absolute left-[7px] top-5 h-[calc(100%-0.25rem)] w-px',
                          todo.status === 'completed' ? 'bg-emerald-500/30' : 'bg-border'
                        )}
                      />
                    )}
                    <span className="pt-0.5">
                      <StatusIcon status={todo.status} />
                    </span>
                    <span
                      className={cn(
                        'min-w-0 flex-1 pb-2.5 text-xs leading-5',
                        todo.status === 'completed' && 'text-muted-foreground line-through decoration-muted-foreground/35',
                        todo.status === 'in_progress' && 'font-medium text-foreground',
                        todo.status === 'pending' && 'text-muted-foreground'
                      )}
                    >
                      {todo.content}
                    </span>
                  </div>
                )
              })}
            </div>
          </div>
        )}

        <div
          title={allCompleted ? '全部任务已完成' : `正在执行：${active?.content ?? ''}`}
          className="flex h-8 cursor-default items-center gap-2 rounded-full border border-border/70 bg-card/90 pl-3 pr-3 text-[13px] shadow-sm backdrop-blur transition-colors hover:border-border hover:bg-card"
        >
          {allCompleted ? (
            <Check className="h-3.5 w-3.5 shrink-0 stroke-[3] text-emerald-500" />
          ) : (
            <LoaderCircle className="h-3.5 w-3.5 shrink-0 animate-spin text-primary" />
          )}
          <span className="font-medium tabular-nums text-foreground">
            任务 {currentStep}/{todos.length}
          </span>
        </div>
      </div>
    </div>
  )
}
