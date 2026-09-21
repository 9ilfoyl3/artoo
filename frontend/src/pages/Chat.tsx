import { useState, useRef, useEffect, useMemo } from 'react'
import { ChevronDown } from 'lucide-react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { knowledgeBaseApi, llmConfigApi, sessionApi, agentPresetApi, sessionFileApi, truncateSessionTitle } from '@/lib/api'
import type { SessionFileResponse, MessageAttachment } from '@/lib/api'
import MessageBubble from '@/components/chat/MessageBubble'
import type { Message, Reference, ContentSegment } from '@/components/chat/MessageBubble'
import ChatInput from '@/components/chat/ChatInput'
import ChatAnchorRail, { type QueryAnchor } from '@/components/chat/ChatAnchorRail'
import type { PendingSessionFile } from '@/components/chat/SessionFileList'
import { isImageFilename } from '@/components/chat/SessionFileList'
import SuggestedQuestions from '@/components/chat/SuggestedQuestions'
import ChatMessagesSkeleton from '@/components/skeletons/ChatMessagesSkeleton'
import SideRays from '@/components/SideRays'
import { useSessionUploadEvents } from '@/hooks/useSessionUploadEvents'
import { useSession } from '@/lib/session-context'
import { useConfirm } from '@/lib/confirm-context'
import { authHeaders, handleUnauthorized } from '@/lib/auth'
import { useArtifactStore } from '@/stores/artifactStore'
import { cn } from '@/lib/utils'
import { isStructuredToolContent } from '@/lib/structuredResult'

function browserTimezone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined
  } catch {
    return undefined
  }
}

interface KnowledgeBaseItem {
  id: string
  name: string
  // 归属/可见性（用于聊天选择器按「个人 / 共享」分组；后端列表已透出）
  owner_user_id?: string | null
  visibility?: string | null
}

interface LLMConfigItem {
  id: string
  name: string
  provider: string
  base_url: string
  model: string
  is_default: boolean
}

function Chat() {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [isLoadingMessages, setIsLoadingMessages] = useState(false)
  const [selectedModel, setSelectedModel] = useState('')
  const [selectedPreset, setSelectedPreset] = useState('')
  // 已选知识库（按选中顺序，首个即后端主库 kb_ids[0]，检索权重更高）。
  const [selectedKbIds, setSelectedKbIds] = useState<string[]>([])
  const [expandedRefs, setExpandedRefs] = useState<Set<number>>(new Set())
  const [contextUsage, setContextUsage] = useState<{ current: number; max: number }>({ current: 0, max: 0 })
  // 会话文件本地占位（POST 在飞 / 失败提示），与服务端列表一起展示。
  // 同步上传完成后由 react-query 刷新列表 + 清掉对应占位。
  const [pendingFiles, setPendingFiles] = useState<PendingSessionFile[]>([])
  // 上传中占位的 AbortController：localId → controller，用于中途取消在飞的 POST。
  const uploadControllersRef = useRef<Record<string, AbortController>>({})
  // 本会话内上传的图片：文件名 → object URL（用于 chip 缩略图与放大预览）。
  // 服务端临时文件处理后即删，无法回源取图，故在上传时就地从客户端 blob 生成。
  const [imagePreviewUrls, setImagePreviewUrls] = useState<Record<string, string>>({})
  const imagePreviewUrlsRef = useRef<Record<string, string>>({})
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  // 消息内容容器：用 ResizeObserver 监听其高度变化。Streamdown 的代码高亮 / Mermaid 是
  // 异步动态 import 的——首屏代码块先以「未高亮的高大纯文本」渲染（scrollHeight 偏大），
  // 高亮 resolve 后重排为紧凑的横向滚动块、高度骤降。任何「一次性 smooth 滚到底」都会
  // 被这次高度坍缩打断、停在半路。改为监听内容尺寸、每次变化即「瞬时」贴底，才能稳定到底。
  const contentRef = useRef<HTMLDivElement>(null)
  // 标记上一次滚动是否由程序触发（贴底）。程序化贴底也会派发 scroll 事件，
  // 若不区分，会和「用户上滑打破粘附」的判定混淆（尤其内容高度坍缩时）。
  const programmaticScrollRef = useRef(false)
  // 入场平滑滚动「正在进行」标志：smooth 动画期间会连续派发大量 scroll 事件，且中途 scrollTop
  // 尚未到底，若据此重算粘附会被误判为用户上滑而打破粘附，随后高度坍缩就停在半路。
  // 动画进行期间用此标志全程抑制粘附重算，直到真正到底或超时。
  const autoScrollingRef = useRef(false)
  // 进入/切换会话的「入场动画」标志：为 true 时首次贴底用平滑滚动（从顶部滑到底部的过渡），
  // 待内容（含异步代码高亮）高度稳定后播放一次，避免被高度坍缩打断。之后转为瞬时贴底。
  const entryAnimateRef = useRef(false)
  // 入场平滑滚动的防抖定时器：每次内容高度变化都重置，停止变化一段时间后才真正平滑滚到底。
  const entryScrollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // 入场 rAF 平滑滚动的句柄：每帧重读实时 scrollHeight 缓动逼近底部，跟随懒加载增长。
  const entryRafRef = useRef<number | null>(null)
  // 入场平滑滚动的兜底超时：动画异常未到底时强制结束抑制。
  const autoScrollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // 是否「粘附底部」：true 时新内容自动滚到底；用户上滑后置 false，回到底部后恢复 true。
  // 用 ref 持有权威值（供流式异步/滚动回调即时读取），state 仅驱动按钮等 UI 渲染。
  const stickToBottomRef = useRef(true)
  const [showScrollToBottom, setShowScrollToBottom] = useState(false)
  // 当前视口内激活的提问轮下标（驱动侧边锚点高亮）
  const [activeAnchorIndex, setActiveAnchorIndex] = useState(-1)
  // 每条 user 消息根节点 ref（按 message 下标存）：供锚点跳转定位 + 滚动高亮计算
  const messageRefs = useRef<Record<number, HTMLDivElement | null>>({})
  // 标记：刚在该会话发起发送（消息已在本地），跳过 loadMessages 避免覆盖
  const pendingSendSessionRef = useRef<string | null>(null)
  // 当前展示的会话 id 镜像：供流式异步回调判断「输出归属的会话是否仍在前台」，
  // 闭包里的 currentSessionId 会过期，必须用 ref 读最新值。
  const currentSessionIdRef = useRef<string | null>(null)
  // messages 状态镜像：发起新一轮流式时以它为基线播种流式缓冲（state 更新是异步的，
  // 不能直接读 messages）。
  const messagesRef = useRef<Message[]>(messages)
  // 正在流式输出的会话缓冲（权威副本）：{ sessionId, messages }。
  // 无论用户当前看的是哪个会话，流式回调都更新这里；仅当该会话在前台时才同步到 messages 状态。
  // 切回该会话时用它恢复，实现会话级输出隔离。
  const streamRef = useRef<{ sessionId: string; messages: Message[] } | null>(null)
  // 正在流式输出的会话 id（用于判断「当前查看的会话是否正在流式」，控制光标/重试按钮）。
  const streamingSessionRef = useRef<string | null>(null)
  // 当前在飞的问答流 AbortController：点「停止」时 abort，触发后端 SSE 取消 + 落库部分答案。
  const chatAbortRef = useRef<AbortController | null>(null)

  const { currentSessionId, setCurrentSessionId, newSessionNonce, refreshSessions, addOptimisticSession, replaceOptimisticSession, removeOptimisticSession } = useSession()
  const confirm = useConfirm()
  const queryClient = useQueryClient()
  const closeArtifact = useArtifactStore((s) => s.closeArtifact)

  const { data: knowledgeBases = [] } = useQuery({
    queryKey: ['knowledge-bases'],
    queryFn: () =>
      knowledgeBaseApi.list({ page_size: 100 }).then((res) => res.items as KnowledgeBaseItem[]),
  })

  const { data: llmConfigs = [] } = useQuery({
    queryKey: ['llm-configs', 'chat-visible'],
    queryFn: () => llmConfigApi.list(true) as Promise<LLMConfigItem[]>,
  })

  const { data: agentPresets = [] } = useQuery({
    queryKey: ['agent-presets'],
    queryFn: () => agentPresetApi.list(),
  })

  // 会话上传文件列表（仅在会话存在时拉取；切会话时随 currentSessionId 重新拉）
  // session-file-upload Task 16 / Req 1.8
  const { data: sessionFiles = [] } = useQuery<SessionFileResponse[]>({
    queryKey: ['session-files', currentSessionId],
    queryFn: () => sessionFileApi.list(currentSessionId!),
    enabled: !!currentSessionId,
  })

  // 会话文件建索引实时状态（WS 推送 queued→processing→progress→completed/failed）。
  // 见 useSessionUploadEvents / Design C10：把 live 状态 merge 进服务端列表，
  // 让 chip 无需等 query refetch 即可实时前进。completed/removed 时该 hook 会自动
  // invalidate ['session-files', sid]，服务端行随后携带真实 chunk_count 等落地。
  const { fileStates } = useSessionUploadEvents(currentSessionId)

  // 服务端列表叠加 live 状态：单一 merge 点，向下游（输入区/气泡）统一透出实时进度。
  // 对每个服务端文件，若存在对应 live 状态则以其覆盖 status/progress/message/error
  //（及 chunk_count，若 live 提供）；无 live 状态时保持服务端值不变。
  const mergedSessionFiles = useMemo<SessionFileResponse[]>(
    () =>
      sessionFiles.map((f) => {
        const live = fileStates[f.id]
        if (!live) return f
        return {
          ...f,
          status: live.status || f.status,
          progress: typeof live.progress === 'number' ? live.progress : f.progress,
          progress_message: live.progress_message ?? f.progress_message,
          error_message: live.error_message ?? f.error_message,
          chunk_count: typeof live.chunk_count === 'number' ? live.chunk_count : f.chunk_count,
        }
      }),
    [sessionFiles, fileStates]
  )

  // 切换会话时清掉本地占位（避免 A 会话上传中切到 B 仍显示）。
  // 用 ref 跟踪上一次会话：跳过「null -> 新建会话」首建场景——该场景是新对话首次
  // 上传时由 ensureSessionId() 创建会话触发的，此刻 handleUploadSessionFiles 正要
  // 加上传占位，若在此清空会把刚加的「处理中」占位冲掉，导致看不到上传进度。
  const prevSessionForPendingRef = useRef<string | null>(currentSessionId)
  useEffect(() => {
    const prev = prevSessionForPendingRef.current
    prevSessionForPendingRef.current = currentSessionId
    // 仅在「从一个已存在会话切到另一个会话」或「切回无会话」时清空；
    // 「null -> 新建会话」不清（保留上传发起时刚加的占位）。
    if (prev !== null) {
      setPendingFiles([])
      // 释放上一会话的图片预览 object URL，避免内存泄漏
      Object.values(imagePreviewUrlsRef.current).forEach((url) => URL.revokeObjectURL(url))
      imagePreviewUrlsRef.current = {}
      setImagePreviewUrls({})
    }
  }, [currentSessionId])

  // 组件卸载时统一释放所有图片预览 object URL
  useEffect(() => {
    return () => {
      Object.values(imagePreviewUrlsRef.current).forEach((url) => URL.revokeObjectURL(url))
    }
  }, [])

  // 同步「当前展示会话」与「消息列表」镜像，供流式异步回调读取最新值（闭包会过期）。
  useEffect(() => { currentSessionIdRef.current = currentSessionId }, [currentSessionId])
  useEffect(() => { messagesRef.current = messages }, [messages])

  // 默认选中 is_default 的 Agent 预设
  useEffect(() => {
    if (agentPresets.length > 0 && !selectedPreset) {
      const def = agentPresets.find((p) => p.is_default) ?? agentPresets[0]
      if (def) setSelectedPreset(def.id)
    }
  }, [agentPresets, selectedPreset])

  // 默认选中 is_default 的模型
  useEffect(() => {
    if (llmConfigs.length > 0 && !selectedModel) {
      const defaultConfig = llmConfigs.find((c) => c.is_default)
      if (defaultConfig) setSelectedModel(defaultConfig.id)
    }
  }, [llmConfigs, selectedModel])

  // 贴底（智能滚动）：用 ResizeObserver 监听内容容器高度变化，只要处于「粘附底部」状态，
  // 每次高度变化（流式增量 / markdown / 代码高亮异步定型 / 高度坍缩）都「瞬时」贴到最新底部。
  // 瞬时 scrollTop = scrollHeight 永远会被正确钳制到当前最大值，不像 smooth 动画会被中途的
  // 高度坍缩打断而停在半路。这是「滚不到底」的根本解法。
  //
  // 入场动画（entryAnimateRef）：进入/切换会话时希望保留「从顶部平滑滑到底部」的过渡。
  // 但平滑动画会被异步代码高亮的高度坍缩打断，故用防抖——内容高度每变一次就重置定时器，
  // 待高度稳定（>120ms 无变化）后播放一次平滑滚动；其间不瞬时贴底，让用户看到从顶部下滑。
  useEffect(() => {
    const content = contentRef.current
    const container = scrollContainerRef.current
    if (!content || !container) return
    const pin = () => {
      if (!stickToBottomRef.current) return
      if (entryAnimateRef.current) {
        // 入场动画阶段：防抖，等高度初步稳定后启动一次「自定义 rAF 平滑滚动」。
        // 不用原生 scrollTo({behavior:'smooth'})——它的目标在发起时就固定了，而懒加载会让
        // 下方内容边滚边挂载、高度持续增长，原生动画滚到「旧底部」就停，随后兜底瞬时贴底，
        // 表现为「平滑一段后突然触底」。改为每帧重新读取实时 scrollHeight 并缓动逼近，
        // 内容增长时持续平滑跟随，最终精确落到真实底部。
        if (entryScrollTimerRef.current) clearTimeout(entryScrollTimerRef.current)
        entryScrollTimerRef.current = setTimeout(() => {
          entryAnimateRef.current = false
          if (!stickToBottomRef.current) return
          autoScrollingRef.current = true
          let stableFrames = 0
          let lastTarget = -1
          const step = () => {
            if (!stickToBottomRef.current) { autoScrollingRef.current = false; return }
            const c = scrollContainerRef.current
            if (!c) { autoScrollingRef.current = false; return }
            const target = c.scrollHeight - c.clientHeight
            const cur = c.scrollTop
            const diff = target - cur
            // 缓动：每帧前进剩余距离的一部分，但夹在 [最小步长, 最大步长] 之间。
            // 上限防止距离大时初段冲太快，整体更慢更匀。
            const stepPx = Math.min(Math.max(diff * 0.06, 8), 28)
            programmaticScrollRef.current = true
            if (diff <= 1) {
              c.scrollTop = target
            } else {
              c.scrollTop = cur + Math.min(stepPx, diff)
            }
            // 目标连续稳定（懒加载不再增高）且已贴底 → 结束。
            if (target === lastTarget && diff <= 1) {
              stableFrames += 1
            } else {
              stableFrames = 0
              lastTarget = target
            }
            if (stableFrames >= 2) {
              c.scrollTop = c.scrollHeight
              autoScrollingRef.current = false
              return
            }
            entryRafRef.current = requestAnimationFrame(step)
          }
          entryRafRef.current = requestAnimationFrame(step)
        }, 200)
        return
      }
      // 入场 rAF 动画进行中：高度变化由 step 每帧自行读取跟随，这里不额外贴底（避免打断缓动）。
      if (autoScrollingRef.current) return
      // 常规阶段（流式增量等）：瞬时贴底，绝不被打断。
      programmaticScrollRef.current = true
      container.scrollTop = container.scrollHeight
    }
    const ro = new ResizeObserver(pin)
    ro.observe(content)
    return () => {
      ro.disconnect()
      if (entryScrollTimerRef.current) clearTimeout(entryScrollTimerRef.current)
      if (autoScrollTimerRef.current) clearTimeout(autoScrollTimerRef.current)
      if (entryRafRef.current) cancelAnimationFrame(entryRafRef.current)
    }
  }, [isLoadingMessages, currentSessionId])

  // 滚动监听：维护「粘附底部」状态、回到底部按钮可见性、当前激活提问锚点。
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    // 距底 <= 阈值视为「在底部」（流式高度抖动留出余量）
    const NEAR_BOTTOM = 80
    const onScroll = (initial = false) => {
      // 程序化贴底触发的 scroll：不重算粘附状态（避免高度坍缩时被误判为离底打破粘附），
      // 仅消费一次标记。锚点高亮等仍按当前位置更新。
      const wasProgrammatic = programmaticScrollRef.current
      programmaticScrollRef.current = false
      const distanceToBottom = el.scrollHeight - el.scrollTop - el.clientHeight
      const atBottom = distanceToBottom <= NEAR_BOTTOM
      // 入场平滑滚动进行中：全程不重算粘附（中途 scrollTop 未到底，重算会误判为离底）。
      // 一旦到底即认为动画完成，解除抑制。
      if (autoScrollingRef.current) {
        if (atBottom) {
          autoScrollingRef.current = false
          if (autoScrollTimerRef.current) clearTimeout(autoScrollTimerRef.current)
        }
      } else if (!wasProgrammatic && !initial) {
        // initial：effect 挂载/消息变更时的同步首调。此刻内容常在顶部（scrollTop=0）且尚未
        // 贴底，绝不能据此把 stick 置 false——那会抢在 ResizeObserver 首次 pin 之前打破粘附，
        // 导致「进入会话停在顶部不贴底」。stick 只由真实滚动事件（用户上滑/到底）更新。
        stickToBottomRef.current = atBottom
        setShowScrollToBottom(!atBottom)
      }

      // 激活提问锚点：以视口上部「阅读基线」（容器高度 ~30%）为判定线，
      // 取该线以上、最靠近基线的那条 user 消息（即当前正在阅读的那一轮）。
      // 按下标升序遍历保证「最靠下」语义正确；若无任何提问越过基线（处于首轮顶部），
      // 回退到第一条提问。
      const containerTop = el.getBoundingClientRect().top
      const readingLine = el.clientHeight * 0.3
      const entries = Object.entries(messageRefs.current)
        .filter(([, node]) => node)
        .map(([idxStr, node]) => ({ idx: Number(idxStr), top: node!.getBoundingClientRect().top - containerTop }))
        .sort((a, b) => a.idx - b.idx)
      if (entries.length === 0) {
        setActiveAnchorIndex(-1)
        return
      }
      let active = entries[0].idx
      for (const e of entries) {
        if (e.top <= readingLine) active = e.idx
        else break
      }
      // 已滚到底部：强制激活最后一轮提问（保证新问题/末轮高亮）
      if (atBottom) active = entries[entries.length - 1].idx
      setActiveAnchorIndex(active)
    }
    const scrollHandler = () => onScroll(false)
    el.addEventListener('scroll', scrollHandler, { passive: true })
    onScroll(true)
    return () => el.removeEventListener('scroll', scrollHandler)
  }, [messages])

  // 强制滚到底部并恢复粘附（回到底部按钮 / 发送新消息时调用）
  function scrollToBottom() {
    stickToBottomRef.current = true
    setShowScrollToBottom(false)
    scrollContainerRef.current?.scrollTo({
      top: scrollContainerRef.current.scrollHeight,
      behavior: 'smooth',
    })
  }

  // 跳转到指定提问轮（侧边锚点点击）
  function scrollToMessage(index: number) {
    const node = messageRefs.current[index]
    const container = scrollContainerRef.current
    if (!node || !container) return
    // 上滑定位即视为离开底部，避免随后流式把视口又拉回底部
    stickToBottomRef.current = false
    const containerTop = container.getBoundingClientRect().top
    const nodeTop = node.getBoundingClientRect().top
    container.scrollTo({
      top: container.scrollTop + (nodeTop - containerTop) - 16,
      behavior: 'smooth',
    })
  }

  // 会话切换时加载消息
  useEffect(() => {
    // 切换/进入会话时关闭可能残留的 Artifact 预览（上一会话打开的附件预览不应带到新会话）
    closeArtifact()
    // 进入/切换会话默认粘附底部：上一会话上滑残留的 stick=false 不带过来，
    // 配合 ResizeObserver 在内容（含异步代码高亮）定型后定位到最新消息。
    stickToBottomRef.current = true
    // 标记本次为入场：内容定型后播放一次「从顶部平滑滑到底部」的过渡（防抖等高度稳定）。
    entryAnimateRef.current = true
    if (currentSessionId === null) {
      setMessages([])
      setInput('')
      setPendingFiles([])
      setExpandedRefs(new Set())
      setContextUsage({ current: 0, max: 0 })
      return
    }
    // 若该会话是刚在本地发起发送的（消息已在本地、可能正在流式），跳过加载避免覆盖
    if (pendingSendSessionRef.current === currentSessionId) {
      return
    }
    // 若切回的会话正在后台流式输出：从流式缓冲（权威副本）恢复，跳过服务端加载，
    // 否则会用「尚未落库的半成品」之前的旧历史覆盖正在进行的输出。
    if (
      streamRef.current &&
      streamRef.current.sessionId === currentSessionId &&
      streamingSessionRef.current === currentSessionId
    ) {
      setMessages(streamRef.current.messages)
      setIsStreaming(true)
      setIsLoadingMessages(false)
      return
    }
    // 切到的不是正在流式的会话：确保流式态关闭（流仍在后台跑，但前台不显示光标/禁用）。
    if (streamingSessionRef.current !== currentSessionId) {
      setIsStreaming(false)
    }
    // 加载会话消息
    async function loadMessages() {
      setIsLoadingMessages(true)
      setExpandedRefs(new Set())
      setContextUsage({ current: 0, max: 0 })
      try {
        const msgs = await sessionApi.getMessages(currentSessionId!)
        setMessages(msgs.map((m) => {
          const base: Message = {
            role: m.role as 'user' | 'assistant',
            content: m.content,
            id: m.id,
            feedback: m.feedback ?? null,
            references: (m.references as Reference[]) || undefined,
            attachments: m.attachments || undefined,
          }
          // 解析 agent_steps：区分新格式（有 type 字段）和旧格式（有 step/detail 字段）
          if (m.agent_steps && Array.isArray(m.agent_steps)) {
            const hasNewFormat = m.agent_steps.some((s: Record<string, unknown>) => s.type)
            if (hasNewFormat) {
              // 新格式：构建 segments 数组（按存储顺序还原交错段落）
              const segments: ContentSegment[] = []
              for (const step of m.agent_steps) {
                if ((step.type === 'reasoning_delta' || step.type === 'thought') && step.content) {
                  // 合并连续的 reasoning 段落
                  const lastSeg = segments[segments.length - 1]
                  if (lastSeg && lastSeg.type === 'reasoning') {
                    lastSeg.content += String(step.content)
                  } else {
                    segments.push({ type: 'reasoning', content: String(step.content) })
                  }
                } else if (step.type === 'tool_call') {
                  segments.push({
                    type: 'tool_call',
                    content: String(step.tool_name || ''),
                    toolCallId: String(step.tool_call_id || ''),
                    toolName: String(step.tool_name || ''),
                    toolArgs: (step.arguments as Record<string, unknown>) || undefined,
                    success: undefined,
                  })
                } else if (step.type === 'tool_result') {
                  // 更新匹配的 tool_call 段落
                  const existing = segments.find(
                    (seg) => seg.type === 'tool_call' && seg.toolCallId === step.tool_call_id
                  )
                  if (existing) {
                    existing.success = step.success as boolean
                    existing.durationMs = step.duration_ms as number | undefined
                    existing.files = ((step as Record<string, unknown>).files as ContentSegment['files']) || undefined
                    const structuredContent = (step as Record<string, unknown>).structured_content
                    existing.structuredContent = isStructuredToolContent(structuredContent)
                      ? structuredContent
                      : undefined
                  }
                } else if (step.type === 'text_delta' || step.type === 'final_answer') {
                  if (step.content) {
                    // 合并连续的 answer 段落
                    const lastSeg = segments[segments.length - 1]
                    if (lastSeg && lastSeg.type === 'text') {
                      lastSeg.content += String(step.content)
                    } else {
                      segments.push({ type: 'text', content: String(step.content) })
                    }
                  } else if ((step as Record<string, unknown>).done) {
                    // 旧版 final_answer done 帧仅用于历史兼容。
                    const lastSeg = segments[segments.length - 1]
                    if (lastSeg && lastSeg.type === 'reasoning') {
                      lastSeg.type = 'text'
                    }
                  }
                } else if (step.type === 'complete' && typeof (step as Record<string, unknown>).total_duration_ms === 'number') {
                  // 恢复整体耗时
                  base.totalDurationMs = (step as Record<string, unknown>).total_duration_ms as number
                }
              }
              // 如果没有从 steps 中还原出 answer 段落，但有 content，补一个
              if (base.content && !segments.some((s) => s.type === 'text')) {
                segments.push({ type: 'text', content: base.content })
              }
              if (segments.length > 0) base.segments = segments
            } else {
              // 旧格式：直接作为 agentSteps
              base.agentSteps = m.agent_steps
            }
          }
          return base
        }))

        // 从历史消息中恢复上下文用量圆环：取最后一条带 token_usage 步骤的记录
        // token_usage 事件在流式时已随其他 SSE 事件一并存入 agent_steps（后端 _stream_response）
        let restoredUsage: { current: number; max: number } | null = null
        for (const m of msgs) {
          if (!m.agent_steps || !Array.isArray(m.agent_steps)) continue
          for (const step of m.agent_steps) {
            if (step.type === 'token_usage' && typeof step.max_context_tokens === 'number') {
              restoredUsage = {
                current: step.current_context_tokens || 0,
                max: step.max_context_tokens,
              }
            }
          }
        }
        if (restoredUsage) setContextUsage(restoredUsage)

        // 恢复该会话最近一次使用的知识库选择：从最后一条带 kb 信息的消息读取。
        // kb_ids（多选，有序，首个为主库）优先；否则回退单选 kb_id。
        for (let i = msgs.length - 1; i >= 0; i--) {
          const m = msgs[i]
          if (m.kb_ids && m.kb_ids.length > 0) {
            setSelectedKbIds(m.kb_ids.filter(Boolean))
            break
          }
          if (m.kb_id) {
            setSelectedKbIds([m.kb_id])
            break
          }
        }
      } catch (e) {
        console.error('加载会话消息失败', e)
        setMessages([])
      } finally {
        setIsLoadingMessages(false)
      }
    }
    loadMessages()
  }, [currentSessionId])

  // 点击「新对话」时强制清空当前页面所有本地状态（输入框、暂存文件、消息、引用展开态、
  // 图片预览等）。用 nonce 触发：即使已处于空会话（currentSessionId 不变）也能重置。
  useEffect(() => {
    if (newSessionNonce === 0) return
    closeArtifact()
    setMessages([])
    setInput('')
    setPendingFiles([])
    setExpandedRefs(new Set())
    setContextUsage({ current: 0, max: 0 })
    // 释放本会话内上传的图片预览 object URL，避免内存泄漏
    Object.values(imagePreviewUrlsRef.current).forEach((url) => URL.revokeObjectURL(url))
    imagePreviewUrlsRef.current = {}
    setImagePreviewUrls({})
  }, [newSessionNonce])

  // 发送消息
  async function handleSend(
    overrideQuery?: string,
    overrides?: { attachments?: MessageAttachment[]; kbIds?: string[] },
  ) {
    const query = (overrideQuery ?? input).trim()
    if (!query || isStreaming) return
    // 已有后台流式在跑（用户可能切到别的会话查看），不允许并发发起新一轮，
    // 否则会覆盖正在进行的流式缓冲。只支持单条在飞流。
    if (streamingSessionRef.current !== null) return

    let sessionId = currentSessionId
    if (!sessionId) {
      // 先用临时 id 同步插入侧栏（不等任何网络往返）：以用户问题作占位标题，
      // 让新会话 item 立即出现。create 返回后再把临时 id 替换成真实 id。
      const tempId = `temp-${Date.now()}`
      const nowIso = new Date().toISOString()
      addOptimisticSession({
        id: tempId,
        title: truncateSessionTitle(query),
        kb_id: null,
        model_config_id: null,
        message_count: 1,
        created_at: nowIso,
        updated_at: nowIso,
      })
      try {
        const session = await sessionApi.create({ title: '新对话' })
        sessionId = session.id
        // 标记该会话为本地发起发送，避免 setCurrentSessionId 触发的 loadMessages 覆盖本地消息
        pendingSendSessionRef.current = session.id
        setCurrentSessionId(session.id)
        // 临时项替换为真实会话（保留问题占位标题；后续 refreshSessions 用服务端数据覆盖）。
        replaceOptimisticSession(tempId, { ...session, title: truncateSessionTitle(query) })
      } catch (e) {
        console.error('自动创建会话失败', e)
        // 回滚临时项，避免侧栏残留无效会话。
        removeOptimisticSession(tempId)
        setIsStreaming(false)
        return
      }
    } else {
      // 已有会话内发送：同样标记，防止其它原因触发的重载覆盖流式消息
      pendingSendSessionRef.current = sessionId
    }

    const userMessage: Message = { role: 'user', content: query }
    // 绑定当前暂存的会话文件为本条用户消息的附件（已建索引完成的；上传中的不绑）。
    // 重试场景由 overrides.attachments 显式传入（避免依赖尚未刷新的派生 stagedFiles）。
    const boundAttachments: MessageAttachment[] =
      overrides?.attachments ??
      stagedFiles.map((f) => ({
        file_id: f.id,
        filename: f.filename,
        file_size: f.file_size,
        file_type: f.file_type,
      }))
    if (boundAttachments.length > 0) userMessage.attachments = boundAttachments

    const assistantMessage: Message = { role: 'assistant', content: '', references: [] }

    // 该轮流式输出绑定的会话 id（闭包内固定，不随用户切换会话变化）。
    const streamSessionId = sessionId!
    // 初始化该会话的流式缓冲（权威副本）：以「当前消息 + 本轮 user + 空 assistant」为基线。
    // 之后所有流式增量都写进这里；仅当该会话在前台时才镜像到 messages 状态，实现会话隔离。
    const baseMessages = [...messagesRef.current, userMessage, assistantMessage]
    streamRef.current = { sessionId: streamSessionId, messages: baseMessages }
    streamingSessionRef.current = streamSessionId
    setMessages(baseMessages)
    // 发起新一轮：恢复粘附底部，并把侧边锚点立即定位到这条新提问
    stickToBottomRef.current = true
    setActiveAnchorIndex(baseMessages.length - 2) // 末尾是空 assistant，其前一条即本轮 user
    setInput('')
    setIsStreaming(true)

    // 会话隔离的消息更新器：增量始终写入该会话的流式缓冲；
    // 仅当该会话仍是前台展示会话时，才同步到可见的 messages 状态。
    // 这样后台流式不会污染用户切过去查看的其它会话。
    const updateStream = (mutate: (prev: Message[]) => Message[]) => {
      if (!streamRef.current || streamRef.current.sessionId !== streamSessionId) return
      const next = mutate(streamRef.current.messages)
      streamRef.current.messages = next
      if (currentSessionIdRef.current === streamSessionId) {
        setMessages(next)
      }
    }
    // 会话隔离的上下文用量更新器：仅在该会话前台时更新进度圆环。
    const updateUsage = (usage: { current: number; max: number }) => {
      if (currentSessionIdRef.current === streamSessionId) {
        setContextUsage(usage)
      }
    }

    try {
      // 合并知识库选择映射到后端契约（保持既有路由语义不变）：
      // - 0 个库 → 都不传；1 个库 → 仅 knowledge_base_id（走 SINGLE_KB，保留单库查询理解链路）；
      // - 2+ 个库 → kb_ids（走 MULTI_KB），数组首个即权重 1.0 的主库。
      // 重试场景由 overrides.kbIds 显式传入（沿用该轮原始知识库，而非当前选择器状态）。
      const effectiveKbIds = overrides?.kbIds ?? selectedKbIds
      const primaryKb = effectiveKbIds[0] || undefined
      const multiKbIds = effectiveKbIds.length > 1 ? effectiveKbIds : undefined

      // 本轮问答流的中止句柄：点「停止」按钮时 abort，断开 SSE 连接，
      // 后端据此取消 Agent 执行并把已生成的部分答案落库。
      const abortController = new AbortController()
      chatAbortRef.current = abortController

      const response = await fetch('/api/chat/completions', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        signal: abortController.signal,
        body: JSON.stringify({
          model: 'rag',
          messages: [{ role: 'user', content: query }],
          stream: true,
          knowledge_base_id: primaryKb,
          model_config_id: selectedModel || undefined,
          agent_preset_id: selectedPreset || undefined,
          kb_ids: multiKbIds,
          session_id: sessionId || undefined,
          timezone_name: browserTimezone(),
          attachments: boundAttachments.length > 0 ? boundAttachments : undefined,
        }),
      })

      if (response.status === 401) {
        handleUnauthorized()
        throw new Error('登录态已失效，请重新登录')
      }
      if (!response.ok) throw new Error(`请求失败: ${response.status}`)

      const reader = response.body?.getReader()
      const decoder = new TextDecoder()
      let fullContent = ''
      let references: Reference[] = []
      let agentSteps: Message['agentSteps'] = []
      let segments: ContentSegment[] = []
      let buffer = ''
      let isAgentMode = false
      let totalDurationMs: number | undefined = undefined
      // 检索降级标志（来自 meta 事件 metadata）：区分会话文件源 / 知识库源失败（Req 2.x）
      let sessionSourceFailed = false
      let kbSourceFailed = false
      // 本轮 assistant 消息落库后的 DB ID（message_saved 事件回填），供反馈/重试定位
      let savedMessageId: string | undefined = undefined
      // 本轮是否为错误结果（error 事件或请求异常）：动作栏仅显示重试
      let isError = false
      // 后端在生成回答前已入库用户消息并播种标题；首个流式分片到达时刷新侧栏，
      // 让新会话（及其问题标题）立即出现，无需等 AI 答完。
      let sidebarRefreshed = false

      if (reader) {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          if (!sidebarRefreshed) {
            sidebarRefreshed = true
            refreshSessions()
          }

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue
            const data = line.slice(6).trim()
            if (data === '[DONE]') continue

            try {
              const parsed = JSON.parse(data)

              // 检索降级元数据（meta 事件携带 metadata；区分会话文件源 / 知识库源失败，Req 2.x）。
              // 任何带 metadata 的事件都提取，挂到当前 assistant 消息以渲染分类提示。
              if (parsed.metadata && typeof parsed.metadata === 'object') {
                if (parsed.metadata.session_source_failed) sessionSourceFailed = true
                if (parsed.metadata.kb_source_failed) kbSourceFailed = true
                if (sessionSourceFailed || kbSourceFailed) {
                  updateStream((prev) => {
                    const updated = [...prev]
                    const last = updated[updated.length - 1]
                    if (last && last.role === 'assistant') {
                      updated[updated.length - 1] = { ...last, sessionSourceFailed, kbSourceFailed }
                    }
                    return updated
                  })
                }
              }

              // Task 10.1: 检测新 Agent SSE 事件格式（有 type 字段）
              if (parsed.type) {
                isAgentMode = true

                switch (parsed.type) {
                  // 推理过程：追加到最后一个 reasoning 段落或新建
                  case 'reasoning_delta':
                  case 'thought': {
                    if (parsed.content) {
                      const lastSeg = segments[segments.length - 1]
                      if (lastSeg && lastSeg.type === 'reasoning') {
                        segments = [...segments.slice(0, -1), { ...lastSeg, content: lastSeg.content + parsed.content }]
                      } else {
                        segments = [...segments, { type: 'reasoning', content: parsed.content }]
                      }
                    }
                    updateStream((prev) => {
                      const updated = [...prev]
                      updated[updated.length - 1] = {
                        role: 'assistant',
                        content: fullContent,
                        references,
                        segments,
                      }
                      return updated
                    })
                    break
                  }

                  // 工具调用开始：插入 tool_call 段落
                  case 'tool_call': {
                    segments = [...segments, {
                      type: 'tool_call',
                      content: parsed.tool_name || '',
                      toolCallId: parsed.tool_call_id,
                      toolName: parsed.tool_name,
                      toolArgs: parsed.arguments,
                    }]
                    updateStream((prev) => {
                      const updated = [...prev]
                      updated[updated.length - 1] = {
                        role: 'assistant',
                        content: fullContent,
                        references,
                        segments,
                      }
                      return updated
                    })
                    break
                  }

                  // 工具调用结果：更新匹配的 tool_call 段落
                  case 'tool_result': {
                    segments = segments.map((seg) =>
                      seg.type === 'tool_call' && seg.toolCallId === parsed.tool_call_id
                        ? {
                            ...seg,
                            success: parsed.success,
                            durationMs: parsed.duration_ms,
                            files: parsed.files,
                            structuredContent: isStructuredToolContent(parsed.structured_content)
                              ? parsed.structured_content
                              : undefined,
                          }
                        : seg
                    )
                    updateStream((prev) => {
                      const updated = [...prev]
                      updated[updated.length - 1] = {
                        role: 'assistant',
                        content: fullContent,
                        references,
                        segments,
                      }
                      return updated
                    })
                    break
                  }

                  // 正文流式渲染：追加到最后一个 text 段落或新建
                  case 'text_delta':
                  case 'final_answer': {
                    if (parsed.content) {
                      fullContent += parsed.content
                      const lastSeg = segments[segments.length - 1]
                      if (lastSeg && lastSeg.type === 'text') {
                        segments = [...segments.slice(0, -1), { ...lastSeg, content: lastSeg.content + parsed.content }]
                      } else {
                        segments = [...segments, { type: 'text', content: parsed.content }]
                      }
                    } else if (parsed.done) {
                      // 旧版 final_answer done 帧仅用于历史兼容。
                      const lastSeg = segments[segments.length - 1]
                      if (lastSeg && lastSeg.type === 'reasoning') {
                        segments = [...segments.slice(0, -1), { ...lastSeg, type: 'text' }]
                        fullContent = lastSeg.content
                      }
                    }
                    updateStream((prev) => {
                      const updated = [...prev]
                      updated[updated.length - 1] = {
                        role: 'assistant',
                        content: fullContent,
                        references,
                        segments,
                      }
                      return updated
                    })
                    break
                  }

                  // 引用来源
                  case 'references': {
                    if (parsed.references && Array.isArray(parsed.references)) {
                      references = parsed.references
                    }
                    updateStream((prev) => {
                      const updated = [...prev]
                      updated[updated.length - 1] = {
                        role: 'assistant',
                        content: fullContent,
                        references,
                        segments,
                      }
                      return updated
                    })
                    break
                  }

                  // 执行完成
                  case 'complete': {
                    if (typeof parsed.total_duration_ms === 'number') {
                      totalDurationMs = parsed.total_duration_ms
                      updateStream((prev) => {
                        const updated = [...prev]
                        updated[updated.length - 1] = {
                          role: 'assistant',
                          content: fullContent,
                          references,
                          segments,
                          totalDurationMs,
                        }
                        return updated
                      })
                    }
                    break
                  }

                  // 上下文 token 用量：更新进度圆环
                  case 'token_usage': {
                    updateUsage({
                      current: parsed.current_context_tokens,
                      max: parsed.max_context_tokens,
                    })
                    break
                  }

                  // 错误事件
                  case 'error': {
                    isError = true
                    fullContent = `⚠️ ${parsed.content || '执行出错'}`
                    segments = [...segments, { type: 'text', content: fullContent }]
                    updateStream((prev) => {
                      const updated = [...prev]
                      updated[updated.length - 1] = {
                        role: 'assistant',
                        content: fullContent,
                        references,
                        segments,
                      }
                      return updated
                    })
                    break
                  }

                  // 助手消息已落库：回填 DB ID，供反馈/重试定位
                  case 'message_saved': {
                    if (parsed.message_id) {
                      savedMessageId = parsed.message_id as string
                      updateStream((prev) => {
                        const updated = [...prev]
                        const last = updated[updated.length - 1]
                        if (last && last.role === 'assistant') {
                          updated[updated.length - 1] = { ...last, id: savedMessageId }
                        }
                        return updated
                      })
                    }
                    break
                  }
                }
                continue
              }

              // Agent 模式下的 references 事件（无 type 字段，直接包含 references 数组）
              if (isAgentMode && parsed.references && Array.isArray(parsed.references)) {
                references = parsed.references
                updateStream((prev) => {
                  const updated = [...prev]
                  updated[updated.length - 1] = {
                    role: 'assistant',
                    content: fullContent,
                    references,
                    segments,
                  }
                  return updated
                })
                continue
              }

              // 旧格式兼容：agent_progress 事件（非 agent 模式下保留）
              if (parsed.type === 'agent_progress') {
                agentSteps = [...(agentSteps || []), { step: parsed.step, detail: parsed.detail }]
                updateStream((prev) => {
                  const updated = [...prev]
                  updated[updated.length - 1] = { role: 'assistant', content: fullContent, references, agentSteps }
                  return updated
                })
                continue
              }

              // 非 agent 模式：ChatCompletionChunk 格式（direct/hybrid 模式）
              if (!isAgentMode) {
                const delta = parsed.choices?.[0]?.delta?.content
                if (delta) {
                  fullContent += delta
                  updateStream((prev) => {
                    const updated = [...prev]
                    updated[updated.length - 1] = { role: 'assistant', content: fullContent, references, agentSteps }
                    return updated
                  })
                }
                // 非 agent 模式下的 references（旧格式，直接在 JSON 中）
                if (parsed.references) {
                  references = parsed.references
                  updateStream((prev) => {
                    const updated = [...prev]
                    updated[updated.length - 1] = { role: 'assistant', content: fullContent, references, agentSteps }
                    return updated
                  })
                }
              }
            } catch { /* 忽略解析错误 */ }
          }
        }
      }

      // 最终状态更新
      if (isAgentMode) {
        updateStream(() => {
          const updated = [...streamRef.current!.messages]
          updated[updated.length - 1] = {
            role: 'assistant',
            content: fullContent,
            id: savedMessageId,
            isError,
            references,
            segments,
            totalDurationMs,
            sessionSourceFailed,
            kbSourceFailed,
          }
          return updated
        })
      } else {
        updateStream(() => {
          const updated = [...streamRef.current!.messages]
          updated[updated.length - 1] = { role: 'assistant', content: fullContent, id: savedMessageId, isError, references, agentSteps, sessionSourceFailed, kbSourceFailed }
          return updated
        })
      }
    } catch (error) {
      // 用户点「停止」：abort 触发的 AbortError 不是错误，保留已流式产出的部分答案，
      // 仅追加一个轻量「已停止」标记，不覆盖成错误气泡。
      if (error instanceof DOMException && error.name === 'AbortError') {
        updateStream((prev) => {
          const updated = [...prev]
          const last = updated[updated.length - 1]
          if (last && last.role === 'assistant') {
            updated[updated.length - 1] = { ...last, stopped: true }
          }
          return updated
        })
      } else {
        const errMsg = error instanceof Error ? error.message : '请求失败'
        updateStream((prev) => {
          const updated = [...prev]
          updated[updated.length - 1] = { role: 'assistant', content: `⚠️ ${errMsg}`, isError: true }
          return updated
        })
      }
    } finally {
      // 清理本轮中止句柄（单条在飞流，本轮结束即可清）。
      chatAbortRef.current = null
      // 仅当本轮流式仍是「最新发起的那一轮」时才清流式态（避免误清掉之后又发起的新一轮）。
      if (streamingSessionRef.current === streamSessionId) {
        streamingSessionRef.current = null
        streamRef.current = null
        // 仅当用户当前仍在看这个会话时，才关闭前台的流式 UI 态。
        if (currentSessionIdRef.current === streamSessionId) {
          setIsStreaming(false)
        }
      }
      refreshSessions()
      // 清除本地发送标记：之后再切回该会话时正常从服务端加载
      if (pendingSendSessionRef.current === streamSessionId) {
        pendingSendSessionRef.current = null
      }
    }
  }

  // 停止当前流式输出：中止在飞的问答请求。后端收到断流后取消 Agent 执行，
  // 并把已生成的部分答案落库；前端保留已显示内容并标记「已停止」。
  function handleStop() {
    chatAbortRef.current?.abort()
  }

  // 设置/取消 AI 回答反馈（点赞/踩）。乐观更新本地状态，失败回滚。
  async function handleFeedback(message: Message, feedback: 'like' | 'dislike' | null) {
    if (!message.id || !currentSessionId) return
    const prevFeedback = message.feedback ?? null
    setMessages((prev) =>
      prev.map((m) => (m.id === message.id ? { ...m, feedback } : m))
    )
    try {
      await sessionApi.setMessageFeedback(currentSessionId, message.id, feedback)
    } catch (e) {
      // 回滚
      setMessages((prev) =>
        prev.map((m) => (m.id === message.id ? { ...m, feedback: prevFeedback } : m))
      )
      toast.error(e instanceof Error ? e.message : '操作失败')
    }
  }

  // 重试最新一轮：先调后端删除该轮 user+assistant 消息，再用原问题、知识库与附件重新发起。
  async function handleRetry() {
    if (!currentSessionId || isStreaming) return
    try {
      const retry = await sessionApi.retryLastRound(currentSessionId)
      // 本地移除最后一轮（最后一条 user 及其之后的所有消息）
      pendingSendSessionRef.current = currentSessionId
      // 关键：同步裁剪 messagesRef（镜像）。messagesRef 只由 [messages] 的副作用在
      // 渲染后更新，而本函数紧接着 await handleSend()，后者以 messagesRef.current 为基线
      // 播种本轮消息。若不同步裁剪，handleSend 会读到「未裁剪」的旧列表（含旧 user+
      // assistant），导致旧轮残留 + 重复追加新 query。故在此即时裁剪 ref 与 state 一致。
      const trimmed = (() => {
        const prev = messagesRef.current
        let lastUserIdx = -1
        for (let i = prev.length - 1; i >= 0; i--) {
          if (prev[i].role === 'user') { lastUserIdx = i; break }
        }
        return lastUserIdx >= 0 ? prev.slice(0, lastUserIdx) : prev
      })()
      messagesRef.current = trimmed
      setMessages(trimmed)
      // 沿用该轮原始知识库（kb_ids 优先，回退单选 kb_id），同步选择器并显式传给重发，
      // 避免依赖尚未刷新的派生状态导致知识库/附件回落到输入框。
      const retryKbIds = (retry.kb_ids && retry.kb_ids.length > 0)
        ? retry.kb_ids.filter(Boolean)
        : (retry.kb_id ? [retry.kb_id] : [])
      setSelectedKbIds(retryKbIds)
      // 恢复该轮绑定的附件供重发（原样带回用户气泡，而非落入输入框暂存区）。
      const retryAttachments = retry.attachments ?? []
      await handleSend(retry.content, { attachments: retryAttachments, kbIds: retryKbIds })
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '重试失败')
    }
  }

  // 交互回调
  function toggleRef(index: number) {
    setExpandedRefs((prev) => {
      const next = new Set(prev)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })
  }

  function toggleKb(kbId: string) {
    setSelectedKbIds((prev) =>
      prev.includes(kbId) ? prev.filter((id) => id !== kbId) : [...prev, kbId]
    )
  }

  // ==========================================================================
  // 会话文件上传 / 移除（session-file-upload Task 16）
  //
  // 设计：上传时若用户尚未开会话，先调 POST /sessions 建会话，再 POST 文件；
  // 同步建索引完成后由后端返回 SessionFileResponse，react-query 刷新列表 +
  // 清掉本地占位；并发上传按文件逐个 await 排队，避免同时打多份解析占用 API
  // 进程的 embed 信号量。失败则将占位状态置 failed + toast 后端友好中文 detail。
  // ==========================================================================

  async function ensureSessionId(): Promise<string | null> {
    if (currentSessionId) return currentSessionId
    try {
      const session = await sessionApi.create({ title: '新对话' })
      pendingSendSessionRef.current = session.id
      setCurrentSessionId(session.id)
      refreshSessions()
      return session.id
    } catch (err) {
      console.error('自动创建会话失败', err)
      toast.error(err instanceof Error ? err.message : '创建会话失败')
      return null
    }
  }

  async function handleUploadSessionFiles(files: FileList) {
    // 关键：在任何 await 之前同步拷贝 FileList。files 是 input.files 的实时引用，
    // ChatInput 在调用本函数后会立即执行 e.target.value=''（为支持重复选同名文件），
    // 该操作会清空这个 FileList。若等到 await ensureSessionId() 之后再读，files 已为空，
    // 导致上传循环不执行（表现为：会话建了、列表查了，但上传 POST 永不发出）。
    const fileArr = Array.from(files)
    if (fileArr.length === 0) return

    const sessionId = await ensureSessionId()
    if (!sessionId) return

    // 逐个串行上传（同步建索引耗时较长，避免并发打爆 API 进程的 embed 信号量）
    for (const file of fileArr) {
      const localId = `local_${Date.now()}_${Math.random().toString(36).slice(2)}`
      // 图片：就地生成预览 object URL（服务端临时文件处理后即删，事后无法回源取图）
      if (isImageFilename(file.name)) {
        const url = URL.createObjectURL(file)
        // 同名旧预览先释放再覆盖
        const old = imagePreviewUrlsRef.current[file.name]
        if (old) URL.revokeObjectURL(old)
        imagePreviewUrlsRef.current = { ...imagePreviewUrlsRef.current, [file.name]: url }
        setImagePreviewUrls((prev) => ({ ...prev, [file.name]: url }))
      }
      setPendingFiles((prev) => [
        ...prev,
        { localId, filename: file.name, size: file.size, status: 'uploading' },
      ])
      // 该次上传的中止句柄：用户在上传中点取消时 abort
      const controller = new AbortController()
      uploadControllersRef.current[localId] = controller
      try {
        await sessionFileApi.upload(sessionId, file, controller.signal)
        // 成功：刷新服务端列表 + 清占位（让服务端条目无缝替换）
        await queryClient.invalidateQueries({ queryKey: ['session-files', sessionId] })
        setPendingFiles((prev) => prev.filter((p) => p.localId !== localId))
      } catch (err) {
        // 用户主动取消：静默清掉占位，不报错（占位已在取消处理器中移除）
        if (err instanceof DOMException && err.name === 'AbortError') {
          setPendingFiles((prev) => prev.filter((p) => p.localId !== localId))
        } else {
          const msg = err instanceof Error ? err.message : '上传失败'
          toast.error(msg)
          // 失败的占位保留并标红，让用户能看到失败原因；点击取消才本地清掉
          setPendingFiles((prev) =>
            prev.map((p) =>
              p.localId === localId ? { ...p, status: 'failed', errorMessage: msg } : p
            )
          )
        }
      } finally {
        delete uploadControllersRef.current[localId]
      }
    }
  }

  async function handleRemoveSessionFile(fileId: string) {
    if (!currentSessionId) return
    const target = sessionFiles.find((f) => f.id === fileId)
    const ok = await confirm({
      title: '移除会话文件',
      description: (
        <>
          确定要从本会话移除文件{target ? `「${target.filename}」` : ''}吗？该文件的索引将被删除，后续问答不会再命中其内容。
        </>
      ),
      confirmText: '移除',
    })
    if (!ok) return
    try {
      await sessionFileApi.remove(currentSessionId, fileId)
      await queryClient.invalidateQueries({ queryKey: ['session-files', currentSessionId] })
      // 释放该文件对应的图片预览 object URL（若有）
      if (target && imagePreviewUrlsRef.current[target.filename]) {
        URL.revokeObjectURL(imagePreviewUrlsRef.current[target.filename])
        const { [target.filename]: _removed, ...rest } = imagePreviewUrlsRef.current
        imagePreviewUrlsRef.current = rest
        setImagePreviewUrls(rest)
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '移除失败')
    }
  }

  function handleDismissPendingSessionFile(localId: string) {
    setPendingFiles((prev) => prev.filter((p) => p.localId !== localId))
  }

  // 取消一个上传中的占位：中止在飞的 POST，并立即清掉占位。
  // 占位清理也由 upload 的 AbortError 分支兜底，这里先行移除让 UI 即时响应。
  function handleCancelPendingSessionFile(localId: string) {
    const controller = uploadControllersRef.current[localId]
    if (controller) {
      controller.abort()
      delete uploadControllersRef.current[localId]
    }
    setPendingFiles((prev) => prev.filter((p) => p.localId !== localId))
  }

  const selectedModelName = llmConfigs.find((c) => c.id === selectedModel)?.name || ''

  // 已被某条用户消息"消费"的会话文件 ID（发送时绑定为附件后，从输入区清出）。
  // 单一数据源：从 messages 的 attachments 派生，刷新历史后同样成立。
  const consumedFileIds = useMemo(() => {
    const ids = new Set<string>()
    for (const m of messages) {
      if (m.attachments) for (const a of m.attachments) ids.add(a.file_id)
    }
    return ids
  }, [messages])

  // 输入区仅展示"尚未随消息发出"的会话文件（已发送的随气泡上移）。
  // 用 merge 后的列表，chip 才能实时反映建索引进度。
  const stagedFiles = useMemo(
    () => mergedSessionFiles.filter((f) => !consumedFileIds.has(f.id)),
    [mergedSessionFiles, consumedFileIds]
  )

  const isEmpty = messages.length === 0

  // 侧边锚点：每条 user 消息一个锚点，文本取该轮提问内容
  const queryAnchors = useMemo<QueryAnchor[]>(
    () =>
      messages
        .map((m, idx) => ({ m, idx }))
        .filter(({ m }) => m.role === 'user')
        .map(({ m, idx }) => ({ index: idx, text: m.content })),
    [messages]
  )

  // 共用的输入框 props
  const chatInputProps = {
    input,
    isStreaming,
    selectedKbIds,
    selectedModel,
    selectedModelName,
    selectedPreset,
    contextUsage,
    knowledgeBases,
    llmConfigs,
    agentPresets,
    onInputChange: setInput,
    onSend: handleSend,
    onStop: handleStop,
    onToggleKb: toggleKb,
    onModelChange: setSelectedModel,
    onPresetChange: setSelectedPreset,
    // 会话文件上传：始终可用（即使未选 KB，Req 1.4），仅在流式或建索引时禁用由组件内判断
    // 输入区只展示"尚未随消息发出"的暂存文件；已发送的随用户气泡上移。
    sessionFiles: stagedFiles,
    pendingSessionFiles: pendingFiles,
    canUploadSessionFile: !isStreaming,
    onUploadSessionFiles: handleUploadSessionFiles,
    onRemoveSessionFile: handleRemoveSessionFile,
    onCancelPendingSessionFile: handleCancelPendingSessionFile,
    onDismissPendingSessionFile: handleDismissPendingSessionFile,
    sessionImagePreviewUrls: imagePreviewUrls,
    sessionId: currentSessionId,
  }

  // 空态：标题 + 提问示例气泡 + 居中输入框
  // 加载历史消息期间不显示空态，避免「Artoo 欢迎页」闪现
  if (isEmpty && !isLoadingMessages) {
    return (
      <div className="relative h-full flex flex-col items-center justify-center px-4 overflow-hidden">
        {/* 新对话页背景：Side Rays 动态光线（reactbits）。仅暗色模式显示，浅色模式会蒙灰故隐藏 */}
        <div className="pointer-events-none absolute inset-0 z-0 hidden dark:block">
          <SideRays />
        </div>
        <div className="relative z-10 w-full max-w-3xl flex flex-col items-center -mt-12">
          <h1 className="text-3xl font-semibold text-foreground text-center mb-8">
            我是 <span className="font-serif">Artoo</span>，你的知识库问答助手
          </h1>

          <div className="mb-8 w-full">
            <SuggestedQuestions onSelect={(q) => setInput(q)} />
          </div>

          <ChatInput {...chatInputProps} centered />
        </div>
      </div>
    )
  }

  return (
    <div className="h-full flex flex-col relative">
      {/* 消息列表 */}
      <div ref={scrollContainerRef} className="flex-1 overflow-auto pb-44">
        {isLoadingMessages ? (
          <ChatMessagesSkeleton />
        ) : (
          <div ref={contentRef} className="max-w-3xl mx-auto py-6 px-4 space-y-5 animate-in fade-in-0 duration-500">
            {(() => {
              // 最新一条 assistant 消息的下标：仅它可重试（历史轮不可重试，避免破坏后续历史）
              let lastAssistantIdx = -1
              for (let i = messages.length - 1; i >= 0; i--) {
                if (messages[i].role === 'assistant') { lastAssistantIdx = i; break }
              }
              return messages.map((msg, idx) => (
                <div
                  key={idx}
                  ref={(node) => {
                    if (msg.role === 'user') messageRefs.current[idx] = node
                  }}
                >
                  <MessageBubble
                    message={msg}
                    index={idx}
                    isStreaming={isStreaming}
                    isLast={idx === messages.length - 1}
                    isLastAssistant={idx === lastAssistantIdx && !isStreaming}
                    expandedRefs={expandedRefs}
                    onToggleRef={toggleRef}
                    imagePreviewUrls={imagePreviewUrls}
                    sessionId={currentSessionId}
                    sessionFiles={mergedSessionFiles}
                    onFeedback={handleFeedback}
                    onRetry={handleRetry}
                  />
                </div>
              ))
            })()}
          </div>
        )}
      </div>

      {/* 浮层：与对话列表共用 max-w-3xl 居中容器，使锚点列 / 回到底部按钮与「对话框」右边对齐 */}
      <div className="pointer-events-none absolute inset-0 z-20">
        <div className="relative mx-auto h-full max-w-3xl px-4">
          {/* 侧边锚点导航：贴对话框右侧外缘，每轮提问一个圆点，点击定位 */}
          <div className="pointer-events-none absolute left-full top-1/2 ml-3 -translate-y-1/2">
            <ChatAnchorRail
              anchors={queryAnchors}
              activeIndex={activeAnchorIndex}
              onJump={scrollToMessage}
            />
          </div>

          {/* 回到底部按钮：离开底部后浮现于输入框上方、对话框右边对齐 */}
          <button
            type="button"
            aria-label="回到底部"
            onClick={scrollToBottom}
            className={cn(
              'absolute bottom-32 right-4 flex h-9 w-9 items-center justify-center rounded-full border border-border bg-card text-muted-foreground shadow-lg transition-all duration-300 ease-out hover:text-foreground hover:shadow-xl',
              showScrollToBottom
                ? 'pointer-events-auto translate-y-0 opacity-100'
                : 'pointer-events-none translate-y-2 opacity-0'
            )}
          >
            <ChevronDown className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* 输入区域 */}
      <ChatInput {...chatInputProps} />
    </div>
  )
}

export default Chat
