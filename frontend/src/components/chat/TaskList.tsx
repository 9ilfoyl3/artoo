import { useState } from 'react'
import {
  Check,
  ChevronDown,
  Circle,
  ListTodo,
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
      <span className="relative z-10 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-emerald-500 text-white shadow-sm">
        <Check className="h-2.5 w-2.5 stroke-[3]" />
      </span>
    )
  }
  if (status === 'in_progress') {
    return (
      <span className="relative z-10 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-primary/35 bg-background text-primary shadow-sm">
        <LoaderCircle className="h-2.5 w-2.5 animate-spin" />
      </span>
    )
  }
  return (
    <span className="relative z-10 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-border bg-background text-muted-foreground/45">
      <Circle className="h-1.5 w-1.5 fill-current" />
    </span>
  )
}

interface TaskListProps {
  todos: TaskListItem[]
}

/**
 * Codex-style compact task rail shown above the composer.
 *
 * The collapsed state keeps the current step visible without adding noise.
 * Expanding reveals the full checklist and its live status transitions.
 */
export default function TaskList({ todos }: TaskListProps) {
  const [expanded, setExpanded] = useState(false)
  if (todos.length === 0) return null

  const completed = todos.filter((todo) => todo.status === 'completed').length
  const active =
    todos.find((todo) => todo.status === 'in_progress') ||
    todos.find((todo) => todo.status === 'pending')
  const allCompleted = completed === todos.length
  const progress = Math.round((completed / todos.length) * 100)

  return (
    <div className="mb-2 overflow-hidden rounded-xl border border-border/80 bg-card/95 shadow-sm backdrop-blur supports-[backdrop-filter]:bg-card/85">
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2.5 px-3 py-2.5 text-left transition-colors hover:bg-muted/30"
      >
        <span
          className={cn(
            'flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border',
            allCompleted
              ? 'border-emerald-500/20 bg-emerald-500/10 text-emerald-600'
              : 'border-primary/20 bg-primary/8 text-primary'
          )}
        >
          {allCompleted ? (
            <Check className="h-3.5 w-3.5 stroke-[3]" />
          ) : (
            <ListTodo className="h-3.5 w-3.5" />
          )}
        </span>

        <span className="min-w-0 flex-1">
          <span className="flex items-baseline gap-2">
            <span className="text-xs font-semibold text-foreground">任务</span>
            <span className="text-[11px] tabular-nums text-muted-foreground">
              {completed}/{todos.length}
            </span>
          </span>
          <span className="mt-0.5 block truncate text-[11px] text-muted-foreground">
            {allCompleted ? '全部完成' : active?.content}
          </span>
        </span>

        <ChevronDown
          className={cn(
            'h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform duration-200',
            expanded && 'rotate-180'
          )}
        />
      </button>

      <div className="h-0.5 bg-muted/50">
        <div
          className={cn(
            'h-full transition-all duration-500 ease-out',
            allCompleted ? 'bg-emerald-500' : 'bg-primary'
          )}
          style={{ width: `${progress}%` }}
        />
      </div>

      {expanded && (
        <div className="border-t border-border/60 px-3 py-2.5">
          <div className="space-y-0">
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
    </div>
  )
}
