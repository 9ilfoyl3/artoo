import { copyToClipboard } from '@/lib/clipboard'
import { useState, useCallback, useRef } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useQuery, useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { useArtifactStore, isPreviewable } from '@/stores/artifactStore'
import {
  Upload,
  FileText,
  ArrowLeft,
  Eye,
  FileSearch,
  Trash2,
  Copy,
  FolderPlus,
  Pencil,
  FolderInput,
  FolderUp,
  LayoutGrid,
  List,
  RotateCcw,
  CheckSquare,
  Square,
  Network,
  Link2,
  X,
  NotebookText,
} from 'lucide-react'
import { documentApi, knowledgeBaseApi, folderApi, systemApi } from '@/lib/api'
import type { PageResult, KBCapacity } from '@/lib/api'
import { useInfiniteScroll } from '@/hooks/useInfiniteScroll'
import { useConfirm } from '@/lib/confirm-context'
import { useGraphGating } from '@/components/graph/useGraphGating'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from '@/components/ui/tooltip'
import {
  ContextMenu,
  ContextMenuTrigger,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
} from '@/components/ui/context-menu'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from '@/components/ui/dialog'
import FileItem from '@/components/documents/FileItem'
import FolderItem from '@/components/documents/FolderItem'
import FolderBreadcrumb from '@/components/documents/FolderBreadcrumb'
import NewFolderDialog from '@/components/documents/NewFolderDialog'
import RenameDialog from '@/components/documents/RenameDialog'
import ChunkViewer from '@/components/documents/ChunkViewer'
import DocumentSummaryPreview from '@/components/documents/DocumentSummaryPreview'
import KBCapacityBar from '@/components/KBCapacityBar'
import DocumentGridSkeleton from '@/components/skeletons/DocumentGridSkeleton'
import TableSkeleton from '@/components/skeletons/TableSkeleton'

import type { DocumentItem, UploadingFile, MergedFile } from '@/components/documents/FileItem'
import { formatSize, statusLabel, statusColor } from '@/components/documents/FileItem'
import type { FolderData } from '@/components/documents/FolderItem'

// 知识库数据类型
interface KnowledgeBaseItem {
  id: string
  name: string
  can_write?: boolean | null
  // 容量进度条（session-file-upload Req 7）
  capacity?: KBCapacity | null
}

// 面包屑项
interface BreadcrumbItem {
  id: string | null
  name: string
}

// 工具栏按钮：artifact 打开（收起为纯图标）时套 Tooltip 显示中文名；展开时按钮自带文字，
// 直接渲染不套 Tooltip，避免多余包裹。label 同时用于纯图标态的无障碍提示。
function ToolbarTip({
  collapsed,
  label,
  children,
}: {
  collapsed: boolean
  label: string
  children: React.ReactNode
}) {
  if (!collapsed) return <>{children}</>
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

function DocumentSummaryHover({
  doc,
  children,
}: {
  doc: MergedFile
  children: React.ReactNode
}) {
  return (
    <TooltipProvider delayDuration={1000} skipDelayDuration={250}>
      <Tooltip>
        <TooltipTrigger asChild>{children}</TooltipTrigger>
        <TooltipContent
          side="right"
          align="start"
          sideOffset={8}
          collisionPadding={12}
          className="w-[300px] overflow-hidden rounded-xl border bg-popover p-0 shadow-xl"
        >
          <DocumentSummaryPreview
            docId={doc.id}
            filename={doc.filename}
            uploadedAt={doc.created_at}
          />
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

// 文档管理页面 - Finder 风格
function Documents() {
  const { id: kbId } = useParams<{ id: string }>()
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)
  const openArtifact = useArtifactStore((s) => s.openArtifact)
  // Artifact 预览面板是否打开：打开时列表区被压窄，工具栏按钮收起为纯图标自适应。
  const artifactOpen = useArtifactStore((s) => s.open)

  // 导航状态
  const [currentFolderId, setCurrentFolderId] = useState<string | null>(null)
  const [isDragging, setIsDragging] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  // 对话框状态
  const [showNewFolder, setShowNewFolder] = useState(false)
  const [renamingFolder, setRenamingFolder] = useState<FolderData | null>(null)
  const [viewingChunks, setViewingChunks] = useState<string | null>(null)
  const [summaryDoc, setSummaryDoc] = useState<MergedFile | null>(null)
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid')

  // 批量选择状态
  const [selectionMode, setSelectionMode] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())

  // 上传状态
  const [uploadingFiles, setUploadingFiles] = useState<UploadingFile[]>([])

  // 文件夹上传状态
  const [folderUploadDialog, setFolderUploadDialog] = useState(false)
  const [folderValidation, setFolderValidation] = useState<{
    supported: { relative_path: string; filename: string; file_type: string }[]
    unsupported: { relative_path: string; filename: string; file_type: string; reason?: string }[]
    folders: string[]
    files: File[]
    paths: string[]
  } | null>(null)
  const [folderUploading, setFolderUploading] = useState(false)

  // 链接转存状态：粘贴网页 / 公众号链接，后端抓取正文转存为文档
  const [urlImportOpen, setUrlImportOpen] = useState(false)
  const [urlImportValue, setUrlImportValue] = useState('')
  const [urlImporting, setUrlImporting] = useState(false)


  // ============================================================
  // 数据查询
  // ============================================================

  // 获取前端配置
  const { data: frontendConfig } = useQuery({
    queryKey: ['frontend-config'],
    queryFn: () => systemApi.getFrontendConfig(),
    staleTime: 60000, // 1 分钟内不重复请求
  })

  // 获取知识库信息
  const { data: kb } = useQuery({
    queryKey: ['knowledge-base', kbId],
    queryFn: () => knowledgeBaseApi.get(kbId!) as Promise<KnowledgeBaseItem>,
    enabled: !!kbId,
  })

  // 当前用户对该库是否有写权限（owner/组织读写/write 共享）。
  // 只读访客（含管理员看他人私有库）隐藏全部写操作入口（上传/新建/删除/重试/拖拽）。
  // 后端 get 接口未返回 can_write 时（加载中）默认按只读处理，避免误显示写入口。
  const canWrite = kb?.can_write === true

  // 知识图谱入口门控（design.md 5.3.1）：全局 graph_enabled 且本 KB config.graph.enabled
  // 才显示「知识图谱」入口。未启用 → 不显示入口（而非显示后报错）。
  const { showEntry: showGraphEntry } = useGraphGating(kbId)

  // 获取当前目录下的文件夹（分页 + 滚动加载）
  const PAGE_SIZE = 20
  const {
    data: foldersData,
    fetchNextPage: fetchNextFolders,
    hasNextPage: hasMoreFolders,
    isFetchingNextPage: isFetchingFolders,
  } = useInfiniteQuery({
    queryKey: ['folders', kbId, currentFolderId],
    queryFn: ({ pageParam }) =>
      folderApi.list(kbId!, currentFolderId, { page: pageParam, page_size: PAGE_SIZE }) as Promise<
        PageResult<FolderData>
      >,
    initialPageParam: 1,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.page + 1 : undefined),
    enabled: !!kbId,
  })
  const folders = foldersData?.pages.flatMap((p) => p.items) ?? []

  // 获取当前目录下的文档（分页 + 滚动加载）
  const {
    data: documentsData,
    isLoading,
    fetchNextPage: fetchNextDocuments,
    hasNextPage: hasMoreDocuments,
    isFetchingNextPage: isFetchingDocuments,
  } = useInfiniteQuery({
    queryKey: ['documents', kbId, currentFolderId],
    queryFn: ({ pageParam }) =>
      documentApi.list(kbId!, currentFolderId, { page: pageParam, page_size: PAGE_SIZE }) as Promise<
        PageResult<DocumentItem>
      >,
    initialPageParam: 1,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.page + 1 : undefined),
    enabled: !!kbId,
    refetchInterval: (query) => {
      const pages = query.state.data?.pages as PageResult<DocumentItem>[] | undefined
      const hasProcessing = pages?.some((p) =>
        p.items.some((d) => d.status === 'processing' || d.status === 'pending')
      )
      return hasProcessing || uploadingFiles.length > 0 ? 2000 : 5000
    },
  })
  const documents = documentsData?.pages.flatMap((p) => p.items) ?? []

  // 滚动加载哨兵：先加载文件夹，文件夹加载完再加载文档
  const loadMore = useCallback(() => {
    if (hasMoreFolders) {
      fetchNextFolders()
    } else if (hasMoreDocuments) {
      fetchNextDocuments()
    }
  }, [hasMoreFolders, hasMoreDocuments, fetchNextFolders, fetchNextDocuments])

  const sentinelRef = useInfiniteScroll(loadMore, {
    hasMore: !!hasMoreFolders || !!hasMoreDocuments,
    loading: isFetchingFolders || isFetchingDocuments,
  })

  // 获取面包屑
  const { data: breadcrumb = [] } = useQuery({
    queryKey: ['breadcrumb', kbId, currentFolderId],
    queryFn: () => folderApi.breadcrumb(kbId!, currentFolderId!) as Promise<BreadcrumbItem[]>,
    enabled: !!kbId && !!currentFolderId,
  })

  // ============================================================
  // 变更操作
  // ============================================================

  // 创建文件夹
  const createFolderMutation = useMutation({
    mutationFn: (name: string) => folderApi.create(kbId!, { name, parent_id: currentFolderId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders', kbId, currentFolderId] })
      setShowNewFolder(false)
      toast('文件夹已创建')
    },
    onError: (err) => {
      toast(`创建失败: ${err instanceof Error ? err.message : '未知错误'}`)
    },
  })

  // 重命名文件夹
  const renameFolderMutation = useMutation({
    mutationFn: ({ folderId, name }: { folderId: string; name: string }) =>
      folderApi.update(folderId, { name }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders', kbId, currentFolderId] })
      setRenamingFolder(null)
      toast('已重命名')
    },
    onError: (err) => {
      toast(`重命名失败: ${err instanceof Error ? err.message : '未知错误'}`)
    },
  })

  // 删除文件夹
  const deleteFolderMutation = useMutation({
    mutationFn: (folderId: string) => folderApi.delete(folderId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders', kbId, currentFolderId] })
      toast('文件夹已删除')
    },
    onError: (err) => {
      toast(`删除失败: ${err instanceof Error ? err.message : '未知错误'}`)
    },
  })

  // 上传文件
  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const uploadMutation = useMutation({
    mutationFn: ({ file, localId }: { file: File; localId: string }) => {
      return documentApi.upload(kbId!, file, currentFolderId).then((res) => ({ res, localId }))
    },
    onSuccess: ({ res, localId }) => {
      if (res?.status === 'duplicate') {
        setUploadingFiles((prev) => prev.filter((f) => f.id !== localId))
        toast(res.error_message || '文件已存在（内容重复）')
      } else {
        // 标记为 uploaded，保留在列表中直到服务端数据确认包含该文件
        setUploadingFiles((prev) =>
          prev.map((f) => (f.id === localId ? { ...f, status: 'uploaded' as const } : f))
        )
      }
      // 防抖刷新：批量上传时多个 onSuccess 只触发一次 refetch
      if (refreshTimerRef.current) {
        clearTimeout(refreshTimerRef.current)
      }
      refreshTimerRef.current = setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ['documents', kbId, currentFolderId] })
        refreshTimerRef.current = null
      }, 800)
    },
    onError: (err, { localId }) => {
      setUploadingFiles((prev) => prev.filter((f) => f.id !== localId))
      toast(`上传失败: ${err instanceof Error ? err.message : '未知错误'}`)
    },
  })

  // 删除文档
  const deleteMutation = useMutation({
    mutationFn: (id: string) => documentApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents', kbId, currentFolderId] })
      toast('文档已删除')
    },
    onError: (err) => {
      toast(`删除失败: ${err instanceof Error ? err.message : '未知错误'}`)
    },
  })

  // 批量删除文档
  const batchDeleteMutation = useMutation({
    mutationFn: (docIds: string[]) => documentApi.batchDelete(docIds),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['documents', kbId, currentFolderId] })
      toast(`已删除 ${data.deleted_count} 个文档`)
      setSelectedIds(new Set())
      setSelectionMode(false)
    },
    onError: (err) => {
      toast(`批量删除失败: ${err instanceof Error ? err.message : '未知错误'}`)
    },
  })

  // ============================================================
  // 统一删除确认交互
  // ============================================================

  // 删除文件夹
  async function handleDeleteFolder(folder: FolderData) {
    const ok = await confirm({
      title: '删除文件夹',
      description: (
        <>
          确定要删除文件夹「{folder.name}」吗？文件夹内的所有文档与子文件夹将被一并删除，此操作不可撤销。
        </>
      ),
    })
    if (ok) deleteFolderMutation.mutate(folder.id)
  }

  // 删除单个文档
  async function handleDeleteDocument(doc: MergedFile) {
    const ok = await confirm({
      title: '删除文档',
      description: <>确定要删除文档「{doc.filename}」吗？相关的向量数据也将被清除，此操作不可撤销。</>,
    })
    if (ok) deleteMutation.mutate(doc.id)
  }

  // 在 Artifact 面板预览文档原件（仅支持可预览类型，如 PDF）
  function handlePreviewDocument(doc: MergedFile) {
    if (doc.isLocal) return
    const fileType = (doc.file_type || doc.filename.split('.').pop() || '').toLowerCase()
    openArtifact({
      id: doc.id,
      filename: doc.filename,
      fileType,
      source: 'document',
    })
  }

  // 批量删除文档
  async function handleBatchDelete() {
    if (selectedIds.size === 0) return
    const ok = await confirm({
      title: '批量删除文档',
      description: (
        <>
          确定要删除选中的 {selectedIds.size} 个文档吗？相关的向量数据也将被清除，此操作不可撤销。
        </>
      ),
    })
    if (ok) batchDeleteMutation.mutate(Array.from(selectedIds))
  }

  const batchRetryMutation = useMutation({
    mutationFn: (docIds: string[]) => documentApi.batchRetry(docIds),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['documents', kbId, currentFolderId] })
      toast(`已重试 ${data.retried_count} 个文档${data.skipped_count > 0 ? `，跳过 ${data.skipped_count} 个` : ''}`)
      setSelectedIds(new Set())
      setSelectionMode(false)
    },
    onError: (err) => {
      toast(`批量重试失败: ${err instanceof Error ? err.message : '未知错误'}`)
    },
  })

  // 重试失败文档
  const retryMutation = useMutation({
    mutationFn: (id: string) => documentApi.retry(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents', kbId, currentFolderId] })
      toast('已重新提交解析')
    },
    onError: (err) => {
      toast(`重试失败: ${err instanceof Error ? err.message : '未知错误'}`)
    },
  })

  // ============================================================
  // 事件处理
  // ============================================================

  // 导航到文件夹
  function navigateToFolder(folderId: string | null) {
    setCurrentFolderId(folderId)
    setSelectedId(null)
  }

  // 处理文件选择（限制并发上传数，避免后端过载）
  function handleFileSelect(files: FileList | null) {
    if (!files) return
    const fileArray = Array.from(files)
    const MAX_CONCURRENT = frontendConfig?.upload_max_concurrent ?? 3
    let index = 0

    function uploadNext() {
      if (index >= fileArray.length) return
      const file = fileArray[index++]
      const localId = `local_${Date.now()}_${Math.random().toString(36).slice(2)}`
      setUploadingFiles((prev) => [
        ...prev,
        { id: localId, filename: file.name, file_size: file.size, status: 'uploading' },
      ])
      uploadMutation.mutate({ file, localId }, { onSettled: uploadNext })
    }

    // 启动最多 MAX_CONCURRENT 个并行上传
    for (let i = 0; i < Math.min(MAX_CONCURRENT, fileArray.length); i++) {
      uploadNext()
    }
  }

  // 处理文件夹选择
  async function handleFolderSelect(files: FileList | null) {
    if (!files || files.length === 0) return

    const fileArray = Array.from(files)
    const paths = fileArray.map((f) => f.webkitRelativePath)

    // 调用校验接口
    try {
      const validation = await documentApi.validateFolder(kbId!, paths)
      setFolderValidation({
        supported: validation.supported_files,
        unsupported: validation.unsupported_files,
        folders: validation.folder_structure,
        files: fileArray,
        paths,
      })
      setFolderUploadDialog(true)
    } catch (err) {
      console.error('文件夹校验失败:', err)
    }
  }

  // 确认文件夹上传
  async function handleFolderUploadConfirm() {
    if (!folderValidation || !kbId) return

    setFolderUploading(true)

    // 只上传支持的文件
    const supportedPaths = new Set(folderValidation.supported.map((f) => f.relative_path))
    const filesToUpload: File[] = []
    const pathsToUpload: string[] = []

    for (let i = 0; i < folderValidation.files.length; i++) {
      const path = folderValidation.paths[i]
      if (supportedPaths.has(path)) {
        filesToUpload.push(folderValidation.files[i])
        pathsToUpload.push(path)
      }
    }

    try {
      const res = await documentApi.uploadFolder(kbId, filesToUpload, pathsToUpload, currentFolderId)
      queryClient.invalidateQueries({ queryKey: ['folders', kbId, currentFolderId] })
      queryClient.invalidateQueries({ queryKey: ['documents', kbId, currentFolderId] })
      // 反馈上传结果：跳过多为内容重复（KB 级去重），提示带首条原因方便定位。
      if (res.skipped_count > 0) {
        const firstSkip = res.results.find((r) => r.status === 'skipped')
        const reason = firstSkip?.message ? `：${firstSkip.message}` : ''
        toast(`已上传 ${res.uploaded_count} 个，跳过 ${res.skipped_count} 个${reason}`)
      } else {
        toast(`已上传 ${res.uploaded_count} 个文件`)
      }
      setFolderUploadDialog(false)
      setFolderValidation(null)
    } catch (err) {
      console.error('文件夹上传失败:', err)
      toast(`文件夹上传失败: ${err instanceof Error ? err.message : '未知错误'}`)
    } finally {
      setFolderUploading(false)
    }
  }

  // 链接转存：抓取网页 / 公众号正文转存为文档
  async function handleUrlImportConfirm() {
    const url = urlImportValue.trim()
    if (!url || !kbId) return
    setUrlImporting(true)
    try {
      const res = await documentApi.fromUrl(kbId, url, currentFolderId) as { status?: string; error_message?: string }
      if (res?.status === 'duplicate') {
        toast(res.error_message || '内容已存在（重复）')
      } else {
        toast('已开始转存，正在抓取并解析…')
      }
      queryClient.invalidateQueries({ queryKey: ['documents', kbId, currentFolderId] })
      setUrlImportOpen(false)
      setUrlImportValue('')
    } catch (err) {
      toast(`转存失败: ${err instanceof Error ? err.message : '未知错误'}`)
    } finally {
      setUrlImporting(false)
    }
  }

  // 拖拽事件（只读库禁用上传）
  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    if (!canWrite) return
    setIsDragging(true)
  }, [canWrite])

  const handleDragLeave = useCallback(() => {
    setIsDragging(false)
  }, [])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setIsDragging(false)
    if (!canWrite) return
    handleFileSelect(e.dataTransfer.files)
  }, [currentFolderId, kbId, canWrite])

  // 选中项
  function handleSelectFolder(id: string) {
    setSelectedId(id)
  }

  function handleSelectFile(id: string) {
    setSelectedId(id)
  }

  // 点击空白取消选中
  function handleBackgroundClick() {
    setSelectedId(null)
    // 不在批量选择模式下才清空
    if (!selectionMode) {
      setSelectedIds(new Set())
    }
  }

  // 批量选择：切换单个文档
  function toggleDocSelection(docId: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(docId)) {
        next.delete(docId)
      } else {
        next.add(docId)
      }
      return next
    })
  }

  // 批量选择：全选/取消全选
  function toggleSelectAll() {
    const serverDocs = documents.filter((d) => d.status !== 'uploading')
    if (selectedIds.size === serverDocs.length) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(serverDocs.map((d) => d.id)))
    }
  }

  // 退出批量选择模式
  function exitSelectionMode() {
    setSelectionMode(false)
    setSelectedIds(new Set())
  }

  // ============================================================
  // 合并列表
  // ============================================================

  // 当服务端数据返回后，清理已标记为 uploaded 且服务端已确认的本地条目
  const serverFilenames = new Set(documents.map((d) => d.filename))
  const idsToRemove = uploadingFiles
    .filter((f) => f.status === 'uploaded' && serverFilenames.has(f.filename))
    .map((f) => f.id)
  if (idsToRemove.length > 0) {
    setTimeout(() => {
      setUploadingFiles((prev) => prev.filter((f) => !idsToRemove.includes(f.id)))
    }, 0)
  }

  // 构建合并列表：服务端文档（已按状态排序）+ 本地条目排在最后
  const confirmedLocalIds = new Set(idsToRemove)
  const allFiles: MergedFile[] = [
    ...documents.map((doc) => ({
      id: doc.id,
      filename: doc.filename,
      file_type: doc.file_type,
      file_size: doc.file_size,
      status: doc.status,
      error_message: doc.error_message,
      chunk_count: doc.chunk_count,
      progress: doc.progress ?? 0,
      progress_message: doc.progress_message ?? null,
      isLocal: false,
      created_at: doc.created_at,
    })),
    ...uploadingFiles
      .filter((f) => !confirmedLocalIds.has(f.id))
      .map((f) => ({
        id: f.id,
        filename: f.filename,
        file_size: f.file_size,
        status: f.status === 'uploaded' ? 'pending' : f.status,
        error_message: null,
        chunk_count: 0,
        progress: 0,
        progress_message: null,
        isLocal: true,
        created_at: null,
      })),
  ]

  const totalItems = folders.length + allFiles.length

  // ============================================================
  // 渲染
  // ============================================================

  return (
    <div
      className="relative h-full flex flex-col"
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      onClick={handleBackgroundClick}
    >
      {/* 拖拽覆盖层 */}
      {isDragging && (
        <div className="fixed inset-0 z-50 bg-primary/5 backdrop-blur-sm flex items-center justify-center">
          <div className="bg-card border-2 border-dashed border-primary rounded-2xl p-12 text-center shadow-2xl">
            <Upload className="h-12 w-12 mx-auto mb-4 text-primary" />
            <p className="text-lg font-medium text-foreground">释放文件以上传</p>
            <p className="text-sm text-muted-foreground mt-1">
              上传到{currentFolderId ? '当前文件夹' : '根目录'}
            </p>
          </div>
        </div>
      )}

      {/* 页面头部 */}
      <div className="flex items-center justify-between gap-3 mb-4 shrink-0">
        <div className="flex items-center gap-3 min-w-0">
          <Link to="/knowledge-bases">
            <button className="h-8 w-8 rounded-lg flex items-center justify-center hover:bg-muted transition-colors cursor-pointer shrink-0">
              <ArrowLeft className="h-4 w-4 text-muted-foreground" />
            </button>
          </Link>
          <div className="min-w-0">
            <h1 className="text-2xl font-bold tracking-tight truncate">{kb?.name || '文档管理'}</h1>
          </div>
        </div>

        {/* 操作按钮（只读库隐藏全部写操作入口）。
            artifact 预览打开时列表区被压窄，按钮收起为纯图标（套 Tooltip 显示中文名）以自适应。 */}
        <TooltipProvider delayDuration={200}>
        <div className="flex items-center gap-2 shrink-0">
          {/* 知识图谱入口：仅全局+KB 双开关均启用时出现（design.md 5.3.1）。
              读权限用户亦可查看图谱，故不受 canWrite 限制。 */}
          {showGraphEntry && (
            <ToolbarTip collapsed={artifactOpen} label="知识图谱">
              <Link to={`/knowledge-bases/${kbId}/graph`} onClick={(e) => e.stopPropagation()}>
                <Button
                  variant="outline"
                  size="sm"
                  className={cn('cursor-pointer', artifactOpen ? 'px-2' : 'gap-1.5')}
                >
                  <Network className="h-4 w-4 shrink-0" />
                  <span className={cn(artifactOpen && 'hidden')}>知识图谱</span>
                </Button>
              </Link>
            </ToolbarTip>
          )}
          {canWrite && (selectionMode ? (
            <>
              <Button
                variant="outline"
                size="sm"
                onClick={(e) => { e.stopPropagation(); toggleSelectAll() }}
                className="gap-1.5 cursor-pointer"
              >
                {selectedIds.size === documents.length && documents.length > 0 ? (
                  <CheckSquare className="h-4 w-4" />
                ) : (
                  <Square className="h-4 w-4" />
                )}
                {selectedIds.size === documents.length && documents.length > 0 ? '取消全选' : '全选'}
              </Button>
              <Button
                variant="default"
                size="sm"
                disabled={selectedIds.size === 0}
                onClick={(e) => { e.stopPropagation(); handleBatchDelete() }}
                className="gap-1.5 cursor-pointer"
              >
                <Trash2 className="h-4 w-4" />
                删除 ({selectedIds.size})
              </Button>
              <Button
                variant="default"
                size="sm"
                disabled={selectedIds.size === 0 || batchRetryMutation.isPending}
                onClick={(e) => { e.stopPropagation(); batchRetryMutation.mutate(Array.from(selectedIds)) }}
                className="gap-1.5 cursor-pointer"
              >
                <RotateCcw className="h-4 w-4" />
                重试 ({selectedIds.size})
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={(e) => { e.stopPropagation(); exitSelectionMode() }}
                className="gap-1.5 cursor-pointer"
              >
                <X className="h-4 w-4" />
                取消
              </Button>
            </>
          ) : (
            <>
              {documents.length > 0 && (
                <ToolbarTip collapsed={artifactOpen} label="批量选择">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={(e) => { e.stopPropagation(); setSelectionMode(true) }}
                    className={cn('cursor-pointer', artifactOpen ? 'px-2' : 'gap-1.5')}
                  >
                    <CheckSquare className="h-4 w-4 shrink-0" />
                    <span className={cn(artifactOpen && 'hidden')}>批量选择</span>
                  </Button>
                </ToolbarTip>
              )}
              <ToolbarTip collapsed={artifactOpen} label="新建文件夹">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={(e) => { e.stopPropagation(); setShowNewFolder(true) }}
                  className={cn('cursor-pointer', artifactOpen ? 'px-2' : 'gap-1.5')}
                >
                  <FolderPlus className="h-4 w-4 shrink-0" />
                  <span className={cn(artifactOpen && 'hidden')}>新建文件夹</span>
                </Button>
              </ToolbarTip>
              <ToolbarTip collapsed={artifactOpen} label="上传文件夹">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={(e) => { e.stopPropagation(); folderInputRef.current?.click() }}
                  className={cn('cursor-pointer', artifactOpen ? 'px-2' : 'gap-1.5')}
                >
                  <FolderUp className="h-4 w-4 shrink-0" />
                  <span className={cn(artifactOpen && 'hidden')}>上传文件夹</span>
                </Button>
              </ToolbarTip>
              <ToolbarTip collapsed={artifactOpen} label="链接转存">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={(e) => { e.stopPropagation(); setUrlImportOpen(true) }}
                  className={cn('cursor-pointer', artifactOpen ? 'px-2' : 'gap-1.5')}
                >
                  <Link2 className="h-4 w-4 shrink-0" />
                  <span className={cn(artifactOpen && 'hidden')}>链接转存</span>
                </Button>
              </ToolbarTip>
              <ToolbarTip collapsed={artifactOpen} label="上传文件">
                <Button
                  size="sm"
                  onClick={(e) => { e.stopPropagation(); fileInputRef.current?.click() }}
                  className={cn('cursor-pointer', artifactOpen ? 'px-2' : 'gap-1.5')}
                >
                  <Upload className="h-4 w-4 shrink-0" />
                  <span className={cn(artifactOpen && 'hidden')}>上传文件</span>
                </Button>
              </ToolbarTip>
            </>
          ))}
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            multiple
            accept=".pdf,.doc,.docx,.xlsx,.pptx,.csv,.txt,.md,.jpg,.jpeg,.png,.mp3,.wav,.m4a,.flac,.ogg"
            onChange={(e) => handleFileSelect(e.target.files)}
          />
          <input
            ref={folderInputRef}
            type="file"
            className="hidden"
            {...({ webkitdirectory: '', directory: '' } as React.InputHTMLAttributes<HTMLInputElement>)}
            onChange={(e) => handleFolderSelect(e.target.files)}
          />
        </div>
        </TooltipProvider>
      </div>

      {/* 容量进度条（chunk 真实度量；接近/已满变色，session-file-upload Req 7） */}
      {kb?.capacity && (
        <div className="mb-4 shrink-0 max-w-2xl">
          <KBCapacityBar capacity={kb.capacity} />
        </div>
      )}

      {/* 面包屑导航 */}
      <div className="flex items-center justify-between mb-4 shrink-0">
        <FolderBreadcrumb items={breadcrumb} onNavigate={navigateToFolder} />
        <div className="flex items-center border border-border rounded-lg p-0.5 shrink-0">
          <button
            onClick={(e) => { e.stopPropagation(); setViewMode('grid') }}
            className={`p-1.5 rounded-md cursor-pointer transition-colors ${viewMode === 'grid' ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground'}`}
          >
            <LayoutGrid className="h-4 w-4" />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); setViewMode('list') }}
            className={`p-1.5 rounded-md cursor-pointer transition-colors ${viewMode === 'list' ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground'}`}
          >
            <List className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* 内容区域 */}
      <div className="flex-1 min-h-0 overflow-auto">
        {isLoading ? (
          viewMode === 'grid' ? (
            <DocumentGridSkeleton count={18} />
          ) : (
            <TableSkeleton rows={6} columns={5} />
          )
        ) : totalItems === 0 ? (
          <div className="flex flex-col items-center justify-center py-20">
            <div className="w-20 h-20 rounded-2xl bg-muted/40 flex items-center justify-center mb-4">
              <FileText className="h-10 w-10 text-muted-foreground/40" />
            </div>
            <p className="text-muted-foreground mb-1">
              {currentFolderId ? '此文件夹为空' : '暂无文档'}
            </p>
            {canWrite ? (
              <>
                <p className="text-sm text-muted-foreground/70 mb-4">拖拽文件到此处或点击上传按钮</p>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    onClick={(e) => { e.stopPropagation(); setShowNewFolder(true) }}
                    className="gap-2 cursor-pointer"
                  >
                    <FolderPlus className="h-4 w-4" />
                    新建文件夹
                  </Button>
                  <Button
                    variant="outline"
                    onClick={(e) => { e.stopPropagation(); fileInputRef.current?.click() }}
                    className="gap-2 cursor-pointer"
                  >
                    <Upload className="h-4 w-4" />
                    选择文件
                  </Button>
                </div>
              </>
            ) : (
              <p className="text-sm text-muted-foreground/70">该知识库暂无可查看的文档</p>
            )}
          </div>
        ) : viewMode === 'grid' ? (
          <div
            className="grid gap-2 p-2 animate-in fade-in-0 duration-500"
            style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(116px, 1fr))' }}
          >
            {/* 文件夹列表 */}
            {folders.map((folder) => (
              <ContextMenu key={folder.id}>
                <ContextMenuTrigger>
                  <FolderItem
                    folder={folder}
                    isSelected={selectedId === folder.id}
                    onSelect={handleSelectFolder}
                    onOpen={navigateToFolder}
                  />
                </ContextMenuTrigger>
                <ContextMenuContent className="w-48">
                  <ContextMenuItem onClick={() => navigateToFolder(folder.id)}>
                    <FolderInput className="h-4 w-4 mr-2" />
                    打开
                  </ContextMenuItem>
                  {canWrite && (
                    <>
                      <ContextMenuItem onClick={() => setRenamingFolder(folder)}>
                        <Pencil className="h-4 w-4 mr-2" />
                        重命名
                      </ContextMenuItem>
                      <ContextMenuSeparator />
                      <ContextMenuItem
                        destructive
                        onClick={() => handleDeleteFolder(folder)}
                      >
                        <Trash2 className="h-4 w-4 mr-2" />
                        删除文件夹
                      </ContextMenuItem>
                    </>
                  )}
                </ContextMenuContent>
              </ContextMenu>
            ))}

            {/* 文件列表 */}
            {allFiles.map((doc) => {
              if (doc.isLocal) {
                return (
                  <div key={doc.id}>
                    <FileItem
                      doc={doc}
                      isSelected={selectedId === doc.id}
                      onSelect={handleSelectFile}
                      onRetry={(id) => retryMutation.mutate(id)}
                    />
                  </div>
                )
              }

              return (
                <ContextMenu key={doc.id}>
                  <ContextMenuTrigger>
                    <DocumentSummaryHover doc={doc}>
                      <div
                        className="relative"
                        onClick={(e) => {
                          if (selectionMode) {
                            e.stopPropagation()
                            toggleDocSelection(doc.id)
                          }
                        }}
                      >
                        {selectionMode && (
                          <div className="absolute -top-1 -left-1 z-10">
                            <div className={`h-5 w-5 rounded border-2 flex items-center justify-center cursor-pointer transition-colors ${
                              selectedIds.has(doc.id)
                                ? 'bg-primary border-primary text-primary-foreground'
                                : 'border-muted-foreground/50 bg-background/80'
                            }`}>
                              {selectedIds.has(doc.id) && (
                                <svg className="h-3 w-3" viewBox="0 0 12 12" fill="none">
                                  <path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                                </svg>
                              )}
                            </div>
                          </div>
                        )}
                        <FileItem
                          doc={doc}
                          isSelected={selectionMode ? selectedIds.has(doc.id) : selectedId === doc.id}
                          onSelect={selectionMode ? toggleDocSelection : handleSelectFile}
                          onRetry={canWrite ? (id) => retryMutation.mutate(id) : undefined}
                        />
                      </div>
                    </DocumentSummaryHover>
                  </ContextMenuTrigger>
                  <ContextMenuContent className="w-48">
                    <ContextMenuItem
                      disabled={!isPreviewable(doc.file_type)}
                      onClick={() => handlePreviewDocument(doc)}
                    >
                      <FileSearch className="h-4 w-4 mr-2" />
                      预览原件
                    </ContextMenuItem>
                    <ContextMenuItem
                      disabled={doc.status !== 'completed'}
                      onClick={() => setViewingChunks(doc.id)}
                    >
                      <Eye className="h-4 w-4 mr-2" />
                      查看切片
                    </ContextMenuItem>
                    <ContextMenuItem onClick={() => setSummaryDoc(doc)}>
                      <NotebookText className="h-4 w-4 mr-2" />
                      摘要预览
                    </ContextMenuItem>
                    <ContextMenuItem
                      onClick={() => {
                        copyToClipboard(doc.filename)
                        toast('已复制文件名')
                      }}
                    >
                      <Copy className="h-4 w-4 mr-2" />
                      复制文件名
                    </ContextMenuItem>
                    {canWrite && (
                      <>
                        <ContextMenuSeparator />
                        {doc.status !== 'processing' && (
                          <ContextMenuItem
                            onClick={() => retryMutation.mutate(doc.id)}
                          >
                            <RotateCcw className="h-4 w-4 mr-2" />
                            重新识别
                          </ContextMenuItem>
                        )}
                        <ContextMenuItem
                          destructive
                          onClick={() => handleDeleteDocument(doc)}
                        >
                          <Trash2 className="h-4 w-4 mr-2" />
                          删除文件
                        </ContextMenuItem>
                      </>
                    )}
                  </ContextMenuContent>
                </ContextMenu>
              )
            })}
          </div>
        ) : (
          /* 列表视图 */
          <div className="border border-border rounded-xl overflow-hidden animate-in fade-in-0 duration-500">
            <table className="w-full text-sm">
              <thead className="bg-muted/80 border-b border-border">
                <tr>
                  {selectionMode && (
                    <th className="w-10 px-3 py-2.5">
                      <div
                        className={`h-4 w-4 rounded border-2 flex items-center justify-center cursor-pointer transition-colors ${
                          selectedIds.size === documents.length && documents.length > 0
                            ? 'bg-primary border-primary text-primary-foreground'
                            : 'border-muted-foreground/50'
                        }`}
                        onClick={(e) => { e.stopPropagation(); toggleSelectAll() }}
                      >
                        {selectedIds.size === documents.length && documents.length > 0 && (
                          <svg className="h-2.5 w-2.5" viewBox="0 0 12 12" fill="none">
                            <path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                          </svg>
                        )}
                      </div>
                    </th>
                  )}
                  <th className="text-left font-medium px-4 py-2.5 text-muted-foreground">名称</th>
                  <th className="text-left font-medium px-4 py-2.5 text-muted-foreground hidden md:table-cell">大小</th>
                  <th className="text-left font-medium px-4 py-2.5 text-muted-foreground hidden lg:table-cell">状态</th>
                  <th className="text-left font-medium px-4 py-2.5 text-muted-foreground hidden lg:table-cell">切片</th>
                  <th className="text-right font-medium px-4 py-2.5 text-muted-foreground">操作</th>
                </tr>
              </thead>
              <tbody>
                {folders.map((folder) => (
                  <tr
                    key={folder.id}
                    className={`border-b border-border/50 last:border-0 transition-colors cursor-pointer ${
                      selectedId === folder.id ? 'bg-primary/5' : 'hover:bg-muted/30'
                    }`}
                    onClick={(e) => { e.stopPropagation(); handleSelectFolder(folder.id) }}
                    onDoubleClick={(e) => { e.stopPropagation(); navigateToFolder(folder.id) }}
                  >
                    {selectionMode && <td className="px-3 py-2.5" />}
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <FolderInput className="h-4 w-4 text-blue-400" />
                        <span className="font-medium">{folder.name}</span>
                      </div>
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground hidden md:table-cell">—</td>
                    <td className="px-4 py-2.5 text-muted-foreground hidden lg:table-cell">—</td>
                    <td className="px-4 py-2.5 text-muted-foreground hidden lg:table-cell">
                      {folder.doc_count + folder.subfolder_count} 项
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center justify-end gap-1">
                        <Button variant="ghost" size="sm" className="h-7 text-xs gap-1 cursor-pointer" onClick={(e) => { e.stopPropagation(); setRenamingFolder(folder) }}>
                          <Pencil className="h-3 w-3" />
                        </Button>
                        <Button variant="ghost" size="sm" className="h-7 text-xs text-destructive hover:text-destructive cursor-pointer" onClick={(e) => { e.stopPropagation(); handleDeleteFolder(folder) }}>
                          <Trash2 className="h-3 w-3" />
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
                {allFiles.map((doc) => (
                  <tr
                    key={doc.id}
                    className={`border-b border-border/50 last:border-0 transition-colors cursor-default ${
                      selectionMode && selectedIds.has(doc.id) ? 'bg-primary/5' :
                      selectedId === doc.id ? 'bg-primary/5' : 'hover:bg-muted/30'
                    }`}
                    onClick={(e) => {
                      e.stopPropagation()
                      if (selectionMode && !doc.isLocal) {
                        toggleDocSelection(doc.id)
                      } else {
                        handleSelectFile(doc.id)
                      }
                    }}
                  >
                    {selectionMode && (
                      <td className="px-3 py-2.5">
                        {!doc.isLocal && (
                          <div
                            className={`h-4 w-4 rounded border-2 flex items-center justify-center cursor-pointer transition-colors ${
                              selectedIds.has(doc.id)
                                ? 'bg-primary border-primary text-primary-foreground'
                                : 'border-muted-foreground/50'
                            }`}
                          >
                            {selectedIds.has(doc.id) && (
                              <svg className="h-2.5 w-2.5" viewBox="0 0 12 12" fill="none">
                                <path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                              </svg>
                            )}
                          </div>
                        )}
                      </td>
                    )}
                    <td className="px-4 py-2.5">
                      {doc.isLocal ? (
                        <div className="flex items-center gap-2">
                          <FileText className="h-4 w-4 text-muted-foreground" />
                          <span className="font-medium truncate max-w-[200px]">{doc.filename}</span>
                        </div>
                      ) : (
                        <DocumentSummaryHover doc={doc}>
                          <div className="flex w-fit items-center gap-2">
                            <FileText className="h-4 w-4 text-muted-foreground" />
                            <span className="font-medium truncate max-w-[200px]">{doc.filename}</span>
                          </div>
                        </DocumentSummaryHover>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground hidden md:table-cell">{formatSize(doc.file_size)}</td>
                    <td className="px-4 py-2.5 hidden lg:table-cell">
                      <span className={`text-xs px-1.5 py-0.5 rounded border ${statusColor(doc.status)}`}>
                        {statusLabel(doc.status)}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground hidden lg:table-cell">
                      {doc.status === 'completed' && doc.chunk_count > 0 ? `${doc.chunk_count} 片` : '—'}
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center justify-end gap-1">
                        {!doc.isLocal && !selectionMode && (
                          <>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 text-xs gap-1 cursor-pointer"
                              disabled={!isPreviewable(doc.file_type)}
                              title="预览原件"
                              onClick={(e) => { e.stopPropagation(); handlePreviewDocument(doc) }}
                            >
                              <FileSearch className="h-3 w-3" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 text-xs gap-1 cursor-pointer"
                              disabled={doc.status !== 'completed'}
                              title="查看切片"
                              onClick={(e) => { e.stopPropagation(); setViewingChunks(doc.id) }}
                            >
                              <Eye className="h-3 w-3" />
                            </Button>
                            {canWrite && (
                              <Button
                                variant="ghost"
                                size="sm"
                                className="h-7 text-xs text-destructive hover:text-destructive cursor-pointer"
                                onClick={(e) => { e.stopPropagation(); handleDeleteDocument(doc) }}
                              >
                                <Trash2 className="h-3 w-3" />
                              </Button>
                            )}
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* 滚动加载哨兵 + 加载状态（网格/列表视图共用） */}
        {!isLoading && totalItems > 0 && (hasMoreFolders || hasMoreDocuments) && (
          <div ref={sentinelRef} className="flex items-center justify-center py-6">
            {(isFetchingFolders || isFetchingDocuments) && (
              <div className="h-6 w-6 border-2 border-primary border-t-transparent rounded-full animate-spin" />
            )}
          </div>
        )}
      </div>

      {/* 链接转存对话框 */}
      <Dialog open={urlImportOpen} onOpenChange={(o) => { if (!urlImporting) setUrlImportOpen(o) }}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>链接转存</DialogTitle>
            <DialogDescription>
              粘贴网页或微信公众号文章链接，系统会抓取正文并转存为知识库文档。
              动态渲染或需登录的页面可能无法提取。
            </DialogDescription>
          </DialogHeader>
          <div className="py-2">
            <Input
              autoFocus
              type="url"
              placeholder="https://mp.weixin.qq.com/s/..."
              value={urlImportValue}
              onChange={(e) => setUrlImportValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !urlImporting && urlImportValue.trim()) {
                  handleUrlImportConfirm()
                }
              }}
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setUrlImportOpen(false)}
              disabled={urlImporting}
            >
              取消
            </Button>
            <Button
              onClick={handleUrlImportConfirm}
              disabled={urlImporting || !urlImportValue.trim()}
              className="gap-1.5"
            >
              {urlImporting ? '抓取中…' : '转存'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 新建文件夹对话框 */}
      <NewFolderDialog
        open={showNewFolder}
        onOpenChange={setShowNewFolder}
        onConfirm={(name) => createFolderMutation.mutate(name)}
        isLoading={createFolderMutation.isPending}
      />

      {/* 重命名对话框 */}
      <RenameDialog
        open={!!renamingFolder}
        onOpenChange={(open) => { if (!open) setRenamingFolder(null) }}
        currentName={renamingFolder?.name || ''}
        onConfirm={(newName) => {
          if (renamingFolder) {
            renameFolderMutation.mutate({ folderId: renamingFolder.id, name: newName })
          }
        }}
        isLoading={renameFolderMutation.isPending}
      />

      {/* 切片查看器 */}
      <ChunkViewer documentId={viewingChunks} onClose={() => setViewingChunks(null)} />

      {/* 摘要预览：与悬浮卡片复用同一只读组件；内容无可聚焦元素，
          阻止打开时自动聚焦到右上角关闭按钮 */}
      <Dialog open={!!summaryDoc} onOpenChange={(open) => { if (!open) setSummaryDoc(null) }}>
        <DialogContent
          className="max-w-md overflow-hidden p-0"
          onOpenAutoFocus={(e) => e.preventDefault()}
        >
          <DialogHeader className="sr-only">
            <DialogTitle>摘要预览</DialogTitle>
            <DialogDescription>查看文档的异步摘要制品</DialogDescription>
          </DialogHeader>
          {summaryDoc && (
            <DocumentSummaryPreview
              docId={summaryDoc.id}
              filename={summaryDoc.filename}
              uploadedAt={summaryDoc.created_at}
            />
          )}
        </DialogContent>
      </Dialog>

      {/* 文件夹上传确认对话框 */}
      <Dialog open={folderUploadDialog} onOpenChange={(open) => { if (!open) { setFolderUploadDialog(false); setFolderValidation(null) } }}>
        <DialogContent className="max-w-lg max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle>上传文件夹</DialogTitle>
            <DialogDescription>
              确认要上传的文件夹内容
            </DialogDescription>
          </DialogHeader>

          {folderValidation && (
            <div className="flex-1 min-h-0 overflow-auto space-y-4 py-2">
              {/* 文件夹结构 */}
              {folderValidation.folders.length > 0 && (
                <div>
                  <p className="text-sm font-medium mb-1.5">
                    将创建 {folderValidation.folders.length} 个文件夹
                  </p>
                  <div className="bg-muted/50 rounded-lg p-3 max-h-28 overflow-auto">
                    {folderValidation.folders.map((f) => (
                      <div key={f} className="text-xs text-muted-foreground flex items-center gap-1.5 py-0.5">
                        <FolderInput className="h-3 w-3 text-blue-400 shrink-0" />
                        {f}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 支持的文件 */}
              <div>
                <p className="text-sm font-medium mb-1.5 text-green-600">
                  ✓ 支持的文件（{folderValidation.supported.length} 个）
                </p>
                {folderValidation.supported.length > 0 && (
                  <div className="bg-green-50 dark:bg-green-950/20 rounded-lg p-3 max-h-36 overflow-auto">
                    {folderValidation.supported.map((f) => (
                      <div key={f.relative_path} className="text-xs text-muted-foreground flex items-center gap-1.5 py-0.5">
                        <FileText className="h-3 w-3 shrink-0" />
                        <span className="truncate">{f.relative_path}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* 不支持的文件 */}
              {folderValidation.unsupported.length > 0 && (
                <div>
                  <p className="text-sm font-medium mb-1.5 text-amber-600">
                    ✗ 不支持的文件（{folderValidation.unsupported.length} 个，将跳过）
                  </p>
                  <div className="bg-amber-50 dark:bg-amber-950/20 rounded-lg p-3 max-h-36 overflow-auto">
                    {folderValidation.unsupported.map((f) => (
                      <div key={f.relative_path} className="text-xs text-muted-foreground flex items-center gap-1.5 py-0.5">
                        <FileText className="h-3 w-3 shrink-0 text-amber-500" />
                        <span className="truncate">{f.relative_path}</span>
                        <span className="text-amber-500 shrink-0">({f.file_type})</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => { setFolderUploadDialog(false); setFolderValidation(null) }}
              disabled={folderUploading}
            >
              取消
            </Button>
            <Button
              onClick={handleFolderUploadConfirm}
              disabled={folderUploading || !folderValidation || folderValidation.supported.length === 0}
              className="gap-1.5"
            >
              {folderUploading ? (
                <>
                  <div className="h-3.5 w-3.5 border-2 border-white border-t-transparent rounded-full animate-spin" />
                  上传中...
                </>
              ) : (
                <>
                  <Upload className="h-4 w-4" />
                  确认上传 {folderValidation?.supported.length || 0} 个文件
                </>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

    </div>
  )
}

export default Documents
