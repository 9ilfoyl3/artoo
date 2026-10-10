// API 客户端：统一请求封装

import { authHeaders, handleUnauthorized } from './auth'

const BASE_URL = '/api'

// 通用请求方法
async function request<T>(
  endpoint: string,
  options: RequestInit = {}
): Promise<T> {
  const url = `${BASE_URL}${endpoint}`
  // 先展开其余选项（method/body 等），headers 放最后合并，避免 options.headers
  // 覆盖掉这里注入的 Content-Type / Authorization；同时把调用方自定义头
  // （如 X-Tenant-ID）经 authHeaders 透传并保留。
  const { headers: extraHeaders, ...rest } = options
  const response = await fetch(url, {
    ...rest,
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(extraHeaders as Record<string, string> | undefined),
    },
  })

  if (!response.ok) {
    // 401：清除登录态并跳转登录页（展示层防御；真正鉴权在后端）
    if (response.status === 401) {
      handleUnauthorized()
    }
    const error = await response.json().catch(() => ({}))
    const detail = error.detail
    // 422 范围校验失败：detail 是数组 [{field, value, allowed_range}, ...]，友好拼接
    if (Array.isArray(detail)) {
      const msg = detail
        .map((d) =>
          d && typeof d === 'object' && 'field' in d
            ? `${d.field}=${d.value} 超出允许范围 ${d.allowed_range}`
            : typeof d === 'string'
              ? d
              : JSON.stringify(d)
        )
        .join('；')
      throw new Error(msg || `请求失败: ${response.status}`)
    }
    throw new Error(typeof detail === 'string' ? detail : `请求失败: ${response.status}`)
  }

  // 204 No Content 无响应体
  if (response.status === 204) {
    return undefined as T
  }

  return response.json()
}

// 通用分页响应结构（与后端 PageResult 对应，用于滚动加载）
export interface PageResult<T> {
  items: T[]
  total: number
  page: number
  page_size: number
  has_more: boolean
}

// 知识库共享入参（user 多选 + 权限）
export interface ShareRequest {
  user_ids: string[]
  permission: string
}

// 知识库列表查询参数（分页 + 关系筛选 + 排序 + 名称搜索）
export interface KnowledgeBaseListParams {
  page?: number
  page_size?: number
  relation?: 'mine' | 'shared' | 'org' | 'others'
  sort?: 'recommended' | 'updated' | 'created' | 'name' | 'docs'
  q?: string
}

// 知识库容量进度条（与后端 KBCapacityVO 对齐，session-file-upload Req 7）
// 真实度量单位是 child chunk；文件数（approx_*_files）是辅助翻译，标"约"。
export interface KBCapacity {
  used_chunks: number
  total_chunks: number
  percent: number
  approx_total_files: number
  approx_used_files: number
  // 约还可上传文档数（向下取整，近似），用户最关心的「还能传多少」
  approx_remaining_files: number
}

// 知识库相关接口
export const knowledgeBaseApi = {
  list: (params?: KnowledgeBaseListParams) => {
    const qs = new URLSearchParams()
    qs.set('page', String(params?.page ?? 1))
    qs.set('page_size', String(params?.page_size ?? 20))
    if (params?.relation) qs.set('relation', params.relation)
    if (params?.sort) qs.set('sort', params.sort)
    if (params?.q && params.q.trim()) qs.set('q', params.q.trim())
    return request<PageResult<unknown>>(`/knowledge-bases?${qs.toString()}`)
  },
  get: (id: string) => request<unknown>(`/knowledge-bases/${id}`),
  create: (data: unknown) =>
    request<unknown>('/knowledge-bases', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: unknown) =>
    request<unknown>(`/knowledge-bases/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/knowledge-bases/${id}`, { method: 'DELETE' }),
  // 共享给指定用户（owner/admin 可调用）；user_ids 批量、permission=read|write
  share: (kbId: string, data: { user_ids: string[]; permission: string }) =>
    request<unknown>(`/knowledge-bases/${kbId}/share`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  // 撤销某个用户的共享授权
  revokeShare: (kbId: string, userId: string) =>
    request<void>(`/knowledge-bases/${kbId}/share/user/${userId}`, { method: 'DELETE' }),
  // 变更可见性（private | organization）；owner 可调用。
  // organization 时可选 orgPermission（read|write）控制组织成员是否可写内容。
  setVisibility: (kbId: string, visibility: string, orgPermission?: string) =>
    request<unknown>(`/knowledge-bases/${kbId}/visibility`, {
      method: 'PUT',
      body: JSON.stringify({ visibility, org_permission: orgPermission ?? null }),
    }),
  // 查看某库已共享用户（仅 owner）
  shares: (kbId: string) =>
    request<{ user_id: string; username: string; avatar: string | null; permission: string }[]>(
      `/knowledge-bases/${kbId}/shares`
    ),
}

// 跨租户知识库分享链接（cross-tenant-kb-share）
export interface ShareLinkInfo {
  kb_name: string
  owner_username: string | null
  owner_avatar: string | null
  owner_tenant_name: string | null
  permission: string
  valid: boolean
  can_accept: boolean
  reason: string | null
}

export interface ShareLinkItem {
  id: string
  token: string | null
  kb_id: string
  permission: string
  max_uses: number | null
  used_count: number
  expires_at: string
  is_active: boolean
  created_at: string
}

export const kbShareLinkApi = {
  // 为本人拥有的库签发跨租户只读分享链接
  create: (data: { kb_id: string; expires_in_hours: number; max_uses?: number | null }) =>
    request<{ id: string; token: string; kb_id: string; permission: string; expires_at: string; max_uses: number | null }>(
      '/kb-share-links',
      { method: 'POST', body: JSON.stringify(data) }
    ),
  // 列出某库的分享链接（仅 owner）
  list: (kbId: string) =>
    request<ShareLinkItem[]>(`/kb-share-links?kb_id=${encodeURIComponent(kbId)}`),
  // 吊销分享链接（仅 owner）
  revoke: (linkId: string) =>
    request<void>(`/kb-share-links/${linkId}`, { method: 'DELETE' }),
  // 领取页信息（需登录）
  info: (token: string) =>
    request<ShareLinkInfo>(`/kb-share-links/${encodeURIComponent(token)}/info`),
  // 领取分享（需登录）
  accept: (token: string) =>
    request<{ detail: string; kb_id: string; permission: string }>(
      `/kb-share-links/${encodeURIComponent(token)}/accept`,
      { method: 'POST' }
    ),
}

// 文档抽取出的事件（事件中心图谱，对应后端 DocumentEventResponse）。
export interface DocumentEvent {
  id: string
  title: string
  summary: string
  content: string
  chunk_id: string
  /** 关联实体规范名列表 */
  entity_names: string[]
}

// 文档相关接口
export const documentApi = {
  list: (
    kbId: string,
    folderId?: string | null,
    params?: { page?: number; page_size?: number }
  ) => {
    const qs = new URLSearchParams()
    if (folderId) qs.set('folder_id', folderId)
    qs.set('page', String(params?.page ?? 1))
    qs.set('page_size', String(params?.page_size ?? 20))
    return request<PageResult<unknown>>(`/knowledge-bases/${kbId}/documents?${qs.toString()}`)
  },
  upload: (kbId: string, file: File, folderId?: string | null) => {
    const formData = new FormData()
    formData.append('file', file)
    const url = folderId
      ? `${BASE_URL}/knowledge-bases/${kbId}/documents/upload?folder_id=${folderId}`
      : `${BASE_URL}/knowledge-bases/${kbId}/documents/upload`
    return fetch(url, {
      method: 'POST',
      headers: authHeaders(),
      body: formData,
    }).then((res) => {
      if (res.status === 401) handleUnauthorized()
      return res.json()
    })
  },
  // 链接转存：粘贴网页 / 公众号链接，后端抓取正文提取后转存为 .md 文档入库。
  // 复用 request()：自动处理 401 与 422（detail 为后端友好中文，透传到 toast）。
  fromUrl: (kbId: string, url: string, folderId?: string | null) =>
    request<unknown>(`/knowledge-bases/${kbId}/documents/from-url`, {
      method: 'POST',
      body: JSON.stringify({ url, folder_id: folderId ?? null }),
    }),
  validateFolder: (kbId: string, paths: string[]) =>
    request<{
      supported_files: { relative_path: string; filename: string; file_type: string; supported: boolean; reason?: string }[]
      unsupported_files: { relative_path: string; filename: string; file_type: string; supported: boolean; reason?: string }[]
      folder_structure: string[]
    }>(`/knowledge-bases/${kbId}/documents/validate-folder`, {
      method: 'POST',
      body: JSON.stringify({ paths }),
    }),
  uploadFolder: (kbId: string, files: File[], paths: string[], parentFolderId?: string | null) => {
    const formData = new FormData()
    files.forEach((file) => formData.append('files', file))
    formData.append('paths', JSON.stringify(paths))
    if (parentFolderId) {
      formData.append('parent_folder_id', parentFolderId)
    }
    return fetch(`${BASE_URL}/knowledge-bases/${kbId}/documents/upload-folder`, {
      method: 'POST',
      headers: authHeaders(),
      body: formData,
    }).then(async (res) => {
      if (res.status === 401) handleUnauthorized()
      if (!res.ok) {
        const error = await res.json().catch(() => ({}))
        throw new Error(error.detail || `请求失败: ${res.status}`)
      }
      return res.json()
    }) as Promise<{
      total_files: number
      uploaded_count: number
      skipped_count: number
      created_folders: string[]
      results: { relative_path: string; filename: string; doc_id?: string; folder_id?: string; status: string; message?: string }[]
    }>
  },
  get: (id: string) => request<unknown>(`/documents/${id}`),
  delete: (id: string) =>
    request<void>(`/documents/${id}`, { method: 'DELETE' }),
  batchDelete: (docIds: string[]) =>
    request<{ deleted_count: number; total_requested: number }>('/documents/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ doc_ids: docIds }),
    }),
  batchRetry: (docIds: string[]) =>
    request<{ retried_count: number; skipped_count: number; total_requested: number }>('/documents/batch-retry', {
      method: 'POST',
      body: JSON.stringify({ doc_ids: docIds }),
    }),
  retry: (id: string) =>
    request<unknown>(`/documents/${id}/retry`, { method: 'POST' }),
  chunks: (id: string, params?: { page?: number; page_size?: number }) =>
    request<PageResult<unknown>>(
      `/documents/${id}/chunks?page=${params?.page ?? 1}&page_size=${params?.page_size ?? 20}`
    ),
  // 文档抽取出的事件列表（事件中心图谱，title/summary + 关联实体）。
  // 图谱未启用 / 无事件时后端返回 []，前端按空态处理。
  events: (id: string) =>
    request<DocumentEvent[]>(`/documents/${id}/events`),
  // 拉取文档缩略图：preview 接口需 Authorization 头，原生 <img> 无法携带，
  // 故用 fetch 带 token 取回 blob 并生成本地 objectURL 供 <img src> 使用。
  // 调用方负责在不再使用时 URL.revokeObjectURL 释放。
  preview: async (id: string): Promise<string> => {
    const response = await fetch(`${BASE_URL}/documents/${id}/preview`, {
      headers: authHeaders(),
    })
    if (!response.ok) {
      if (response.status === 401) handleUnauthorized()
      throw new Error(`加载缩略图失败: ${response.status}`)
    }
    const blob = await response.blob()
    return URL.createObjectURL(blob)
  },
  // 拉取文档原件（用于原件在线预览/下载）：同样需 Authorization 头，故用 fetch 取
  // blob 生成 objectURL。调用方负责在不再使用时 URL.revokeObjectURL 释放。
  rawFile: async (id: string): Promise<string> => {
    const response = await fetch(`${BASE_URL}/documents/${id}/raw`, {
      headers: authHeaders(),
    })
    if (!response.ok) {
      if (response.status === 401) handleUnauthorized()
      throw new Error(`加载原件失败: ${response.status}`)
    }
    const blob = await response.blob()
    return URL.createObjectURL(blob)
  },
}

// 文件夹相关接口
export const folderApi = {
  list: (
    kbId: string,
    parentId?: string | null,
    params?: { page?: number; page_size?: number }
  ) => {
    const qs = new URLSearchParams()
    if (parentId) qs.set('parent_id', parentId)
    qs.set('page', String(params?.page ?? 1))
    qs.set('page_size', String(params?.page_size ?? 20))
    return request<PageResult<unknown>>(`/knowledge-bases/${kbId}/folders?${qs.toString()}`)
  },
  create: (kbId: string, data: { name: string; parent_id?: string | null }) =>
    request<unknown>(`/knowledge-bases/${kbId}/folders`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (folderId: string, data: { name?: string; parent_id?: string | null }) =>
    request<unknown>(`/folders/${folderId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (folderId: string) =>
    request<void>(`/folders/${folderId}`, { method: 'DELETE' }),
  breadcrumb: (kbId: string, folderId: string) =>
    request<{ id: string | null; name: string }[]>(`/knowledge-bases/${kbId}/folders/${folderId}/breadcrumb`),
  move: (kbId: string, data: { item_ids: string[]; item_type: string; target_folder_id: string | null }) =>
    request<unknown>(`/knowledge-bases/${kbId}/move`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
}

// 检索测试接口（纯检索，不经过 LLM 生成）
export interface RetrievalResultItem {
  chunk_id: string
  doc_id: string
  filename: string
  content: string
  child_content: string
  score: number
  rrf_score: number | null
  rerank_score: number | null
  routes: string[]
  metadata?: Record<string, unknown>
}

export interface RetrievalTrace {
  routes: { name: string; recalled: number; enabled: boolean }[]
  funnel: { stage: string; count: number }[]
}

export interface RetrievalTestResponse {
  query: string
  mode: string
  total: number
  elapsed_ms: number
  results: RetrievalResultItem[]
  trace: RetrievalTrace | null
}

export const retrievalApi = {
  test: (data: { query: string; knowledge_base_id: string; mode?: string; top_k?: number }) =>
    request<RetrievalTestResponse>('/retrieval/test', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
}

// API Key 相关接口
// capability-config-to-platform：API Key 为平台能力出口（外部系统凭 Key + 自身用户标识
// 在 External 租户内维护并查询自己的知识库），仅超级管理员签发/撤销。创建走代理 Key 端点
// （external_agent，require_platform）。
export const apiKeyApi = {
  list: () => request<{ items: unknown[]; total: number }>('/api-keys').then(res => res.items),
  create: (data: { name?: string }) =>
    request<unknown>('/api-keys/external-agent', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/api-keys/${id}`, { method: 'DELETE' }),
}

// 系统配置接口
//
// 租户级配置（/system/config 及 reset）支持可选 tenantId：
// - 普通租户管理员：不传 tenantId，后端据 JWT 身份定位自身租户；
// - 超级管理员：必须传 tenantId（经租户管理列表进入），注入 X-Tenant-ID 指定目标租户，
//   否则后端返回 400。
// 平台配置（/system/platform-config）为超管专属，承载 Load_Cache_TTL。
const tenantHeader = (tenantId?: string): RequestInit =>
  tenantId ? { headers: { 'X-Tenant-ID': tenantId } } : {}

// 平台级配置（超管）：当前承载向量集合加载缓存 TTL + 单库/单会话 chunk 硬上限。
// 注：全部知识库共用一个 Milvus collection（kb_id 作为 Partition Key），故加载缓存是全局粒度。
export interface PlatformConfig {
  load_cache_ttl: number
  kb_chunk_cap: number
}

// 单库 chunk 上限的内存推荐值（仅 GET 返回；信息性建议，不自动写入，Req 5.1）
export interface MemoryRecommendation {
  detected_memory_gb: number
  recommended_kb_chunk_cap: number
  safety_factor: number
  active_kbs_assumption: number
  assumption: string
}

// 平台配置 GET/PUT 响应：附带 memory_recommendation（仅 GET 填充）与 changes（仅 PUT 填充）
export interface PlatformConfigResponse extends PlatformConfig {
  memory_recommendation?: MemoryRecommendation | null
  changes?: { field: string; old: unknown; new: unknown }[]
}

export const systemApi = {
  health: () => request<unknown>('/system/health'),
  getConfig: (tenantId?: string) =>
    request<unknown>('/system/config', tenantHeader(tenantId)),
  updateConfig: (data: unknown, tenantId?: string) =>
    request<unknown>('/system/config', {
      method: 'PUT',
      body: JSON.stringify(data),
      ...tenantHeader(tenantId),
    }),
  // 恢复检索参数默认值（后端：POST /api/system/config/retrieval/reset）
  resetRetrievalConfig: (tenantId?: string) =>
    request<unknown>('/system/config/retrieval/reset', {
      method: 'POST',
      ...tenantHeader(tenantId),
    }),
  // 平台配置（超管专属）：向量集合加载缓存 TTL + 单库 chunk 上限 + 会话 chunk 天花板
  getPlatformConfig: () => request<PlatformConfigResponse>('/system/platform-config'),
  // 仅提交本次改动的字段（后端 model_dump(exclude_unset=True, exclude_none=True)）
  updatePlatformConfig: (data: Partial<PlatformConfig>) =>
    request<PlatformConfigResponse>('/system/platform-config', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  getFrontendConfig: () =>
    request<{ upload_max_concurrent: number; upload_max_file_size_mb: number; graph_enabled: boolean }>('/system/frontend-config'),
}

// LLM 模型配置接口
export const llmConfigApi = {
  list: (chatVisible?: boolean) =>
    request<unknown[]>(chatVisible !== undefined ? `/llm-configs?chat_visible=${chatVisible}` : '/llm-configs'),
  create: (data: { name: string; provider: string; vendor?: string; base_url: string; model: string; api_key?: string; is_default?: boolean; stream_enabled?: boolean; thinking_control?: string; max_context_tokens?: number; max_output_tokens?: number; chat_visible?: boolean }) =>
    request<unknown>('/llm-configs', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: Record<string, unknown>) =>
    request<unknown>(`/llm-configs/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/llm-configs/${id}`, { method: 'DELETE' }),
  test: (id: string) =>
    request<{ success: boolean; message: string; reply?: string }>(`/llm-configs/${id}/test`, { method: 'POST' }),
  testConnection: (data: { provider: string; vendor?: string; base_url: string; model: string; api_key?: string; thinking_control?: string; config_id?: string }) =>
    request<{ success: boolean; message: string; reply?: string }>('/llm-configs/test', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
}

// Embedding/Rerank 配置接口类型
export interface EmbedConfigItem {
  id: string
  name: string
  config_type: string  // embedding | rerank
  provider: string  // remote
  vendor: string | null
  model_name: string
  base_url: string | null
  api_key_set: boolean
  timeout: number
  sparse_enabled: boolean
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface EmbedTestResult {
  success: boolean
  message: string
}

export interface EmbedCurrentConfig {
  embed_model: string
  embed_base_url: string
  embed_sparse_enabled: boolean
  rerank_model: string
  rerank_base_url: string
  /** 该项是否回落自环境变量（数据库无启用配置时为 true） */
  embed_from_env?: boolean
  rerank_from_env?: boolean
}

// Embedding/Rerank 配置接口
export const embedConfigApi = {
  list: (configType?: string) =>
    request<EmbedConfigItem[]>(configType ? `/embed-configs?config_type=${configType}` : '/embed-configs'),
  current: () => request<EmbedCurrentConfig>('/embed-configs/current'),
  create: (data: {
    name: string
    config_type: string
    vendor?: string
    model_name?: string
    base_url: string
    api_key?: string
    timeout?: number
    sparse_enabled?: boolean
    is_active?: boolean
  }) =>
    request<EmbedConfigItem>('/embed-configs', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: Record<string, unknown>) =>
    request<EmbedConfigItem>(`/embed-configs/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/embed-configs/${id}`, { method: 'DELETE' }),
  test: (data: {
    config_type: string
    model_name?: string
    base_url: string
    api_key?: string
    timeout?: number
    config_id?: string
    sparse_enabled?: boolean
  }) =>
    request<EmbedTestResult>('/embed-configs/test', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  testSaved: (id: string) =>
    request<EmbedTestResult>(`/embed-configs/${id}/test`, { method: 'POST' }),
}

export interface OCRConfigItem {
  id: string
  name: string
  provider_type: string
  /** provider_type 是否在后端 Provider 注册表内，false 表示该配置已失效需重建 */
  provider_type_valid: boolean
  api_url: string
  api_key_set: boolean
  timeout: number
  is_default: boolean
  is_fallback: boolean
  extra_config: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

/** OCR 服务类型元数据（能力与展示信息由后端注册表派生，前端不硬编码） */
export interface OCRProviderTypeMeta {
  provider_type: string
  label: string
  summary: string
  api_url_example: string
  accepts: string[]
  accepts_pdf: boolean
  outputs_markdown: boolean
  recommended_timeout: number
  extra_config_keys: Record<string, string>
}

/** 单种输入形态（image / pdf）的真实链路验证结果 */
export interface OCRTestCheck {
  input_kind: string
  ok: boolean
  elapsed_ms: number | null
  text_preview: string | null
  error: string | null
}

export interface OCRTestResult {
  success: boolean
  message: string
  elapsed_ms: number | null
  checks?: OCRTestCheck[]
}

// OCR 服务配置接口
export const ocrConfigApi = {
  list: () => request<OCRConfigItem[]>('/ocr-configs'),
  providerTypes: () => request<OCRProviderTypeMeta[]>('/ocr-configs/provider-types'),
  create: (data: { name: string; provider_type: string; api_url: string; api_key?: string; timeout?: number; is_default?: boolean; is_fallback?: boolean; extra_config?: Record<string, unknown> }) =>
    request<OCRConfigItem>('/ocr-configs', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: Record<string, unknown>) =>
    request<OCRConfigItem>(`/ocr-configs/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/ocr-configs/${id}`, { method: 'DELETE' }),
  test: (data: { provider_type: string; api_url: string; api_key?: string; timeout?: number; extra_config?: Record<string, unknown> }) =>
    request<OCRTestResult>('/ocr-configs/test', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  testSaved: (id: string) =>
    request<OCRTestResult>(`/ocr-configs/${id}/test`, { method: 'POST' }),
}

/** MCP 传输模式：auto=先试标准协议再回落私有 REST */
export type MCPTransport = 'auto' | 'streamable_http' | 'legacy_rest'
/** MCP 静态凭据方式：Artoo 向远端证明"我是 Artoo" */
export type MCPAuthType = 'none' | 'bearer' | 'header'

export interface MCPConfigItem {
  id: string
  name: string
  url: string
  enabled: boolean
  transport: MCPTransport
  auth_type: MCPAuthType
  auth_header_name: string | null
  /** 是否已配置凭据（明文永不回显，仅回掩码） */
  has_auth_token: boolean
  auth_token_masked: string | null
  /** 是否向该 server 透传调用方上下文（会话 / 租户 / 主体） */
  forward_context: boolean
  tool_prefix: string | null
  created_at: string
  updated_at: string
}

/** 远端 MCP server 暴露的单个工具元信息 */
export interface MCPToolMeta {
  name: string
  description: string
}

export interface MCPTestResult {
  reachable: boolean
  tool_count: number
  tools: MCPToolMeta[]
  /** 本次实际走通的协议，用于判断对方是否已升级到标准 MCP */
  protocol: MCPTransport | null
  error: string | null
}

/** 创建/更新 MCP 配置的请求体。auth_token 三态：不传=保持，''=清除，非空=替换 */
export interface MCPConfigPayload {
  name: string
  url: string
  enabled?: boolean
  transport?: MCPTransport
  auth_type?: MCPAuthType
  auth_token?: string
  auth_header_name?: string | null
  forward_context?: boolean
  tool_prefix?: string | null
}

// MCP Server 配置接口（平台底座，仅超管）
export const mcpConfigApi = {
  list: () => request<MCPConfigItem[]>('/mcp-configs'),
  create: (data: MCPConfigPayload) =>
    request<MCPConfigItem>('/mcp-configs', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: Partial<MCPConfigPayload>) =>
    request<MCPConfigItem>(`/mcp-configs/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/mcp-configs/${id}`, { method: 'DELETE' }),
  test: (data: { url: string; transport?: MCPTransport; auth_type?: MCPAuthType; auth_token?: string; auth_header_name?: string | null }) =>
    request<MCPTestResult>('/mcp-configs/test', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  testSaved: (id: string) =>
    request<MCPTestResult>(`/mcp-configs/${id}/test`, { method: 'POST' }),
  tools: (id: string) =>
    request<MCPToolMeta[]>(`/mcp-configs/${id}/tools`),
}

export interface ASRConfigItem {
  id: string
  name: string
  provider_type: string
  vendor: string | null
  api_url: string
  api_key_set: boolean
  model_name: string
  language: string | null
  timeout: number
  is_default: boolean
  is_fallback: boolean
  extra_config: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export interface ASRTestResult {
  success: boolean
  message: string
  elapsed_ms: number | null
}

// ASR（语音识别）服务配置接口
export const asrConfigApi = {
  list: () => request<ASRConfigItem[]>('/asr-configs'),
  create: (data: { name: string; provider_type: string; vendor?: string; api_url: string; api_key?: string; model_name: string; language?: string; timeout?: number; is_default?: boolean; is_fallback?: boolean; extra_config?: Record<string, unknown> }) =>
    request<ASRConfigItem>('/asr-configs', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: Record<string, unknown>) =>
    request<ASRConfigItem>(`/asr-configs/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/asr-configs/${id}`, { method: 'DELETE' }),
  test: (data: { provider_type: string; api_url: string; api_key?: string; timeout?: number }) =>
    request<ASRTestResult>('/asr-configs/test', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  testSaved: (id: string) =>
    request<ASRTestResult>(`/asr-configs/${id}/test`, { method: 'POST' }),
}



// Agent 预设配置接口
export interface AgentPresetItem {
  id: string
  name: string
  description: string | null
  config_json: {
    agent_mode?: 'agent' | 'hybrid'
    max_iterations?: number
    temperature?: number
    thinking_enabled?: boolean
    allowed_tools?: string[]
    custom_instructions?: string
  } | null
  is_default: boolean
  created_at: string
  updated_at: string
  // 归属与可见性（agent-preset-sharing）
  is_shared: boolean
  is_builtin: boolean
  is_owner: boolean
  owner_user_id: string | null
  owner_username: string | null
}

// 可配进预设 allowed_tools 的工具选项。内置工具 + 平台已启用 MCP server 发现的外部工具。
// 后端只回工具名/描述/来源 server 名，不含 url 与凭据。
export interface AgentToolOption {
  name: string
  label: string
  description: string
  source: 'builtin' | 'mcp'
  // 基础设施工具：恒注册，取消勾选也关不掉，前端显示为「始终启用」
  always_on: boolean
  server_name: string | null
}

export interface AgentAvailableTools {
  tools: AgentToolOption[]
  // 非空表示外部工具发现失败（远端不可达等），此时 tools 仅含内置工具
  mcp_error: string | null
}

export const agentPresetApi = {
  list: () => request<AgentPresetItem[]>('/agent-presets'),
  availableTools: () => request<AgentAvailableTools>('/agent-presets/available-tools'),
  setDefault: (id: string) =>
    request<AgentPresetItem>(`/agent-presets/${id}/default`, {
      method: 'PUT',
    }),
  rewritePrompt: (data: { instruction: string; current_prompt?: string }) =>
    request<{ prompt: string }>('/agent-presets/rewrite-prompt', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  create: (data: { name: string; description?: string; config_json?: Record<string, unknown>; is_default?: boolean; is_shared?: boolean }) =>
    request<unknown>('/agent-presets', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: Record<string, unknown>) =>
    request<unknown>(`/agent-presets/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/agent-presets/${id}`, { method: 'DELETE' }),
}

// 自定义技能（Agent Skills）接口。每个用户维护自己的技能（per-user），
// 对话时与平台预置技能合并，Agent 按需通过 read_skill 加载。
export interface CustomSkillItem {
  id: string
  name: string
  description: string
  instructions: string
  enabled: boolean
  created_at: string
  updated_at: string
}

export const skillsApi = {
  list: () => request<CustomSkillItem[]>('/skills'),
  generate: (data: { instruction: string }) =>
    request<{ name: string; description: string; instructions: string }>('/skills/generate', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  create: (data: { name: string; description: string; instructions: string; enabled?: boolean }) =>
    request<CustomSkillItem>('/skills', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: { name?: string; description?: string; instructions?: string; enabled?: boolean }) =>
    request<CustomSkillItem>(`/skills/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/skills/${id}`, { method: 'DELETE' }),
}

// 会话相关接口类型
export interface SessionItem {
  id: string
  title: string
  kb_id: string | null
  model_config_id: string | null
  message_count: number
  created_at: string
  updated_at: string
}

export interface SessionMessageItem {
  id: string
  role: string
  content: string
  references: unknown[] | null
  agent_steps: {
    type: string
    content: boolean
    tool_call_id: string
    tool_name: string
    arguments?: Record<string, unknown>
    success: any
    duration_ms: number | undefined
    step: string
    detail: string
    max_context_tokens?: number
    current_context_tokens?: number
  }[] | null
  attachments: MessageAttachment[] | null
  kb_id: string | null
  kb_ids: string[] | null
  feedback?: 'like' | 'dislike' | null
  created_at: string
}

/** 用户消息携带的会话文件附件（发送时绑定的已上传文件快照）。 */
export interface MessageAttachment {
  file_id: string
  filename: string
  file_size?: number | null
  file_type?: string | null
}

// 会话级文件上传（session-file-upload Task 8 / Design C8）
//
// 后端入口 /api/sessions/{session_id}/files：
// - POST  multipart(file)  → 秒回 + 后台异步建索引，返回 202 + SessionFileResponse(status=queued)
// - GET                    → 列出本会话已上传文件（含最新 status/progress/error_message，轮询/对账兜底）
// - DELETE /{file_id}      → 移除单文件并释放配额（204）
// - WS   /events           → 实时推送建索引状态事件（见 useSessionUploadEvents）
//
// 鉴权：仅会话所有者本人；非 owner / 非存在 → 后端统一 404（存在性非泄露）。
// 限制超额：413（FileTooLargeError / UploadCapExceeded），
// detail 是后端已格式化的友好中文，由 request()/uploadFile() 透传到 toast。
export interface SessionFileResponse {
  id: string
  session_id: string
  filename: string
  file_type: string | null
  file_size: number | null
  chunk_count: number
  /** queued | processing | completed | failed —— 异步路径上传后初始为 queued */
  status: string
  /** 建索引进度 0-100（异步路径由 worker 各阶段更新） */
  progress: number
  /** 当前阶段人类可读描述（可空） */
  progress_message: string | null
  /** 失败原因（status=failed 时有值） */
  error_message: string | null
  created_at: string
}

// multipart 上传专用：复用 request() 的错误解析（含 413/422 友好中文 detail）
// 支持传入 AbortSignal，便于上传中途取消（abort 时 fetch 抛 AbortError）。
async function uploadFile<T>(
  endpoint: string,
  formData: FormData,
  signal?: AbortSignal,
): Promise<T> {
  const url = `${BASE_URL}${endpoint}`
  const response = await fetch(url, {
    method: 'POST',
    headers: authHeaders(),
    body: formData,
    signal,
  })
  if (!response.ok) {
    if (response.status === 401) handleUnauthorized()
    const error = await response.json().catch(() => ({}))
    const detail = error.detail
    if (Array.isArray(detail)) {
      const msg = detail
        .map((d) =>
          d && typeof d === 'object' && 'field' in d
            ? `${d.field}=${d.value} 超出允许范围 ${d.allowed_range}`
            : typeof d === 'string'
              ? d
              : JSON.stringify(d)
        )
        .join('；')
      throw new Error(msg || `请求失败: ${response.status}`)
    }
    throw new Error(typeof detail === 'string' ? detail : `请求失败: ${response.status}`)
  }
  if (response.status === 204) return undefined as T
  return response.json()
}

export const sessionFileApi = {
  list: (sessionId: string) =>
    request<SessionFileResponse[]>(`/sessions/${sessionId}/files`),
  upload: (sessionId: string, file: File, signal?: AbortSignal) => {
    const fd = new FormData()
    fd.append('file', file)
    return uploadFile<SessionFileResponse>(`/sessions/${sessionId}/files`, fd, signal)
  },
  remove: (sessionId: string, fileId: string) =>
    request<void>(`/sessions/${sessionId}/files/${fileId}`, { method: 'DELETE' }),
  // 拉取会话附件原件（原件在线预览/下载）：需 Authorization 头，取 blob 生成 objectURL。
  // 调用方负责在不再使用时 URL.revokeObjectURL 释放。
  rawFile: async (sessionId: string, fileId: string): Promise<string> => {
    const response = await fetch(`${BASE_URL}/sessions/${sessionId}/files/${fileId}/raw`, {
      headers: authHeaders(),
    })
    if (!response.ok) {
      if (response.status === 401) handleUnauthorized()
      throw new Error(`加载原件失败: ${response.status}`)
    }
    const blob = await response.blob()
    return URL.createObjectURL(blob)
  },
}

// 会话管理接口
// 用问题文本截断生成会话占位标题（≤30 字符，与后端 _truncate_title 对齐）。
export function truncateSessionTitle(query: string): string {
  return query.slice(0, 30) + (query.length > 30 ? '...' : '')
}

export const sessionApi = {
  list: () => request<SessionItem[]>('/sessions'),
  create: (data: { title?: string; kb_id?: string; model_config_id?: string }) =>
    request<SessionItem>('/sessions', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: { title?: string }) =>
    request<SessionItem>(`/sessions/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/sessions/${id}`, { method: 'DELETE' }),
  getMessages: (id: string) =>
    request<SessionMessageItem[]>(`/sessions/${id}/messages`),
  clearMessages: (id: string) =>
    request<void>(`/sessions/${id}/messages`, { method: 'DELETE' }),
  setMessageFeedback: (sessionId: string, messageId: string, feedback: 'like' | 'dislike' | null) =>
    request<{ detail: string; feedback: string | null }>(
      `/sessions/${sessionId}/messages/${messageId}/feedback`,
      { method: 'PUT', body: JSON.stringify({ feedback }) },
    ),
  retryLastRound: (sessionId: string) =>
    request<{ content: string; attachments: MessageAttachment[] | null; kb_id: string | null; kb_ids: string[] | null }>(
      `/sessions/${sessionId}/messages/retry`,
      { method: 'POST' },
    ),
}


// ============================================================
// 认证相关接口（tenant-auth）
// ============================================================

export interface LoginResponse {
  access_token: string
  token_type: string
  must_change_password: boolean
  is_super_admin: boolean
}

// 当前登录者的身份摘要（替代旧的 /auth/me/permissions）
export interface MeResponse {
  user_id: string
  tenant_id: string | null
  is_super_admin: boolean
  role: 'admin' | 'member' | null
}

export interface MeProfile {
  user_id: string
  username: string
  tenant_id: string | null
  tenant_name: string | null
  is_super_admin: boolean
  role: string | null
  role_label: string
  description: string | null
  avatar: string | null
}

export const authApi = {
  login: (username: string, password: string) =>
    request<LoginResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  changePassword: (oldPassword: string, newPassword: string) =>
    request<{ detail: string }>('/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),
  me: () => request<MeResponse>('/auth/me'),
  // 同租户可选用户（用户名模糊搜索，多选用）：任意登录成员可调
  selectableUsers: (q?: string) =>
    request<{ id: string; username: string; avatar: string | null }[]>(
      `/auth/users/selectable${q ? `?q=${encodeURIComponent(q)}` : ''}`
    ),
  // 当前登录者资料（左下角展示 + 个人资料页）
  myProfile: () => request<MeProfile>('/auth/me/profile'),
  updateMyProfile: (data: { description?: string | null; avatar?: string | null }) =>
    request<MeProfile>('/auth/me/profile', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  // 是否开放租户自助注册（公开端点，决定登录页是否显示"注册"入口）
  registrationMode: () => request<{ self_serve: boolean }>('/auth/registration-mode'),
  // 租户自助注册：开一个独立租户，注册人即该租户管理员
  register: (username: string, password: string, tenantName: string) =>
    request<LoginResponse & { tenant_id: string }>('/auth/register', {
      method: 'POST',
      body: JSON.stringify({ username, password, tenant_name: tenantName }),
    }),
}


// ============================================================
// 管理接口（tenant-auth）：平台级租户管理 + 租户级用户/角色管理
// ============================================================

export interface TenantItem {
  id: string
  name: string
  tenant_type: string
  is_active: boolean
  description: string | null
  avatar: string | null
}

export interface TenantCreateResult extends TenantItem {
  admin_username: string
  admin_temp_password: string | null
}

export interface AdminUserItem {
  id: string
  tenant_id: string | null
  username: string
  is_active: boolean
  must_change_password: boolean
  role: string | null
  temp_password: string | null
  description: string | null
  avatar: string | null
}

export interface AdminUserCreateResult extends AdminUserItem {}

export interface AuditLogItem {
  id: string
  actor_user_id: string | null
  actor_username: string | null
  actor_tenant_id: string | null
  actor_is_super_admin: boolean
  actor_role: string | null
  action: string
  target_type: string | null
  target_id: string | null
  target_name: string | null
  detail: Record<string, unknown> | null
  result: string
  ip: string | null
  created_at: string
}

export interface InvitationItem {
  id: string
  token: string | null
  scope: string
  tenant_id: string | null
  max_uses: number | null
  used_count: number
  expires_at: string
  is_active: boolean
  created_by_username: string | null
  created_at: string
}

export interface InvitationCreateResult {
  id: string
  token: string
  scope: string
  tenant_id: string | null
  expires_at: string
  max_uses: number | null
}

export const adminApi = {
  // —— 租户（Super_Admin / tenant:manage）——
  listTenants: () => request<TenantItem[]>('/admin/tenants'),
  createTenant: (name: string, adminUsername: string, adminPassword?: string, description?: string | null, avatar?: string | null) =>
    request<TenantCreateResult>('/admin/tenants', {
      method: 'POST',
      body: JSON.stringify({ name, admin_username: adminUsername, admin_password: adminPassword ?? null, description: description ?? null, avatar: avatar ?? null }),
    }),
  setTenantStatus: (tenantId: string, isActive: boolean) =>
    request<TenantItem>(`/admin/tenants/${tenantId}/status`, {
      method: 'PUT',
      body: JSON.stringify({ is_active: isActive }),
    }),
  updateTenantProfile: (tenantId: string, data: { name?: string; description?: string | null; avatar?: string | null }) =>
    request<TenantItem>(`/admin/tenants/${tenantId}/profile`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  listTenantUsers: (tenantId: string) =>
    request<AdminUserItem[]>(`/admin/tenants/${tenantId}/users`),
  createTenantAdmin: (tenantId: string, username: string, password?: string) =>
    request<AdminUserCreateResult>(`/admin/tenants/${tenantId}/admins`, {
      method: 'POST',
      body: JSON.stringify({ username, password: password ?? null }),
    }),

  // —— 用户（user:manage）——
  listUsers: (params?: { page?: number; page_size?: number; q?: string }) => {
    const qs = new URLSearchParams()
    qs.set('page', String(params?.page ?? 1))
    qs.set('page_size', String(params?.page_size ?? 20))
    if (params?.q) qs.set('q', params.q)
    return request<PageResult<AdminUserItem>>(`/admin/users?${qs.toString()}`)
  },
  createUser: (username: string, password?: string, description?: string | null, avatar?: string | null) =>
    request<AdminUserCreateResult>('/admin/users', {
      method: 'POST',
      body: JSON.stringify({ username, password: password ?? null, description: description ?? null, avatar: avatar ?? null }),
    }),
  setUserStatus: (userId: string, isActive: boolean) =>
    request<AdminUserItem>(`/admin/users/${userId}/status`, {
      method: 'PUT',
      body: JSON.stringify({ is_active: isActive }),
    }),
  resetPassword: (userId: string) =>
    request<AdminUserCreateResult>(`/admin/users/${userId}/reset-password`, { method: 'POST' }),
  transferKnowledgeBases: (userId: string, targetUserId: string) =>
    request<{ detail: string; transferred_count: number }>(`/admin/users/${userId}/transfer-knowledge-bases`, {
      method: 'POST',
      body: JSON.stringify({ target_user_id: targetUserId }),
    }),

  // —— 审计日志（user:manage 可读；租管限本租户，超管全局）——
  auditLogs: (params?: { page?: number; page_size?: number; action?: string; actor?: string }) => {
    const qs = new URLSearchParams()
    qs.set('page', String(params?.page ?? 1))
    qs.set('page_size', String(params?.page_size ?? 20))
    if (params?.action) qs.set('action', params.action)
    if (params?.actor) qs.set('actor', params.actor)
    return request<PageResult<AuditLogItem>>(`/admin/audit-logs?${qs.toString()}`)
  },

  // —— 邀请链接 ——
  listInvitations: (params?: { page?: number; page_size?: number }) => {
    const qs = new URLSearchParams()
    qs.set('page', String(params?.page ?? 1))
    qs.set('page_size', String(params?.page_size ?? 20))
    return request<PageResult<InvitationItem>>(`/admin/invitations?${qs.toString()}`)
  },
  createInvitation: (data: { scope: string; expires_in_hours: number; max_uses?: number | null }) =>
    request<InvitationCreateResult>('/admin/invitations', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  revokeInvitation: (id: string) =>
    request<void>(`/admin/invitations/${id}`, { method: 'DELETE' }),
  // 通过某邀请链接创建的用户（按时间倒序）
  invitationUsers: (id: string) =>
    request<{ id: string; username: string; tenant_id: string | null; is_active: boolean; created_at: string }[]>(
      `/admin/invitations/${id}/users`
    ),
}

// 免登录邀请接受（无 token 注入；接受页用）
export const inviteApi = {
  info: (token: string) =>
    request<{ scope: string; tenant_name: string | null; valid: boolean }>(`/invitations/${token}`),
  accept: (token: string, data: { username: string; password: string; tenant_name?: string; description?: string | null; avatar?: string | null }) =>
    request<{ detail: string; tenant_id?: string; user_id?: string }>(`/invitations/${token}/accept`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
}

// ============================================================
// 知识图谱接口（knowledge-graph，design.md 5.1 / 5.3）
// ============================================================
//
// 数据流：组件 → graphStore action → 这里的 graphApi → 后端 Graph API。
// 组件不直接调用这些接口，统一经 store 触发，保证数据流单向清晰（design.md 5.3.3）。
//
// 降级语义：后端 store 不可用时返回 503，request() 会抛出带 detail 的 Error
// （"知识图谱未启用或不可用"），由 store 捕获后置为不可用态（design.md 5.3.4）。

/** 力导向图节点（对应后端 nodes[]，design.md 5.1）。 */
export interface GraphNode {
  id: string
  name: string
  type: string
  /** 度数（入边+出边），用于映射节点大小 */
  degree: number
  /** 节点大类：entity（实体，默认）| event（事件）。用于可视化区分两层节点。 */
  node_type?: 'entity' | 'event'
}

/** 力导向图边（对应后端 edges[]）。source/target 为实体 id。 */
export interface GraphEdge {
  source: string
  target: string
  type: string
  weight: number
}

/** 子图元信息（对应后端 meta）。center/depth 仅 ego 模式下返回。 */
export interface GraphMeta {
  mode: 'overview' | 'ego'
  total: number
  returned: number
  /** 是否因上限被截断（用于「已显示 X / 共 Y」提示） */
  truncated: boolean
  center?: string
  depth?: number
}

/** overview / ego 子图响应（GET /graph）。 */
export interface GraphSubset {
  nodes: GraphNode[]
  edges: GraphEdge[]
  meta: GraphMeta
}

/** 图谱统计（GET /graph/stats）。types 为「类型 → 数量」分布。 */
export interface GraphStats {
  entity_count: number
  relation_count: number
  types: Record<string, number>
  orphan_count: number
  status: string
}

/** 实体邻居摘要（实体详情的 neighbors[]）。 */
export interface GraphNeighbor {
  id: string
  name: string
  type: string
  /** 与中心实体的关系类型 */
  rel_type: string
}

/** 实体关联原文 chunk 预览（实体详情的 chunks[]）。 */
export interface GraphEntityChunk {
  chunk_id: string
  doc_id: string
  content_preview: string
}

/** 实体详情（GET /graph/entity/{id}，懒加载，design.md 5.3.3）。 */
export interface GraphEntityDetail {
  id: string
  name: string
  type: string
  aliases: string[]
  /** 属性描述（LLM 抽取，后端为字符串列表） */
  attributes: string[]
  degree: number
  neighbors: GraphNeighbor[]
  chunks: GraphEntityChunk[]
}

/** 事件详情里关联（MENTIONS）的实体（可点击 pivot）。 */
export interface GraphEventMention {
  id: string
  name: string
  type: string
}

/** 事件详情（GET /graph/event/{id}，事件中心图谱，懒加载）。 */
export interface GraphEventDetail {
  id: string
  title: string
  summary: string
  content: string
  doc_id: string
  /** 关联实体列表（可点击 pivot） */
  mentions: GraphEventMention[]
  /** 来源 chunk 原文预览（可为 null） */
  chunk: GraphEntityChunk | null
}

/** KB 级图谱配置（config.graph，design.md 3.3 / 5.2）。 */
export interface GraphConfig {
  enabled: boolean
  entity_types: string[]
  relation_types: string[]
  extract_granularity: string
  extract_model_id: string | null
  enable_alias_dedup: boolean
  alias_sim_threshold: number
}

/** getGraph 查询参数（design.md 5.1）。 */
export interface GraphQueryParams {
  mode?: 'overview' | 'ego'
  /** ego 中心节点（entity_id 或 name），ego 模式必填 */
  center?: string
  /** ego BFS 跳数（后端 clamp 到平台硬上限） */
  depth?: number
  /** 类型过滤（前端传数组，这里拼为逗号分隔串） */
  types?: string[]
  /** 节点数上限（0/省略=用平台默认上限，后端 clamp） */
  limit?: number
  /** 是否把事件作为一类节点并入返回（node_type 区分，默认 false） */
  include_events?: boolean
}

// config.graph 缺省值（KB 详情未显式配置 graph 时的兜底，与后端默认对齐）。
const DEFAULT_GRAPH_CONFIG: GraphConfig = {
  enabled: false,
  entity_types: [],
  relation_types: [],
  extract_granularity: 'parent',
  extract_model_id: null,
  enable_alias_dedup: true,
  alias_sim_threshold: 0.92,
}

export const graphApi = {
  // overview / ego 子图：参数被后端按平台上限 clamp。
  getGraph: (kbId: string, params?: GraphQueryParams) => {
    const qs = new URLSearchParams()
    qs.set('mode', params?.mode ?? 'overview')
    if (params?.center) qs.set('center', params.center)
    if (params?.depth != null) qs.set('depth', String(params.depth))
    if (params?.types && params.types.length > 0) qs.set('types', params.types.join(','))
    if (params?.limit != null) qs.set('limit', String(params.limit))
    if (params?.include_events) qs.set('include_events', 'true')
    return request<GraphSubset>(`/kb/${kbId}/graph?${qs.toString()}`)
  },
  // 图谱统计：实体/关系数、类型分布、孤立数、状态。
  getGraphStats: (kbId: string) =>
    request<GraphStats>(`/kb/${kbId}/graph/stats`),
  // 实体详情（懒加载）：属性/别名/邻居/关联原文 chunk 预览。
  getGraphEntity: (kbId: string, entityId: string) =>
    request<GraphEntityDetail>(`/kb/${kbId}/graph/entity/${entityId}`),
  // 事件详情（懒加载）：标题/摘要/完整内容/关联实体/来源原文预览（事件中心图谱）。
  getGraphEvent: (kbId: string, eventId: string) =>
    request<GraphEventDetail>(`/kb/${kbId}/graph/event/${eventId}`),
  // KB 图谱配置：后端尚无独立 GET config 端点（task 5.2 的 PUT 之外），
  // 故从 KB 详情的 config.graph 派生（逐字段兜底，缺失回退安全默认）。
  getGraphConfig: async (kbId: string): Promise<GraphConfig> => {
    const kb = await request<{ config?: { graph?: Partial<GraphConfig> } | null }>(
      `/knowledge-bases/${kbId}`
    )
    const graph = kb?.config?.graph ?? {}
    return { ...DEFAULT_GRAPH_CONFIG, ...graph }
  },
  // 更新 KB 图谱配置（部分更新，仅传字段被合并）。返回生效后的 config.graph。
  updateGraphConfig: (kbId: string, body: Partial<GraphConfig>) =>
    request<GraphConfig>(`/kb/${kbId}/graph/config`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
}
