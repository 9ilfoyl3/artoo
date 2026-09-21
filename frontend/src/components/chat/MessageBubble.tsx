import { useState, useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { ChevronDown, ChevronUp, Bot, FileText, Loader2, CheckCircle2, XCircle, Lightbulb, Monitor, AlertTriangle, Image as ImageIcon, BookOpen, Copy, Check, ThumbsUp, ThumbsDown, RotateCcw } from 'lucide-react'
import { Streamdown } from 'streamdown'
import { cjk } from '@streamdown/cjk'
import { copyToClipboard } from '@/lib/clipboard'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from '@/components/ui/tooltip'
import { isImageFilename } from '@/components/chat/SessionFileList'
import { StructuredResultCard } from '@/components/chat/StructuredResultCard'
import { summarizeStructuredResult } from '@/components/chat/structuredResultRegistry'
import type { MessageAttachment, SessionFileResponse } from '@/lib/api'
import type { StructuredToolContent } from '@/lib/structuredResult'
import { useArtifactStore, isPreviewable, type ArtifactTarget } from '@/stores/artifactStore'

// 从文件名提取小写扩展名（无点），用于判断是否可预览
function extOf(name: string): string {
  return name.includes('.') ? name.split('.').pop()!.toLowerCase() : ''
}

// 会话文件扩展名：优先 file_type，回退文件名后缀
function sessionFileExt(f: SessionFileResponse): string {
  return (f.file_type || extOf(f.filename)).toLowerCase()
}

// 流式渲染动画配置：模糊渐入、按字符、放慢节奏
const STREAM_ANIMATION = {
  animation: 'blurIn',
  sep: 'word',
  duration: 300,
  stagger: 20,
  easing: 'ease-out',
} as const

// 工具读到的文件（检索/附件类工具带出）：用于在步骤行内联展示可点击预览。
export interface ToolFile {
  id: string
  filename: string
  source: 'document' | 'session-file'
}

// 内容段落类型：思考、回答、工具调用、工具结果按 SSE 顺序交错排列
export interface ContentSegment {
  type: 'reasoning' | 'text' | 'tool_call'
  content: string  // reasoning/text: 文本内容; tool_call: 工具名
  toolCallId?: string
  toolName?: string
  // 工具调用参数（如 read_skill 的 skill_name、检索的 query），用于在步骤行展示更具体的信息
  toolArgs?: Record<string, unknown>
  success?: boolean
  durationMs?: number
  // 本次工具读到的文件（检索类工具解析 doc_id→文件名/来源），用于步骤行内联可点击预览
  files?: ToolFile[]
  // MCP structuredContent 经后端通用校验后的宿主渲染数据；原始工具文本不在此通道。
  structuredContent?: StructuredToolContent
}

// 消息类型
export interface Message {
  role: 'user' | 'assistant'
  content: string
  // 已落库消息的 DB ID（assistant 消息流式结束后由 message_saved 事件回填；历史消息加载时带上）。
  // 用于反馈（点赞/踩）定位；为空表示尚未落库（如流式进行中或保存失败）。
  id?: string
  // 用户对本条 AI 回答的反馈：'like' | 'dislike' | null
  feedback?: 'like' | 'dislike' | null
  // 标记本条 assistant 消息为错误结果（请求异常）：动作栏仅显示重试
  isError?: boolean
  // 标记本条 assistant 消息被用户中途停止：保留已生成内容，气泡尾部展示「已停止」提示
  stopped?: boolean
  references?: Reference[]
  agentSteps?: AgentStep[]
  // 用户消息绑定的会话文件附件（发送时从已上传文件中选取，随消息进入历史）
  attachments?: MessageAttachment[]
  // 新格式：交错流式段落
  segments?: ContentSegment[]
  // Agent 执行整体耗时（毫秒），来自 complete 事件
  totalDurationMs?: number
  // 旧格式兼容
  thoughts?: string[]
  toolCalls?: ToolCallStatus[]
  // 检索降级提示（session-file-upload Req 2.x）：区分会话文件源 / 知识库源失败。
  // 来自 SSE meta 事件的 metadata，渲染"部分来源检索失败、结果可能不完整"的分类提示。
  sessionSourceFailed?: boolean
  kbSourceFailed?: boolean
}

// 旧格式兼容
export interface AgentStep {
  step: string
  detail: string
}

// 新格式：工具调用状态
export interface ToolCallStatus {
  tool_call_id: string
  tool_name: string
  status: 'calling' | 'success' | 'failed'
  duration_ms?: number
}

export interface Reference {
  doc_id: string
  chunk_id: string
  filename: string
  content: string
  child_content: string
  score: number
}

interface MessageBubbleProps {
  message: Message
  index: number
  isStreaming: boolean
  isLast: boolean
  expandedRefs: Set<number>
  onToggleRef: (index: number) => void
  /** 文件名 → 图片预览 URL（本会话内上传的图片，用于附件 chip 缩略图/放大预览） */
  imagePreviewUrls?: Record<string, string>
  /** 当前会话 ID（用于附件在 Artifact 面板按会话拉取原件预览） */
  sessionId?: string | null
  /** 本会话已上传文件列表（用于 read_attachment 步骤按文件名解析原件、点击预览） */
  sessionFiles?: SessionFileResponse[]
  /** 设置/取消反馈（点赞/踩）。仅 assistant 且已落库（有 id）时可用。 */
  onFeedback?: (message: Message, feedback: 'like' | 'dislike' | null) => void
  /** 重试本轮对话。仅最新一条 assistant 消息可用。 */
  onRetry?: () => void
  /** 本条是否为最新一条 assistant 消息（决定是否显示重试按钮）。 */
  isLastAssistant?: boolean
}

function MessageBubble({
  message: msg,
  index: idx,
  isStreaming,
  isLast,
  expandedRefs,
  onToggleRef,
  imagePreviewUrls = {},
  sessionId = null,
  sessionFiles = [],
  onFeedback,
  onRetry,
  isLastAssistant = false,
}: MessageBubbleProps) {
  if (msg.role === 'user') {
    return (
      <div className="flex flex-col items-end gap-1.5 animate-in fade-in-0 slide-in-from-bottom-2 duration-300">
        {msg.attachments && msg.attachments.length > 0 && (
          <MessageAttachments attachments={msg.attachments} imagePreviewUrls={imagePreviewUrls} sessionId={sessionId} />
        )}
        <div className="max-w-[75%]">
          <div className="rounded-2xl rounded-br-md bg-primary text-primary-foreground px-4 py-3 text-sm leading-relaxed shadow-sm">
            <p className="whitespace-pre-wrap">{msg.content}</p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="group animate-in fade-in-0 slide-in-from-bottom-2 duration-300">
      <div className="flex-1 min-w-0 space-y-2">
        {/* 新格式：交错流式段落渲染 */}
        {msg.segments && msg.segments.length > 0 ? (
          <AgentStreamContent
            segments={msg.segments}
            isStreaming={isStreaming}
            isLast={isLast}
            sessionId={sessionId}
            sessionFiles={sessionFiles}
          />
        ) : (
          <>
            {/* 旧格式兼容：思考过程折叠面板 */}
            {msg.thoughts && msg.thoughts.length > 0 && (
              <ThinkingPanel thoughts={msg.thoughts} />
            )}

            {/* 旧格式兼容：工具调用状态 */}
            {msg.toolCalls && msg.toolCalls.length > 0 && (
              <ToolCallsBlock toolCalls={msg.toolCalls} />
            )}

            {/* 旧格式兼容：Agent 思考过程 */}
            {msg.agentSteps && msg.agentSteps.length > 0 && (
              <AgentStepsBlock
                steps={msg.agentSteps}
                hasContent={!!msg.content}
                index={idx}
                expandedRefs={expandedRefs}
                onToggleRef={onToggleRef}
              />
            )}

            {/* 回答内容（非 segments 模式） */}
            {msg.content ? (
              <div className="px-4 py-3 text-sm leading-relaxed">
                <div className="prose prose-sm max-w-none dark:prose-invert [&>p]:mb-2 [&>p:last-child]:mb-0">
                  <Streamdown
                    mode={isStreaming && isLast ? 'streaming' : 'static'}
                    plugins={{ cjk: cjk }}
                    isAnimating={isStreaming && isLast}
                    animated={STREAM_ANIMATION}
                  >
                    {msg.content}
                  </Streamdown>
                </div>
              </div>
            ) : (
              isStreaming && isLast && (
                <div className="px-4 py-3">
                  <div className="flex items-center gap-2 py-1">
                    <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_infinite]" />
                    <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_0.2s_infinite]" />
                    <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_0.4s_infinite]" />
                  </div>
                </div>
              )
            )}
          </>
        )}

        {/* 检索降级提示（session-file-upload Req 2.x）：区分会话文件源 / 知识库源失败。
            会话源与知识库源可能其一失败、其一成功，分类提示让用户知道结果可能不完整及缺失来源。 */}
        {(msg.sessionSourceFailed || msg.kbSourceFailed) && (
          <div className="mx-1 flex items-start gap-2 rounded-lg border border-amber-300/60 bg-amber-50 px-3 py-2 text-xs text-amber-700 dark:border-amber-700/50 dark:bg-amber-950/30 dark:text-amber-400">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
            <span>
              {msg.sessionSourceFailed && msg.kbSourceFailed
                ? '会话文件与知识库检索均部分失败，本次回答可能不完整。'
                : msg.sessionSourceFailed
                  ? '会话文件检索失败，本次回答未纳入本会话上传文件的内容。'
                  : '知识库检索部分失败，本次回答可能遗漏部分知识库内容。'}
            </span>
          </div>
        )}

        {/* 用户中途停止：保留已生成内容，气泡尾部展示「已停止」轻量提示 */}
        {msg.stopped && !(isStreaming && isLast) && (
          <div className="mx-1 flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
            <span className="flex h-3.5 w-3.5 items-center justify-center rounded-full border border-current">
              <span className="h-1.5 w-1.5 rounded-[1px] bg-current" />
            </span>
            <span>已停止生成</span>
          </div>
        )}

        {/* 动作栏：复制 / 赞 / 踩 / 重试。流式进行中不显示；错误消息仅显示重试。 */}
        {!(isStreaming && isLast) && (msg.content || msg.isError) && (
          <MessageActions
            message={msg}
            isLastAssistant={isLastAssistant}
            onFeedback={onFeedback}
            onRetry={onRetry}
          />
        )}
      </div>
    </div>
  )
}

// 消息动作栏：复制 / 点赞 / 踩 / 重试。
// - 复制：始终可用；
// - 点赞/踩：仅 assistant 且已落库（有 id）且非错误消息时可用，互斥可取消；
// - 重试：仅最新一条 assistant 消息显示；错误消息仅显示重试。
function MessageActions({
  message,
  isLastAssistant,
  onFeedback,
  onRetry,
}: {
  message: Message
  isLastAssistant: boolean
  onFeedback?: (message: Message, feedback: 'like' | 'dislike' | null) => void
  onRetry?: () => void
}) {
  const [copied, setCopied] = useState(false)
  // 触发图标弹跳动画的计数键：变化即重播动画（复制成功 / 点赞 / 点踩 各自独立）
  const [popKey, setPopKey] = useState({ copy: 0, like: 0, dislike: 0 })
  const isError = !!message.isError
  const canFeedback = !isError && !!message.id && !!onFeedback
  const showRetry = isLastAssistant && !!onRetry

  async function handleCopy() {
    const ok = await copyToClipboard(message.content)
    if (ok) {
      setCopied(true)
      setPopKey((p) => ({ ...p, copy: p.copy + 1 }))
      setTimeout(() => setCopied(false), 1500)
    }
  }

  function toggleFeedback(next: 'like' | 'dislike') {
    if (!canFeedback) return
    // 仅在「激活」（非取消）时弹跳，取消则不弹
    if (message.feedback !== next) {
      setPopKey((p) => ({ ...p, [next]: p[next] + 1 }))
    }
    onFeedback?.(message, message.feedback === next ? null : next)
  }

  return (
    <div className="flex items-center gap-1 px-1 pt-0.5 opacity-0 transition-opacity duration-200 group-hover:opacity-100 focus-within:opacity-100">
      {/* 错误消息不展示复制/反馈，仅重试 */}
      {!isError && (
        <>
          <ActionButton label={copied ? '已复制' : '复制'} onClick={handleCopy}>
            {copied ? (
              <Check key={`copy-${popKey.copy}`} className="h-3.5 w-3.5 text-green-500 animate-icon-pop" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
          </ActionButton>
          {canFeedback && (
            <>
              <ActionButton
                label="赞"
                active={message.feedback === 'like'}
                onClick={() => toggleFeedback('like')}
              >
                <ThumbsUp
                  key={`like-${popKey.like}`}
                  className={`h-3.5 w-3.5 ${message.feedback === 'like' ? 'text-primary fill-primary/20 animate-icon-pop' : ''}`}
                />
              </ActionButton>
              <ActionButton
                label="踩"
                active={message.feedback === 'dislike'}
                onClick={() => toggleFeedback('dislike')}
              >
                <ThumbsDown
                  key={`dislike-${popKey.dislike}`}
                  className={`h-3.5 w-3.5 ${message.feedback === 'dislike' ? 'text-destructive fill-destructive/20 animate-icon-pop' : ''}`}
                />
              </ActionButton>
            </>
          )}
        </>
      )}
      {showRetry && (
        <ActionButton label="重试" onClick={onRetry}>
          <RotateCcw className="h-3.5 w-3.5 transition-transform duration-300 group-active/btn:-rotate-180" />
        </ActionButton>
      )}
    </div>
  )
}

// 动作栏单个图标按钮（带 tooltip + 选中态高亮 + 按下回弹）
function ActionButton({
  label,
  active,
  onClick,
  children,
}: {
  label: string
  active?: boolean
  onClick?: () => void
  children: ReactNode
}) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            onClick={onClick}
            className={`group/btn p-1.5 rounded-md transition-all duration-150 cursor-pointer text-muted-foreground hover:text-foreground hover:bg-muted/60 active:scale-90 ${active ? 'text-foreground' : ''}`}
          >
            {children}
          </button>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

// Agent 活动流：reasoning / tool_call / text 按真实到达顺序在同一个对话块内渲染。
function AgentStreamContent({
  segments,
  isStreaming,
  isLast,
  sessionId,
  sessionFiles,
}: {
  segments: ContentSegment[]
  isStreaming: boolean
  isLast: boolean
  sessionId?: string | null
  sessionFiles?: SessionFileResponse[]
}) {
  const [expandedRows, setExpandedRows] = useState<Set<number>>(new Set())
  const visibleSegments = segments.filter(
    (seg) => seg.type !== 'reasoning' || seg.content.trim() !== ''
  )
  const activeIndex = isStreaming && isLast && visibleSegments[visibleSegments.length - 1]?.type !== 'text'
    ? visibleSegments.length - 1
    : -1

  function toggleRow(index: number) {
    setExpandedRows((prev) => {
      const next = new Set(prev)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })
  }

  return (
    <div className="px-4 py-3">
      {visibleSegments.length === 0 && isStreaming && isLast ? (
        <div className="flex items-center gap-2 py-1">
          <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_infinite]" />
          <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_0.2s_infinite]" />
          <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_0.4s_infinite]" />
        </div>
      ) : (
        <div className="space-y-2.5">
          {visibleSegments.map((seg, index) => {
            const active = index === activeIndex
            if (seg.type === 'text') {
              const live = isStreaming && isLast && index === visibleSegments.length - 1
              return (
                <div key={`text-${index}`} className="text-sm leading-relaxed">
                  <div className="prose prose-sm max-w-none dark:prose-invert [&>p]:mb-2 [&>p:last-child]:mb-0">
                    <Streamdown
                      mode={live ? 'streaming' : 'static'}
                      plugins={{ cjk }}
                      isAnimating={live}
                      animated={STREAM_ANIMATION}
                    >
                      {seg.content}
                    </Streamdown>
                  </div>
                </div>
              )
            }

            return (
              <StepRow
                key={`${seg.type}-${index}`}
                seg={seg}
                expanded={expandedRows.has(index) || active}
                onToggle={() => toggleRow(index)}
                animating={active && seg.type === 'reasoning'}
                isActive={active}
                sessionId={sessionId}
                sessionFiles={sessionFiles}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}

// 耗时格式化：>=1s 显示秒，否则显示毫秒
function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}m${s}s`
}

// 工具名 → 中文标签映射（步骤行展示用，与 AgentConfig 的 ALL_TOOLS 保持一致）
const TOOL_LABELS: Record<string, string> = {
  knowledge_search: '语义检索',
  grep_chunks: '关键词检索',
  list_knowledge_chunks: '分页浏览',
  web_search: '网页搜索',
  read_attachment: '阅读附件',
  read_skill: '加载技能',
}

// 从工具调用参数中提取一段简短描述，用于在步骤标题后展示「调用了什么」的具体信息
function toolArgSummary(toolName?: string, args?: Record<string, unknown>): string | null {
  if (!args) return null
  if (toolName === 'read_skill') {
    const name = args.skill_name
    return typeof name === 'string' && name ? name : null
  }
  if (toolName === 'read_attachment') {
    const fn = args.filename
    return typeof fn === 'string' && fn ? fn : null
  }
  return null
}

// 检索类工具读到的文件：在步骤标题行内联展示（与阅读附件同形式，免展开即可见）。
// 单行排列，放不下时右侧淡出并显示「共 N 个」总数提示；命中可预览类型的文件名可点击预览。
function InlineToolFiles({
  files,
  fileTarget,
  onOpen,
}: {
  files: ToolFile[]
  fileTarget: (f: ToolFile) => ArtifactTarget | null
  onOpen: (t: ArtifactTarget) => void
}) {
  const clipRef = useRef<HTMLSpanElement>(null)
  const [overflow, setOverflow] = useState(false)

  useEffect(() => {
    const el = clipRef.current
    if (!el) return
    const measure = () => setOverflow(el.scrollWidth - el.clientWidth > 1)
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [files])

  return (
    <span className="flex items-center gap-1.5 min-w-0 flex-1">
      <span
        ref={clipRef}
        className={cn(
          'flex items-center gap-2 min-w-0 overflow-hidden',
          overflow && 'mask-[linear-gradient(to_right,black_88%,transparent)]'
        )}
      >
        {files.map((f, i) => {
          const t = fileTarget(f)
          return t ? (
            <span
              key={`${f.id}-${i}`}
              role="link"
              tabIndex={0}
              onClick={(e) => {
                e.stopPropagation()
                onOpen(t)
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  e.stopPropagation()
                  onOpen(t)
                }
              }}
              className="shrink-0 max-w-[18em] truncate text-primary underline-offset-2 hover:underline cursor-pointer"
              title={`点击预览 ${f.filename}`}
            >
              {f.filename}
            </span>
          ) : (
            <span key={`${f.id}-${i}`} className="shrink-0 max-w-[18em] truncate text-muted-foreground">
              {f.filename}
            </span>
          )
        })}
      </span>
      {overflow && (
        <span className="shrink-0 text-muted-foreground/70">共 {files.length} 个</span>
      )}
    </span>
  )
}

// 单个活动行：reasoning 显示可折叠 Think 内容，tool_call 显示调用与结果状态。
function StepRow({
  seg,
  expanded,
  onToggle,
  animating,
  isActive,
  sessionId,
  sessionFiles,
}: {
  seg: ContentSegment
  expanded: boolean
  onToggle: () => void
  animating: boolean
  isActive: boolean
  sessionId?: string | null
  sessionFiles?: SessionFileResponse[]
}) {
  const openArtifact = useArtifactStore((s) => s.openArtifact)
  const isTool = seg.type === 'tool_call'
  const isSkill = isTool && seg.toolName === 'read_skill'
  const toolLabel = (seg.toolName && TOOL_LABELS[seg.toolName]) || seg.toolName || seg.content || ''
  const argSummary = toolArgSummary(seg.toolName, seg.toolArgs)
  const structuredSummary = seg.structuredContent
    ? summarizeStructuredResult(seg.structuredContent)
    : null
  const headerSummary = structuredSummary || argSummary

  // read_attachment 步骤：尝试按文件名解析本会话已上传文件，命中可预览类型则可点击预览。
  const attachmentTarget: ArtifactTarget | null = (() => {
    if (seg.toolName !== 'read_attachment' || !sessionId || !sessionFiles?.length) return null
    const wanted = typeof seg.toolArgs?.filename === 'string' ? seg.toolArgs.filename.trim().toLowerCase() : ''
    const file = wanted
      ? sessionFiles.find((f) => f.filename.trim().toLowerCase() === wanted)
      : sessionFiles[0]
    if (!file) return null
    const ext = sessionFileExt(file)
    if (!isPreviewable(ext)) return null
    return { id: file.id, filename: file.filename, fileType: ext, source: 'session-file', sessionId }
  })()

  function fileTarget(f: ToolFile): ArtifactTarget | null {
    const ext = extOf(f.filename)
    if (!isPreviewable(ext)) return null
    if (f.source === 'session-file') {
      return { id: f.id, filename: f.filename, fileType: ext, source: 'session-file', sessionId: sessionId ?? undefined }
    }
    return { id: f.id, filename: f.filename, fileType: ext, source: 'document' }
  }
  const readFiles = isTool ? seg.files ?? [] : []
  const summary = seg.type === 'reasoning' ? firstLine(seg.content) : toolLabel

  return (
    <div
      className={cn(
        'rounded-lg border overflow-hidden transition-colors',
        isActive
          ? 'border-primary/40 bg-primary/[0.03]'
          : 'border-border/40 bg-background/40'
      )}
    >
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-2 px-2.5 py-2 text-xs hover:bg-muted/40 transition-colors cursor-pointer text-left"
      >
        {isSkill ? (
          <BookOpen className="h-3.5 w-3.5 shrink-0 text-primary/70" />
        ) : isTool ? (
          <Monitor className="h-3.5 w-3.5 shrink-0 text-primary/70" />
        ) : (
          <Lightbulb
            className={cn(
              'h-3.5 w-3.5 shrink-0 text-primary/70',
              animating && 'animate-twinkle'
            )}
          />
        )}

        {isTool ? (
          <span className="flex min-w-0 flex-1 items-center gap-2">
            <span className="shrink-0 text-foreground/80">{toolLabel}</span>
            {attachmentTarget ? (
              <span
                role="link"
                tabIndex={0}
                onClick={(e) => {
                  e.stopPropagation()
                  openArtifact(attachmentTarget)
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    e.stopPropagation()
                    openArtifact(attachmentTarget)
                  }
                }}
                className="min-w-0 truncate text-primary underline-offset-2 hover:underline cursor-pointer"
                title="点击预览原文"
              >
                {attachmentTarget.filename}
              </span>
            ) : readFiles.length > 0 ? (
              <InlineToolFiles files={readFiles} fileTarget={fileTarget} onOpen={openArtifact} />
            ) : (
              headerSummary && (
                <span
                  className={cn(
                    'min-w-0 truncate',
                    structuredSummary ? 'text-muted-foreground/80' : 'font-mono text-primary/80'
                  )}
                >
                  {headerSummary}
                </span>
              )
            )}
            <ToolResultStatus success={seg.success} durationMs={seg.durationMs} />
          </span>
        ) : (
          <span className="flex min-w-0 flex-1 items-center gap-2">
            <span className="shrink-0 text-muted-foreground">Thinking</span>
            <span className="min-w-0 truncate text-muted-foreground/80">{summary}</span>
          </span>
        )}

        <ChevronDown
          className="h-3.5 w-3.5 ml-auto text-muted-foreground/60 shrink-0 transition-transform duration-200"
          style={{ transform: expanded ? 'rotate(0deg)' : 'rotate(-90deg)' }}
        />
      </button>

      <div
        className="grid transition-all duration-200 ease-in-out"
        style={{
          gridTemplateRows: expanded ? '1fr' : '0fr',
          opacity: expanded ? 1 : 0,
        }}
      >
        <div className="overflow-hidden">
          <div className="px-2.5 pb-2.5 pt-1 border-t border-border/30">
            {isTool ? (
              <div className="space-y-1.5">
                <p className="text-xs text-muted-foreground">
                  {attachmentTarget ? (
                    <>
                      已读取附件原文{' '}
                      <button
                        onClick={() => openArtifact(attachmentTarget)}
                        className="font-medium text-primary underline-offset-2 hover:underline cursor-pointer"
                        title="点击预览原文"
                      >
                        {attachmentTarget.filename}
                      </button>
                    </>
                  ) : isSkill ? (
                    <>
                      已加载技能 <span className="font-mono">{argSummary || toolLabel}</span>
                    </>
                  ) : readFiles.length > 0 ? (
                    <>读到 {readFiles.length} 个文件：</>
                  ) : (
                    <>已调用 <span className="font-mono">{headerSummary || toolLabel}</span></>
                  )}
                </p>
                {seg.structuredContent && (
                  <StructuredResultCard
                    content={seg.structuredContent}
                    toolName={seg.toolName || ''}
                    success={seg.success}
                  />
                )}
                {readFiles.length > 0 && (
                  <div className="flex flex-col gap-1">
                    {readFiles.map((f, fi) => {
                      const target = fileTarget(f)
                      return (
                        <div key={`${f.id}-${fi}`} className="flex items-center gap-1.5 text-xs">
                          <FileText className="h-3 w-3 shrink-0 text-muted-foreground" />
                          {target ? (
                            <button
                              onClick={() => openArtifact(target)}
                              className="truncate text-primary underline-offset-2 hover:underline cursor-pointer text-left"
                              title="点击预览原文"
                            >
                              {f.filename}
                            </button>
                          ) : (
                            <span className="truncate text-muted-foreground">{f.filename}</span>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            ) : (
              <div className="prose prose-sm max-w-none dark:prose-invert text-xs leading-relaxed **:text-xs [&>p]:mb-1 [&>p:last-child]:mb-0 text-muted-foreground **:text-muted-foreground">
                <Streamdown
                  mode={animating ? 'streaming' : 'static'}
                  plugins={{ cjk }}
                  isAnimating={animating}
                  animated={STREAM_ANIMATION}
                >
                  {seg.content}
                </Streamdown>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function firstLine(text: string): string {
  return text.split('\n', 1)[0] || ''
}

function ToolResultStatus({
  success,
  durationMs,
}: {
  success?: boolean
  durationMs?: number
}) {
  return (
    <span className="ml-auto flex shrink-0 items-center gap-1.5 text-muted-foreground">
      {durationMs !== undefined && <span className="text-[10px]">{formatDuration(durationMs)}</span>}
      {success === undefined ? (
        <Loader2 className="h-3 w-3 animate-spin text-primary/70" />
      ) : success ? (
        <CheckCircle2 className="h-3 w-3 text-green-500" />
      ) : (
        <XCircle className="h-3 w-3 text-red-500" />
      )}
    </span>
  )
}
// Legacy renderer retained only for historical replay compatibility.
export function LegacyStepRow({
  seg,
  expanded,
  onToggle,
  animating,
  sessionId,
  sessionFiles,
}: {
  seg: ContentSegment
  expanded: boolean
  onToggle: () => void
  animating: boolean
  sessionId?: string | null
  sessionFiles?: SessionFileResponse[]
}) {
  const openArtifact = useArtifactStore((s) => s.openArtifact)
  const isTool = seg.type === 'tool_call'
  const isSkill = isTool && seg.toolName === 'read_skill'
  const toolLabel = (seg.toolName && TOOL_LABELS[seg.toolName]) || seg.toolName || seg.content
  const argSummary = toolArgSummary(seg.toolName, seg.toolArgs)

  // read_attachment 步骤：尝试按文件名解析本会话已上传文件，命中可预览类型则可点击预览。
  // filename 省略时（默认读第一个附件），回退到列表首个文件，与后端解析逻辑一致。
  const attachmentTarget: ArtifactTarget | null = (() => {
    if (seg.toolName !== 'read_attachment' || !sessionId || !sessionFiles?.length) return null
    const wanted = typeof seg.toolArgs?.filename === 'string' ? seg.toolArgs.filename.trim().toLowerCase() : ''
    const file = wanted
      ? sessionFiles.find((f) => f.filename.trim().toLowerCase() === wanted)
      : sessionFiles[0]
    if (!file) return null
    const ext = sessionFileExt(file)
    if (!isPreviewable(ext)) return null
    return { id: file.id, filename: file.filename, fileType: ext, source: 'session-file', sessionId }
  })()

  // 检索类工具（语义检索/关键词检索）本次读到的文件：后端按 doc_id 解析出文件名/来源。
  // 在步骤行内联展示，命中可预览类型即可点击预览（粒度到文件，不到 chunk）。
  function fileTarget(f: ToolFile): ArtifactTarget | null {
    const ext = extOf(f.filename)
    if (!isPreviewable(ext)) return null
    if (f.source === 'session-file') {
      if (!sessionId) return null
      return { id: f.id, filename: f.filename, fileType: ext, source: 'session-file', sessionId }
    }
    return { id: f.id, filename: f.filename, fileType: ext, source: 'document' }
  }
  const readFiles = isTool ? seg.files ?? [] : []

  return (
    <div className="rounded-lg border border-border/40 bg-background/40 overflow-hidden">
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-2 px-2.5 py-2 text-xs hover:bg-muted/40 transition-colors cursor-pointer text-left"
      >
        {isSkill ? (
          <BookOpen className="h-3.5 w-3.5 shrink-0 text-primary/70" />
        ) : isTool ? (
          <Monitor className="h-3.5 w-3.5 shrink-0 text-primary/70" />
        ) : (
          <Lightbulb className="h-3.5 w-3.5 shrink-0 text-primary/70" />
        )}
        {isTool ? (
          <span className="flex items-center gap-2 min-w-0 flex-1">
            <span className="shrink-0 text-foreground/80">{toolLabel}</span>
            {attachmentTarget ? (
              <span
                role="link"
                tabIndex={0}
                onClick={(e) => {
                  e.stopPropagation()
                  openArtifact(attachmentTarget)
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    e.stopPropagation()
                    openArtifact(attachmentTarget)
                  }
                }}
                className="min-w-0 truncate text-primary underline-offset-2 hover:underline cursor-pointer"
                title="点击预览原文"
              >
                {attachmentTarget.filename}
              </span>
            ) : readFiles.length > 0 ? (
              <InlineToolFiles files={readFiles} fileTarget={fileTarget} onOpen={openArtifact} />
            ) : (
              argSummary && (
                <span className="min-w-0 truncate font-mono text-primary/80">{argSummary}</span>
              )
            )}
          </span>
        ) : (
          <span className="truncate text-muted-foreground">{seg.content}</span>
        )}
        <ChevronDown
          className="h-3.5 w-3.5 ml-auto text-muted-foreground/60 shrink-0 transition-transform duration-200"
          style={{ transform: expanded ? 'rotate(0deg)' : 'rotate(-90deg)' }}
        />
      </button>

      <div
        className="grid transition-all duration-200 ease-in-out"
        style={{
          gridTemplateRows: expanded ? '1fr' : '0fr',
          opacity: expanded ? 1 : 0,
        }}
      >
        <div className="overflow-hidden">
          <div className="px-2.5 pb-2.5 pt-1 border-t border-border/30">
            {isTool ? (
              <div className="space-y-1.5">
                <p className="text-xs text-muted-foreground">
                  {attachmentTarget ? (
                    <>
                      已读取附件原文{' '}
                      <button
                        onClick={() => openArtifact(attachmentTarget)}
                        className="font-medium text-primary underline-offset-2 hover:underline cursor-pointer"
                        title="点击预览原文"
                      >
                        {attachmentTarget.filename}
                      </button>
                    </>
                  ) : isSkill ? (
                    <>
                      已加载技能 <span className="font-mono">{argSummary || toolLabel}</span>
                    </>
                  ) : readFiles.length > 0 ? (
                    <>读到 {readFiles.length} 个文件 ：</>
                  ) : (
                    <>已调用 <span className="font-mono">{argSummary || toolLabel}</span></>
                  )}
                </p>
                {/* 检索类工具读到的文件：内联可点击预览（命中可预览类型才可点） */}
                {readFiles.length > 0 && (
                  <div className="flex flex-col gap-1">
                    {readFiles.map((f, fi) => {
                      const target = fileTarget(f)
                      return (
                        <div key={`${f.id}-${fi}`} className="flex items-center gap-1.5 text-xs">
                          <FileText className="h-3 w-3 shrink-0 text-muted-foreground" />
                          {target ? (
                            <button
                              onClick={() => openArtifact(target)}
                              className="truncate text-primary underline-offset-2 hover:underline cursor-pointer text-left"
                              title="点击预览原文"
                            >
                              {f.filename}
                            </button>
                          ) : (
                            <span className="truncate text-muted-foreground">{f.filename}</span>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            ) : (
              <div className="prose prose-sm max-w-none dark:prose-invert text-xs leading-relaxed **:text-xs [&>p]:mb-1 [&>p:last-child]:mb-0 text-muted-foreground **:text-muted-foreground">
                <Streamdown mode={animating ? 'streaming' : 'static'} plugins={{ cjk: cjk }} isAnimating={animating} animated={STREAM_ANIMATION}>
                  {seg.content}
                </Streamdown>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// 旧格式兼容：思考过程折叠面板（Task 10.2）
function ThinkingPanel({ thoughts }: { thoughts: string[] }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="rounded-xl border border-border/50 bg-muted/30 overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-colors cursor-pointer"
      >
        <Bot className="h-3.5 w-3.5 shrink-0 text-primary/70" />
        <span className="font-medium">思考过程</span>
        <Badge variant="outline" className="text-[10px] px-1.5 py-0 ml-1">
          {thoughts.length}
        </Badge>
        {expanded ? (
          <ChevronUp className="h-3.5 w-3.5 ml-auto" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5 ml-auto" />
        )}
      </button>
      <div
        className="grid transition-all duration-300 ease-in-out"
        style={{
          gridTemplateRows: expanded ? '1fr' : '0fr',
          opacity: expanded ? 1 : 0,
        }}
      >
        <div className="overflow-hidden">
          <div className="px-3 pb-3 pt-1 space-y-2 border-t border-border/40">
            {thoughts.map((thought, i) => (
              <div key={i} className="flex items-start gap-2 text-xs text-muted-foreground">
                <span className="w-1.5 h-1.5 rounded-full bg-primary/40 shrink-0 mt-1.5" />
                <span className="leading-relaxed whitespace-pre-wrap">{thought}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

// 新格式：工具调用状态展示（Task 10.3）
function ToolCallsBlock({ toolCalls }: { toolCalls: ToolCallStatus[] }) {
  return (
    <div className="space-y-1.5 px-1">
      {toolCalls.map((tc) => (
        <div
          key={tc.tool_call_id}
          className="flex items-center gap-2 text-xs text-muted-foreground py-1 px-2 rounded-lg bg-muted/20"
        >
          {tc.status === 'calling' && (
            <Loader2 className="h-3 w-3 animate-spin text-primary/70 shrink-0" />
          )}
          {tc.status === 'success' && (
            <CheckCircle2 className="h-3 w-3 text-green-500 shrink-0" />
          )}
          {tc.status === 'failed' && (
            <XCircle className="h-3 w-3 text-red-500 shrink-0" />
          )}
          <span className="font-mono text-[11px]">{tc.tool_name}</span>
          {tc.duration_ms !== undefined && (
            <span className="text-[10px] text-muted-foreground/70 ml-auto">
              {tc.duration_ms}ms
            </span>
          )}
        </div>
      ))}
    </div>
  )
}

// 旧格式兼容：Agent 思考步骤子组件
function AgentStepsBlock({
  steps,
  hasContent,
  index,
  expandedRefs,
  onToggleRef,
}: {
  steps: AgentStep[]
  hasContent: boolean
  index: number
  expandedRefs: Set<number>
  onToggleRef: (index: number) => void
}) {
  const refKey = -index - 100

  return (
    <div className="rounded-2xl rounded-bl-md bg-muted/40 border border-border/50 overflow-hidden">
      {!hasContent && (
        <div className="px-4 py-3 space-y-2">
          {steps.map((step, stepIdx) => {
            const isLatest = stepIdx === steps.length - 1
            return (
              <div key={stepIdx} className={`flex items-start gap-2 text-sm ${isLatest ? 'text-primary font-medium' : 'text-muted-foreground'}`}>
                {isLatest ? (
                  <span className="w-2 h-2 rounded-full bg-primary animate-pulse shrink-0 mt-1.5" />
                ) : (
                  <span className="w-2 h-2 rounded-full bg-primary/30 shrink-0 mt-1.5" />
                )}
                <span className="leading-relaxed">{step.detail}</span>
              </div>
            )
          })}
        </div>
      )}

      {hasContent && (
        <>
          <button
            onClick={() => onToggleRef(refKey)}
            className="w-full flex items-center gap-2 px-4 py-2.5 text-xs text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors cursor-pointer"
          >
            <Bot className="h-3.5 w-3.5 shrink-0" />
            <span>深度检索 · {steps.length} 步完成</span>
            <ChevronDown
              className="h-3.5 w-3.5 ml-auto transition-transform duration-200"
              style={{ transform: expandedRefs.has(refKey) ? 'rotate(0deg)' : 'rotate(-90deg)' }}
            />
          </button>
          <div
            className="grid transition-all duration-300 ease-in-out"
            style={{
              gridTemplateRows: expandedRefs.has(refKey) ? '1fr' : '0fr',
              opacity: expandedRefs.has(refKey) ? 1 : 0,
            }}
          >
            <div className="overflow-hidden">
              <div className="px-4 pb-3 pt-1 space-y-1.5 border-t border-border/40">
                {steps.map((step, stepIdx) => (
                  <div key={stepIdx} className="flex items-start gap-2 text-xs text-muted-foreground">
                    <span className="w-1.5 h-1.5 rounded-full bg-primary/30 shrink-0 mt-1.5" />
                    <span className="leading-relaxed">{step.detail}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

// 用户消息附件 chip 行（发送时绑定的会话文件）。展示在用户气泡上方、右对齐。
// 可预览类型（pdf/图片/txt/md/csv）点击 chip 在右侧 Artifact 面板预览（对历史会话同样有效，
// 原件从 MinIO 按会话+文件 ID 拉取）。图片附件若本会话内有客户端 blob 则内联缩略图。
function MessageAttachments({
  attachments,
  imagePreviewUrls,
  sessionId,
}: {
  attachments: MessageAttachment[]
  imagePreviewUrls: Record<string, string>
  sessionId?: string | null
}) {
  const openArtifact = useArtifactStore((s) => s.openArtifact)

  function formatSize(bytes?: number | null): string {
    if (!bytes || bytes <= 0) return ''
    if (bytes < 1024) return `${bytes}B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
    return `${(bytes / 1024 / 1024).toFixed(1)}MB`
  }

  function attachmentExt(a: MessageAttachment): string {
    if (a.file_type) return a.file_type.toLowerCase()
    return a.filename.includes('.') ? a.filename.split('.').pop()!.toLowerCase() : ''
  }

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex flex-wrap justify-end gap-1.5 max-w-[75%]">
        {attachments.map((a) => {
          const isImg = isImageFilename(a.filename)
          const previewUrl = isImg ? imagePreviewUrls[a.filename] : undefined
          const sz = formatSize(a.file_size)
          const ext = attachmentExt(a)
          // 有会话上下文 + 支持的类型 → 点击 chip 在 Artifact 面板预览（历史会话亦可）
          const canArtifact = !!sessionId && isPreviewable(ext)
          const onPreview = canArtifact
            ? () =>
                openArtifact({
                  id: a.file_id,
                  filename: a.filename,
                  fileType: ext,
                  source: 'session-file',
                  sessionId: sessionId!,
                })
            : undefined
          return (
            <Tooltip key={a.file_id}>
              <TooltipTrigger asChild>
                <div
                  className={cn(
                    'inline-flex items-center gap-1.5 h-8 pl-1.5 pr-2 rounded-xl border border-border bg-card text-xs text-foreground max-w-[15em] transition-colors hover:border-primary/40',
                    onPreview && 'cursor-pointer'
                  )}
                  onClick={() => onPreview?.()}
                >
                  {previewUrl ? (
                    <span className="h-6 w-6 shrink-0 rounded-md overflow-hidden ring-1 ring-border">
                      <img src={previewUrl} alt="" className="h-full w-full object-cover" />
                    </span>
                  ) : (
                    <span className="h-6 w-6 shrink-0 flex items-center justify-center">
                      {isImg ? (
                        <ImageIcon className="h-3.5 w-3.5 text-muted-foreground" />
                      ) : (
                        <FileText className="h-3.5 w-3.5 text-muted-foreground" />
                      )}
                    </span>
                  )}
                  <span className="truncate font-medium">{a.filename}</span>
                </div>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-xs p-2">
                {previewUrl && (
                  <img src={previewUrl} alt="" className="mb-1.5 max-h-40 w-auto rounded-md object-contain" />
                )}
                <div className="font-medium break-all leading-snug">{a.filename}</div>
                {sz && <div className="mt-0.5 text-xs text-muted-foreground">{sz}</div>}
                {onPreview && <div className="mt-0.5 text-xs text-muted-foreground">点击预览</div>}
              </TooltipContent>
            </Tooltip>
          )
        })}
      </div>
    </TooltipProvider>
  )
}

export default MessageBubble
