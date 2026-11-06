import { Database, ChevronDown } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuCheckboxItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu'
import { useAuth } from '@/lib/auth-context'

interface KnowledgeBaseItem {
  id: string
  name: string
  // 归属/可见性：用于按「个人 / 组织公共 / 共享给我」分组展示
  owner_user_id?: string | null
  visibility?: string | null
}

interface KbSelectorProps {
  /** 可选知识库列表 */
  knowledgeBases: KnowledgeBaseItem[]
  /** 已选知识库 ID（按选中顺序，首个作为后端主库 kb_ids[0]，权重更高） */
  selectedKbIds: string[]
  /** 切换某个知识库的选中状态（多选） */
  onToggle: (kbId: string) => void
}

// 库分组类型：个人（自己创建）/ 组织公共 / 共享给我。
type KbGroupKey = 'mine' | 'org' | 'shared'

const GROUP_LABELS: Record<KbGroupKey, string> = {
  mine: '个人知识库',
  org: '组织公共',
  shared: '共享知识库',
}

// 分组渲染顺序：个人 > 组织公共 > 共享给我（与知识库管理页关系档位一致）。
const GROUP_ORDER: KbGroupKey[] = ['mine', 'org', 'shared']

/**
 * 知识库多选器：作为选择入口。未选时显示「知识库」文本；已选时隐藏文本，仅在数据库图标
 * 后显示一个圆形数字角标表示已选数量。选中的具体知识库由独立的 KbSelectionList 区域渲染。
 *
 * 下拉内按库类型分组（个人 / 组织公共 / 共享给我），便于区分不同来源的知识库。
 */
function KbSelector({ knowledgeBases, selectedKbIds, onToggle }: KbSelectorProps) {
  const { isOwner } = useAuth()
  const count = selectedKbIds.length

  // 单个库归类：自己创建归「个人」；否则按可见性区分组织公共 / 共享给我。
  function groupOf(kb: KnowledgeBaseItem): KbGroupKey {
    if (isOwner(kb.owner_user_id ?? null)) return 'mine'
    if (kb.visibility === 'organization') return 'org'
    return 'shared'
  }

  // 按分组聚合并保留库原有顺序。
  const grouped = GROUP_ORDER.map((key) => ({
    key,
    items: knowledgeBases.filter((kb) => groupOf(kb) === key),
  })).filter((g) => g.items.length > 0)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button className="h-7 flex items-center gap-1.5 border-none bg-muted/50 hover:bg-muted rounded-lg px-2.5 text-xs text-muted-foreground cursor-pointer transition-colors whitespace-nowrap outline-none focus:outline-none focus-visible:outline-none focus:ring-0">
          <Database className="h-3 w-3 shrink-0" />
          {count > 0 ? (
            <span className="h-4 min-w-4 px-1 flex items-center justify-center rounded-full bg-muted-foreground/20 text-foreground text-[10px] leading-none">
              {count}
            </span>
          ) : (
            <span>知识库</span>
          )}
          <ChevronDown className="h-3 w-3" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="bottom" align="start" className="max-h-96 overflow-y-auto">
        {knowledgeBases.length === 0 ? (
          <div className="px-3 py-2 text-xs text-muted-foreground">暂无可用知识库</div>
        ) : (
          grouped.map((group, idx) => (
            <div key={group.key}>
              {idx > 0 && <DropdownMenuSeparator />}
              <DropdownMenuLabel className="text-xs text-muted-foreground font-normal">
                {GROUP_LABELS[group.key]}
              </DropdownMenuLabel>
              {group.items.map((kb) => (
                <DropdownMenuCheckboxItem
                  key={kb.id}
                  checked={selectedKbIds.includes(kb.id)}
                  onCheckedChange={() => onToggle(kb.id)}
                  onSelect={(e) => e.preventDefault()}
                >
                  {kb.name}
                </DropdownMenuCheckboxItem>
              ))}
            </div>
          ))
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export default KbSelector
