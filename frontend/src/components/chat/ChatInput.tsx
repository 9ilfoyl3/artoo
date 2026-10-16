import { useRef, useEffect } from 'react'
import { Send, Cpu, Bot, Paperclip, Square } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from '@/components/ui/tooltip'
import ContextUsageRing from '@/components/chat/ContextUsageRing'
import KbSelector from '@/components/chat/KbSelector'
import KbSelectionList from '@/components/chat/KbSelectionList'
import SessionFileList, { type PendingSessionFile } from '@/components/chat/SessionFileList'
import TaskList, { type TaskListItem } from '@/components/chat/TaskList'
import type { AgentPresetItem, SessionFileResponse } from '@/lib/api'

/** 会话上传支持的文件类型（与后端 _ALLOWED_EXTENSIONS 一致）。 */
const UPLOAD_ACCEPT = '.pdf,.doc,.docx,.xlsx,.pptx,.csv,.txt,.md,.jpg,.jpeg,.png,.mp3,.wav,.m4a,.flac,.ogg'
const UPLOAD_ACCEPT_LABEL = 'pdf、doc、docx、xlsx、pptx、csv、txt、md、jpg、jpeg、png、mp3、wav、m4a、flac、ogg'

interface KnowledgeBaseItem {
  id: string
  name: string
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

interface ChatInputProps {
  input: string
  isStreaming: boolean
  selectedKbIds: string[]
  selectedModel: string
  selectedModelName: string
  selectedPreset: string
  contextUsage: { current: number; max: number }
  knowledgeBases: KnowledgeBaseItem[]
  llmConfigs: LLMConfigItem[]
  agentPresets: AgentPresetItem[]
  onInputChange: (value: string) => void
  onSend: (query?: string) => void
  /** 停止当前流式输出（中止在飞请求） */
  onStop?: () => void
  onToggleKb: (kbId: string) => void
  onModelChange: (value: string) => void
  onPresetChange: (value: string) => void
  /** 居中静态布局（用于新对话空态），默认 false 时固定在底部 */
  centered?: boolean
  // ====== 会话文件上传（session-file-upload Task 16）======
  /** 已建索引完成的服务端文件列表 */
  sessionFiles?: SessionFileResponse[]
  /** 同步上传中的本地占位（POST 在飞 / 失败） */
  pendingSessionFiles?: PendingSessionFile[]
  /** 是否启用上传按钮：会话存在 + 未在流式响应中（关闭 KB 选择不影响） */
  canUploadSessionFile?: boolean
  /** 选择文件后触发同步上传 */
  onUploadSessionFiles?: (files: FileList) => void
  /** 移除单个已建索引文件 */
  onRemoveSessionFile?: (fileId: string) => void
  /** 取消一个上传中的占位（中止在飞的 POST） */
  onCancelPendingSessionFile?: (localId: string) => void
  /** 关闭一个失败占位（仅本地清理） */
  onDismissPendingSessionFile?: (localId: string) => void
  /** 文件名 → 图片预览 URL（本会话内上传的图片缩略图/放大预览） */
  sessionImagePreviewUrls?: Record<string, string>
  /** 当前会话 ID（用于 Artifact 面板按会话拉取附件原件预览） */
  sessionId?: string | null
  /** Codex-style live task pill above the composer; hover reveals the checklist. */
  todos?: TaskListItem[]
}

function ChatInput({
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
  onInputChange,
  onSend,
  onStop,
  onToggleKb,
  onModelChange,
  onPresetChange,
  centered = false,
  sessionFiles = [],
  pendingSessionFiles = [],
  canUploadSessionFile = false,
  onUploadSessionFiles,
  onRemoveSessionFile,
  onCancelPendingSessionFile,
  onDismissPendingSessionFile,
  sessionImagePreviewUrls = {},
  sessionId = null,
  todos = [],
}: ChatInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
      textareaRef.current.style.height = Math.min(textareaRef.current.scrollHeight, 120) + 'px'
    }
  }, [input])

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      onSend()
    }
  }

  function handlePickFile() {
    fileInputRef.current?.click()
  }

  function handleFileInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    const files = e.target.files
    if (files && files.length > 0 && onUploadSessionFiles) {
      onUploadSessionFiles(files)
    }
    // 重置以便相同文件名可再次选中触发 change
    if (e.target) e.target.value = ''
  }

  // 上传按钮（纯图标 + Tooltip 说明） + 隐藏 input（centered/底部两套布局共用）
  const uploadButton = (
    <>
      <TooltipProvider delayDuration={200}>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={handlePickFile}
              disabled={!canUploadSessionFile || isStreaming}
              aria-label="上传会话文件"
              className="h-7 w-7 flex items-center justify-center border-none bg-muted/50 hover:bg-muted disabled:opacity-50 disabled:cursor-not-allowed rounded-lg text-muted-foreground cursor-pointer transition-colors shrink-0"
            >
              <Paperclip className="h-3.5 w-3.5 shrink-0" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="top" className="max-w-xs">
            {canUploadSessionFile ? (
              <div className="space-y-0.5 text-xs leading-relaxed">
                <div>上传文件到本会话（无需选择知识库）</div>
                <div className="text-muted-foreground">支持类型：{UPLOAD_ACCEPT_LABEL}</div>
              </div>
            ) : (
              <div className="text-xs">请先开始一个会话再上传文件</div>
            )}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={UPLOAD_ACCEPT}
        className="hidden"
        onChange={handleFileInputChange}
      />
    </>
  )

  // 已选知识库 + 已上传文件列表（centered/底部两套布局共用）
  const fileListSlot = (
    <>
      <KbSelectionList
        knowledgeBases={knowledgeBases}
        selectedKbIds={selectedKbIds}
        onRemove={onToggleKb}
      />
      <SessionFileList
        files={sessionFiles}
        pending={pendingSessionFiles}
        onRemove={(id) => onRemoveSessionFile?.(id)}
        onCancelPending={(id) => onCancelPendingSessionFile?.(id)}
        onDismissPending={(id) => onDismissPendingSessionFile?.(id)}
        imagePreviewUrls={sessionImagePreviewUrls}
        sessionId={sessionId}
      />
    </>
  )

  // 居中静态布局（新对话空态）
  if (centered) {
    return (
      <div className="w-full">
        <TaskList todos={todos} />
        <div className="rounded-3xl border border-border bg-card shadow-lg transition-shadow focus-within:shadow-xl focus-within:border-primary/30">
          {fileListSlot}
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => onInputChange(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="输入问题，将基于知识库和网络搜索回答..."
            className="w-full px-5 pt-5 pb-2 text-sm bg-transparent border-none outline-none resize-none placeholder:text-muted-foreground/60 min-h-[60px] max-h-[160px]"
            rows={2}
            disabled={isStreaming}
          />

          <div className="flex items-center justify-between px-3.5 pb-3.5 gap-2 flex-wrap">
            {/* 左侧工具栏 */}
            <div className="flex items-center gap-2 flex-wrap">
              {uploadButton}
              <KbSelector
                knowledgeBases={knowledgeBases}
                selectedKbIds={selectedKbIds}
                onToggle={onToggleKb}
              />

              <Select value={selectedPreset} onValueChange={onPresetChange}>
                <SelectTrigger className="h-7 w-auto border-none bg-muted/50 hover:bg-muted rounded-lg px-2.5 gap-1.5 text-xs text-muted-foreground shadow-none focus:ring-0">
                  <Bot className="h-3 w-3 shrink-0" />
                  <SelectValue placeholder="选择智能体" />
                </SelectTrigger>
                <SelectContent>
                  {agentPresets.map((preset) => (
                    <SelectItem key={preset.id} value={preset.id}>{preset.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* 右侧 */}
            <div className="flex items-center gap-2">
              <ContextUsageRing
                currentTokens={contextUsage.current}
                maxTokens={contextUsage.max}
                visible={contextUsage.max > 0}
              />

              <Select value={selectedModel} onValueChange={onModelChange}>
                <SelectTrigger className="h-7 w-auto border-none bg-transparent hover:bg-muted/50 rounded-lg px-2 gap-1 text-xs text-muted-foreground shadow-none focus:ring-0">
                  <Cpu className="h-3 w-3 shrink-0" />
                  <span className="max-w-[15em] truncate">{selectedModelName || '模型'}</span>
                </SelectTrigger>
                <SelectContent>
                  {llmConfigs.length === 0 ? (
                    <div className="px-3 py-2 text-xs text-muted-foreground">暂无可用的对话模型</div>
                  ) : (
                    llmConfigs.map((config) => (
                      <SelectItem key={config.id} value={config.id}>{config.name}</SelectItem>
                    ))
                  )}
                </SelectContent>
              </Select>

              {isStreaming ? (
                <Button
                  size="icon"
                  variant="secondary"
                  className="h-9 w-9 rounded-full shrink-0"
                  onClick={() => onStop?.()}
                  aria-label="停止输出"
                >
                  <Square className="h-3.5 w-3.5 fill-current" />
                </Button>
              ) : (
                <Button
                  size="icon"
                  className="h-9 w-9 rounded-full shrink-0"
                  onClick={() => onSend()}
                  disabled={!input.trim()}
                >
                  <Send className="h-4 w-4" />
                </Button>
              )}
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="absolute bottom-0 left-0 right-0 p-4 bg-linear-to-t from-background via-background to-transparent pt-10 pointer-events-none">
      <div className="max-w-3xl mx-auto pointer-events-auto">
        <TaskList todos={todos} />
        <div className="rounded-2xl border border-border bg-card shadow-lg">
          {fileListSlot}
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => onInputChange(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="输入问题，按 Enter 发送..."
            className="w-full px-4 pt-4 pb-2 text-sm bg-transparent border-none outline-none resize-none placeholder:text-muted-foreground/60 min-h-[44px] max-h-[120px]"
            rows={1}
            disabled={isStreaming}
          />

          <div className="flex items-center justify-between px-3 pb-3 gap-2 flex-wrap">
            {/* 左侧工具栏 */}
            <div className="flex items-center gap-2 flex-wrap">
              {uploadButton}
              <KbSelector
                knowledgeBases={knowledgeBases}
                selectedKbIds={selectedKbIds}
                onToggle={onToggleKb}
              />

              <Select value={selectedPreset} onValueChange={onPresetChange}>
                <SelectTrigger className="h-7 w-auto border-none bg-muted/50 hover:bg-muted rounded-lg px-2.5 gap-1.5 text-xs text-muted-foreground shadow-none focus:ring-0">
                  <Bot className="h-3 w-3 shrink-0" />
                  <SelectValue placeholder="选择智能体" />
                </SelectTrigger>
                <SelectContent>
                  {agentPresets.map((preset) => (
                    <SelectItem key={preset.id} value={preset.id}>{preset.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* 右侧 */}
            <div className="flex items-center gap-2">
              <ContextUsageRing
                currentTokens={contextUsage.current}
                maxTokens={contextUsage.max}
                visible={contextUsage.max > 0}
              />

              <Select value={selectedModel} onValueChange={onModelChange}>
                <SelectTrigger className="h-7 w-auto border-none bg-transparent hover:bg-muted/50 rounded-lg px-2 gap-1 text-xs text-muted-foreground shadow-none focus:ring-0">
                  <Cpu className="h-3 w-3 shrink-0" />
                  <span className="max-w-[15em] truncate">{selectedModelName || '模型'}</span>
                </SelectTrigger>
                <SelectContent>
                  {llmConfigs.length === 0 ? (
                    <div className="px-3 py-2 text-xs text-muted-foreground">暂无可用的对话模型</div>
                  ) : (
                    llmConfigs.map((config) => (
                      <SelectItem key={config.id} value={config.id}>{config.name}</SelectItem>
                    ))
                  )}
                </SelectContent>
              </Select>

              {isStreaming ? (
                <Button
                  size="icon"
                  variant="secondary"
                  className="h-8 w-8 rounded-full shrink-0"
                  onClick={() => onStop?.()}
                  aria-label="停止输出"
                >
                  <Square className="h-3 w-3 fill-current" />
                </Button>
              ) : (
                <Button
                  size="icon"
                  className="h-8 w-8 rounded-full shrink-0"
                  onClick={() => onSend()}
                  disabled={!input.trim()}
                >
                  <Send className="h-4 w-4" />
                </Button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export default ChatInput
