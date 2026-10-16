import { useEffect, useRef, useState } from 'react'
import {
  Folder,
  FolderOpen,
  FileText,
  Brain,
  Search,
  Regex,
  BookOpen,
  Globe,
  Sparkles,
  CheckCircle2,
  Send,
} from 'lucide-react'

// 左侧技能列表（贴合 Artoo 预置 Skills）。id 用于与对话脚本联动高亮。
const SKILLS = [
  { id: 'document-analyzer', name: 'document-analyzer' },
  { id: 'comparison-analyst', name: 'comparison-analyst' },
  { id: 'citation-generator', name: 'citation-generator' },
  { id: 'table-extractor', name: 'table-extractor' },
  { id: 'meeting-summarizer', name: 'meeting-summarizer' },
]

// 对话脚本：模拟一次完整 ReAct 检索循环（结合 Artoo 真实工具/技能/事件）
type Step =
  | { kind: 'user'; text: string }
  | { kind: 'info'; text: string }
  | { kind: 'thought'; text: string }
  | {
      kind: 'tool'
      icon: 'search' | 'regex' | 'read' | 'web' | 'skill'
      label: string
      detail: string
      skill?: string // 触发/高亮的技能 id
    }
  | { kind: 'result'; text: string }
  | { kind: 'answer'; text: string }

const SCRIPT: Step[] = [
  { kind: 'user', text: '对比两版产品需求文档的差异，并标注引用来源' },
  { kind: 'info', text: '匹配到 2 个技能：document-analyzer、comparison-analyst' },
  { kind: 'thought', text: '先评估任务：需读取两份文档并逐节对比，加载技能获取分析方法。' },
  {
    kind: 'tool',
    icon: 'skill',
    label: 'read_skill',
    detail: '加载 comparison-analyst/SKILL.md',
    skill: 'comparison-analyst',
  },
  {
    kind: 'tool',
    icon: 'search',
    label: 'search_knowledge',
    detail: 'query="产品需求 V1 V2 变更" · hybrid 三路召回',
  },
  { kind: 'result', text: 'RRF 融合 + Rerank 命中 12 个子块，父块扩展为 5 段上下文' },
  { kind: 'tool', icon: 'regex', label: 'search_knowledge', detail: 'mode="keyword" 精确定位变更点' },
  { kind: 'thought', text: '已锁定 3 处关键差异，深读对应父块确认细节与出处。' },
  {
    kind: 'tool',
    icon: 'read',
    label: 'read_document',
    detail: 'doc="PRD_V2.docx" 顺序精读第 3-4 节',
  },
  { kind: 'result', text: '提取到差异明细与对应 chunk 编号，可生成引用' },
  {
    kind: 'tool',
    icon: 'skill',
    label: 'read_skill',
    detail: '加载 citation-generator 规范化引用格式',
    skill: 'citation-generator',
  },
  {
    kind: 'answer',
    text: '两版共 3 处主要差异：①新增「离线部署」需求 ②删除旧鉴权方案 ③调整 SLA 至 99.9%。已附引用来源 [PRD_V2 §3.2] [PRD_V1 §2.1]。',
  },
]

// 默认激活的技能（仅一个）
const DEFAULT_SKILL = 'document-analyzer'

const TOOL_ICONS = {
  search: Search,
  regex: Regex,
  read: BookOpen,
  web: Globe,
  skill: Sparkles,
} as const

function StepRow({ step }: { step: Step }) {
  if (step.kind === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-2xl rounded-tr-sm bg-[#3b82f6] px-4 py-2.5 text-sm text-white shadow-lg">
          {step.text}
        </div>
      </div>
    )
  }
  if (step.kind === 'info') {
    return (
      <div className="flex items-center gap-2 border-b border-white/5 pb-2 text-sm text-[#65bd43]">
        <Sparkles className="h-4 w-4 shrink-0" />
        <span>{step.text}</span>
      </div>
    )
  }
  if (step.kind === 'thought') {
    return (
      <div className="flex items-start gap-2 text-sm text-white/55">
        <Brain className="mt-0.5 h-4 w-4 shrink-0 text-white/35" />
        <span className="italic">{step.text}</span>
      </div>
    )
  }
  if (step.kind === 'tool') {
    const Icon = TOOL_ICONS[step.icon]
    return (
      <div className="flex items-center gap-3 rounded-lg border border-white/8 bg-white/3 px-3 py-2.5">
        <Icon className="h-4 w-4 shrink-0 animate-[toolSpin_0.5s_ease] text-[#27ff84]" />
        <div className="min-w-0">
          <span className="font-mono text-xs font-medium text-white/90">{step.label}</span>
          <p className="truncate text-xs text-white/45">{step.detail}</p>
        </div>
      </div>
    )
  }
  if (step.kind === 'result') {
    return (
      <div className="flex items-center gap-2 pl-1 text-xs text-white/45">
        <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-[#65bd43]/70" />
        <span>{step.text}</span>
      </div>
    )
  }
  // answer
  return (
    <div className="flex justify-start">
      <div className="max-w-[88%] rounded-2xl rounded-tl-sm border border-[#27ff84]/25 bg-[#27ff84]/8 px-4 py-3 text-sm leading-relaxed text-white/90">
        {step.text}
      </div>
    </div>
  )
}

export default function AgentDemo() {
  const [visible, setVisible] = useState(0)
  const scrollRef = useRef<HTMLDivElement>(null)

  // 逐条推进，播放到结尾后停顿再循环（无暂停，一直播放）
  useEffect(() => {
    const atEnd = visible >= SCRIPT.length
    const delay = atEnd ? 2800 : visible === 0 ? 600 : 1100
    const timer = setTimeout(() => {
      setVisible((v) => (v >= SCRIPT.length ? 0 : v + 1))
    }, delay)
    return () => clearTimeout(timer)
  }, [visible])

  // 新增气泡时自动滚到底部
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [visible])

  const shown = SCRIPT.slice(0, visible)

  // 左侧高亮：取已展示步骤中最后一个被加载的技能；未触发时回退到默认技能
  const loadedSkill = (() => {
    for (let i = shown.length - 1; i >= 0; i--) {
      const s = shown[i]
      if (s.kind === 'tool' && s.skill) return s.skill
    }
    return DEFAULT_SKILL
  })()

  const activeIndex = SKILLS.findIndex((s) => s.id === loadedSkill)

  return (
    <section className="relative border-y border-white/5 bg-white/2 py-24">
      <div className="mx-auto max-w-6xl px-6">
        <div className="mb-14 text-center">
          <p className="mb-3 flex items-center justify-center gap-2 text-sm font-medium uppercase tracking-widest text-[#65bd43]">
            <Sparkles className="h-4 w-4" />
            Live Agent
          </p>
          <h2 className="text-3xl font-semibold tracking-tight sm:text-4xl">
            看 <span className="font-serif font-semibold">Artoo</span> 自主检索与推理
          </h2>
          <p className="mx-auto mt-3 max-w-2xl text-sm leading-relaxed text-white/55">
            技能按需渐进式加载——只在需要时读取。每一轮的思考、工具调用与技能加载全程可视。
          </p>
        </div>

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[300px_1fr]">
          {/* 左侧：技能列表（与右侧对话联动，高亮滑动过渡） */}
          <div className="rounded-2xl border border-white/8 bg-[#0a0f15]/60 p-5">
            <p className="mb-4 font-mono text-xs text-white/40">/mnt/skills/</p>
            <div className="relative">
              {/* 滑动高亮条：按激活技能 index 平移 */}
              <span
                className="pointer-events-none absolute left-0 right-0 h-9 rounded-md bg-[#27ff84]/10 transition-all duration-500 ease-out"
                style={{ transform: `translateY(${activeIndex * 36}px)`, opacity: activeIndex < 0 ? 0 : 1 }}
              />
              <ul className="relative space-y-0">
                {SKILLS.map((skill) => {
                  const active = skill.id === loadedSkill
                  const FolderIcon = active ? FolderOpen : Folder
                  return (
                    <li key={skill.id}>
                      <div
                        className={`flex h-9 items-center gap-2 rounded-md px-2 text-sm transition-colors duration-300 ${
                          active ? 'text-white' : 'text-white/40'
                        }`}
                      >
                        <FolderIcon
                          className={`h-4 w-4 shrink-0 transition-colors duration-300 ${
                            active ? 'text-[#27ff84]' : 'text-white/45'
                          }`}
                        />
                        <span className={active ? 'font-medium' : ''}>{skill.name}</span>
                      </div>
                      {active && (
                        <div className="ml-4 flex animate-[agentFade_0.4s_ease] items-center gap-2 px-2 py-1 text-sm text-white/70">
                          <FileText className="h-4 w-4 shrink-0 text-[#27ff84]" />
                          <span className="font-mono text-xs">SKILL.md</span>
                        </div>
                      )}
                    </li>
                  )
                })}
              </ul>
            </div>
          </div>

          {/* 右侧：模拟对话 */}
          <div className="flex h-[520px] flex-col overflow-hidden rounded-2xl border border-white/8 bg-[#0a0f15]/60">
            {/* 头部 */}
            <div className="flex items-center gap-2 border-b border-white/8 px-5 py-3.5">
              <span className="flex h-2 w-2 rounded-full bg-[#27ff84]" />
              <span className="text-sm font-medium text-white/80">
                <span className="font-serif font-semibold">Artoo</span> Agent
              </span>
            </div>

            {/* 消息区 */}
            <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-5 py-4">
              {shown.map((step, i) => (
                <div key={i} className="animate-[agentFade_0.4s_ease]">
                  <StepRow step={step} />
                </div>
              ))}
              {visible < SCRIPT.length && (
                <div className="flex items-center gap-1.5 pl-1 pt-1">
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-white/40 [animation-delay:-0.3s]" />
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-white/40 [animation-delay:-0.15s]" />
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-white/40" />
                </div>
              )}
            </div>

            {/* 输入框（装饰） */}
            <div className="border-t border-white/8 px-5 py-3.5">
              <div className="flex items-center gap-2 rounded-full border border-white/10 bg-white/3 px-4 py-2.5">
                <input
                  disabled
                  placeholder="向 Artoo 提问任何问题…"
                  className="flex-1 bg-transparent text-sm text-white/40 outline-none placeholder:text-white/25"
                />
                <Send className="h-4 w-4 text-white/30" />
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
