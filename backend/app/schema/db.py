"""数据库 ORM 模型定义"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Float, Integer, String, Text, JSON, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """ORM 基类"""
    pass


class TenantScopedMixin:
    """受租户隔离的模型标记 Mixin（tenant-auth）。

    继承本 Mixin 的模型都带有 `tenant_id` 列，并被仓储层方案 B
    （`with_loader_criteria` + contextvar 三态）识别为需自动注入 tenant 过滤的目标。
    Mixin 只声明列，不承载行为；真正的过滤注入在 `app/repositories/tenant_repo.py`。
    """

    # 受隔离资源的归属租户。建表即存在（新分支、create_all 一步到位，不做历史回填）。
    tenant_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)


class KnowledgeBase(Base, TenantScopedMixin):
    """知识库表"""
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    doc_count: Mapped[int] = mapped_column(Integer, default=0)
    # tenant-auth：归属与可见性
    owner_user_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)  # KB_Owner
    visibility: Mapped[str] = mapped_column(String, default="private", nullable=False)  # private | organization
    # 组织公共库开放维度：read（默认，组织成员仅可读）| write（组织成员可写内容）。
    # 仅 visibility=organization 时有效；private 忽略（不参与判定）。
    org_permission: Mapped[str] = mapped_column(String, default="read", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # 关联
    folders: Mapped[list["Folder"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")


class Folder(Base, TenantScopedMixin):
    """文件夹表（支持嵌套目录）"""
    __tablename__ = "folders"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kb_id: Mapped[str] = mapped_column(String, ForeignKey("knowledge_bases.id"), nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("folders.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # 关联
    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="folders")
    children: Mapped[list["Folder"]] = relationship(back_populates="parent", cascade="all, delete-orphan")
    parent: Mapped[Optional["Folder"]] = relationship(back_populates="children", remote_side=[id])
    documents: Mapped[list["Document"]] = relationship(back_populates="folder", cascade="all, delete-orphan")


class Document(Base, TenantScopedMixin):
    """文档表"""
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kb_id: Mapped[str] = mapped_column(String, ForeignKey("knowledge_bases.id"), nullable=False)
    folder_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("folders.id"), nullable=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    file_type: Mapped[str] = mapped_column(String, nullable=False)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    file_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # SHA256 文件内容哈希
    # 链接转存来源：经网页链接抓取正文入库时记录原文 URL（普通上传为 None），便于溯源展示。
    source_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100 处理进度
    progress_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # 阶段描述
    # 知识图谱抽取状态（knowledge-graph）：none|pending|processing|completed|failed
    graph_status: Mapped[str] = mapped_column(String, default="none", nullable=False)
    # 权威 attempt 计数，重解析时 +1，用于隔离陈旧的在途抽取子任务
    graph_attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # 关联
    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="documents")
    folder: Mapped[Optional["Folder"]] = relationship(back_populates="documents")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class Chunk(Base, TenantScopedMixin):
    """Chunk 元数据表（向量存 Milvus，元数据存 SQLite）"""
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    doc_id: Mapped[str] = mapped_column(String, ForeignKey("documents.id"), nullable=False)
    kb_id: Mapped[str] = mapped_column(String, ForeignKey("knowledge_bases.id"), nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    chunk_metadata: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # 关联
    document: Mapped["Document"] = relationship(back_populates="chunks")
    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="chunks")


class ApiKey(Base, TenantScopedMixin):
    """API Key 表"""
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    key_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    call_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # tenant-auth：三模型 Key
    # tenant_level（默认，机器凭据）| user_level（绑定用户）| external_agent（超管级代理）
    key_type: Mapped[str] = mapped_column(String, default="tenant_level", nullable=False)
    bound_user_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)  # 用户级 Key 绑定的 user
    authorized_scope: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # 租户级 Key 授权范围
    key_source: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # 代理 Key 的来源标识（命名空间前缀）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LLMConfig(Base):
    """LLM 模型配置表"""
    __tablename__ = "llm_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)  # ollama | vllm
    # 模型厂商（vendor）：openai/aliyun/zhipu/volcengine/deepseek/siliconflow/generic 等。
    # 显式存储后传给 VllmLLM，使 thinking 方言分派不再依赖 base_url 猜测。空表示未指定。
    vendor: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    base_url: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    api_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    chat_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    stream_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 思考模式参数格式：none/chat_template_kwargs/enable_thinking/thinking_type。
    # 显式指定 thinking 开关写入请求体的字段格式，取代脆弱的厂商/模型名自动匹配。
    # 空表示未指定 → 运行时回退到自动方言分派（零回归）。
    # 注：是否开启思考由智能体预设独占控制，本字段仅决定「开启时怎么写入 API」。
    thinking_control: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    max_context_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OCRConfig(Base):
    """OCR 服务配置表"""
    __tablename__ = "ocr_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # vl | paddle | mineru：取值域由 app.pipeline.ocr.registry 的 Provider 注册表派生。
    # 不在注册表内的存量取值运行时被 OCRManager 跳过，配置接口标记为无效待重建。
    provider_type: Mapped[str] = mapped_column(String, nullable=False)
    api_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    api_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    timeout: Mapped[float] = mapped_column(Float, default=30.0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    extra_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ASRConfig(Base):
    """ASR（语音识别）服务配置表

    属平台底座（capability-config-to-platform），全平台一份，无 tenant_id，
    仅超级管理员维护。所有厂商统一走 OpenAI 兼容的 /v1/audio/transcriptions 接口。
    """
    __tablename__ = "asr_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_type: Mapped[str] = mapped_column(String, nullable=False, default="openai")  # openai（兼容接口）
    # 服务商（vendor）：仅用于 UI 记忆「选了哪家服务商」以自动回填 base_url；
    # 运行时统一走 OpenAI 兼容 /v1/audio/transcriptions，不影响后端多厂商兼容。
    vendor: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    api_url: Mapped[str] = mapped_column(String(2048), nullable=False)  # base_url，如 http://host:port/v1
    api_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)  # whisper-1 等
    language: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # 语言提示（可选）
    timeout: Mapped[float] = mapped_column(Float, default=300.0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    extra_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class MCPConfig(Base):
    """MCP Server 配置表（Artoo 作为 MCP client 要连的远端服务）

    属平台底座（capability-config-to-platform），全平台一份，无 tenant_id，
    仅超级管理员维护。Agent 运行时按此表发现远端 MCP server 的工具注入
    （default-off，须预设 allowed_tools 显式白名单才注册）。
    """
    __tablename__ = "mcp_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 远端 MCP server 的 base URL（如 http://host:port）。标准协议下打 {url}/mcp。
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 传输/协议模式：auto（先试标准 MCP，失败回落私有 REST）| streamable_http（只用标准）
    # | legacy_rest（只用私有 REST，对接尚未升级的老服务端）。
    transport: Mapped[str] = mapped_column(String(20), default="auto", nullable=False)
    # 静态凭据：Artoo 向远端证明"我是 Artoo"。none | bearer（Authorization: Bearer）
    # | header（自定义头，头名见 auth_header_name）。
    auth_type: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    # 凭据密文（app.auth.secret_box 加密，密钥从 jwt_secret 派生）。**不存明文**：
    # 这张表里的 token 是第三方系统的凭据，DB 泄露不应等于第三方系统被攻破。
    auth_token_encrypted: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    auth_header_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # 是否向该 server 透传调用方上下文（session/tenant/subject）。**默认关闭**：
    # 把 A 方终端用户标识发给不相关的第三方属跨方数据泄露，须逐个显式开启。
    forward_context: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 工具名前缀（可选）。多个 server 声明同名工具时用它区分；留空保持原始工具名。
    tool_prefix: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class EmbedConfig(Base):
    """Embedding/Rerank 远程服务配置表"""
    __tablename__ = "embed_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    config_type: Mapped[str] = mapped_column(String(20), nullable=False)  # embedding | rerank
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="remote")  # 统一为 remote
    # 模型厂商（vendor）：仅用于 UI 记忆「选了哪家服务商」以自动回填 base_url；
    # 运行时仍走 RemoteEmbedder/RemoteReranker 的 URL 格式自动检测，不影响多后端兼容。
    vendor: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # 远程服务字段
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)  # BAAI/bge-m3 等
    base_url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    api_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    timeout: Mapped[float] = mapped_column(Float, default=60.0)
    # sparse 向量支持（仅 embedding 类型有效）
    sparse_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 状态
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)  # 当前是否启用
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RetrievalConfigRow(Base):
    """检索参数租户级配置表（每租户一行，主键 = tenant_id）。

    承载分块档 + 五档检索参数共 17 个参数，取代硬编码在 hybrid.py / milvus.py 的
    检索参数与散落在 Settings 的分块参数。租户级（每租户一行），对该租户名下所有
    知识库生效。

    不继承 TenantScopedMixin：本表读写已由 RetrievalConfigStore 显式按主键
    tenant_id 定位（Store 按 tenant_id 分键缓存，绕过 get_settings 的 @lru_cache，
    支持即时热生效），主键即租户键，无需再叠加方案 B 的 loader criteria 兜底过滤
    （否则会干扰"超管为指定租户读配置"——platform 态不注入过滤反而正确）。
    ORM 类名用 ...Row 后缀，与 app.retrieval.config.RetrievalConfig（Pydantic）消歧。

    所有参数列 nullable=True：缺失语义由 RetrievalConfig.effective_from_raw 在读时
    逐字段兜底为 Safe_Default（Req 2.2）。建表经现有 init_db 的
    Base.metadata.create_all 自动创建，无需迁移脚本。
    """
    __tablename__ = "retrieval_configs"

    # 租户级：以 tenant_id 为主键，每租户一行；Store 用 session.get(..., tenant_id) 定位
    tenant_id: Mapped[str] = mapped_column(String, primary_key=True)

    # 分块档 Chunk_Tier（本期纳入，默认对齐 Settings：2500/450/70，读时由 effective_from_raw 兜底）
    parent_chunk_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    child_chunk_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    chunk_overlap: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 召回档 Recall_Tier
    recall_k: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rerank_candidate_k: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 融合档 Fusion_Tier
    rrf_k: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    composite_rerank_weight: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    composite_base_weight: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    composite_source_weight: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # 精排档 Rerank_Tier
    rerank_threshold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rerank_top_k: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    threshold_degradation_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # 去重档 Dedup_Tier
    mmr_lambda: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mmr_threshold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # 索引档 Index_Tier
    hnsw_ef: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hnsw_ef_construction: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hnsw_m: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 上传限制档（租户级；仅 upload_max_file_mb 仍生效，其余已废弃但 DB 列保留向后兼容）
    upload_max_file_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PlatformConfigRow(Base):
    """平台级全局配置表（单行，固定主键 'global'）。本期承载 Load_Cache_TTL。

    跨租户共享、仅超管可改。读写经 PlatformConfigStore（绕过 get_settings 的
    @lru_cache，支持即时热生效）。缺失由 PlatformConfig.effective_from_raw 读时兜底
    （load_cache_ttl 默认 30 秒）。建表经 create_all 自动创建，无迁移脚本。
    """
    __tablename__ = "platform_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default="global")
    load_cache_ttl: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # collection 加载缓存有效期（秒）
    # 上传限制平台级（超管可配；仅 kb_chunk_cap 生效，session_chunk_ceiling 已废弃）
    kb_chunk_cap: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 单库/单会话 child chunk 硬上限
    # 知识图谱抗压参数（knowledge-graph，design.md 3.4）：缺失由 PlatformConfig.effective_from_raw 读时兜底
    graph_overview_max_nodes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # overview 节点上限
    graph_ego_max_nodes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # ego 节点上限
    graph_ego_max_depth: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # ego BFS 最大跳数硬上限
    graph_retriever_hops: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 检索融合邻居跳数
    graph_retriever_max_chunks: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 图谱召回每查询最大 chunk 数
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AgentPreset(Base):
    """Agent 预设配置表

    归属与可见性（agent-preset-sharing）：
    - tenant_id：所属租户。内置预设为 None（平台内置、跨租户可见）。
    - owner_user_id：创建者（acting_subject_id）。内置预设为 None（无归属）。
    - is_shared：是否开放给本租户全体成员可见可用。私有（默认 False）仅创建者可见。
      内置预设恒 True（全租户可见）。
    管理权（改/删）仅创建者本人；内置预设任何人不可改删。

    不继承 TenantScopedMixin：可见性为「内置(tenant_id IS NULL) ∪ 本租户自有 ∪
    本租户已开放」三段并集，含 tenant_id IS NULL 分支，方案 B 的 loader criteria
    （仅注入 tenant_id == 当前租户）会误过滤内置预设，故改由 API 显式过滤
    （与 RetrievalConfigRow 同理）。
    """
    __tablename__ = "agent_presets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    config_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 平台唯一默认；仅超级管理员可切换。删除默认项时回落到内置智能推理。
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # 所属租户；内置预设为 None（跨租户可见）
    tenant_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    # 创建者（acting_subject_id）；内置预设为 None（平台内置、无归属）
    owner_user_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    # 是否开放给本租户全体成员可见可用；私有（False）仅创建者可见。内置预设恒 True。
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CustomSkill(Base, TenantScopedMixin):
    """用户自定义技能表（Agent Skills 的 Progressive Disclosure 扩展）。

    归属与可见性（对齐 ChatSession 的 per-user 模式）：
    - tenant_id：所属租户（方案 B loader criteria 自动注入兜底过滤）。
    - owner_user_id：创建者（acting_subject_id）。技能是个人资产，仅本人可见可改可用，
      故在 tenant_id 之外再按 owner 收敛——否则同租户用户会互相看到对方的技能。
    管理权（改/删/启停）仅创建者本人。预置技能（preloaded/*/SKILL.md）走文件、全局只读，
    不入此表，与自定义技能在 SkillManager 层合并。

    name 为技能标识（read_skill 工具按 name 加载）。同一 owner 下 name 唯一由 API 层保证
    （避免与方案 B 的可空 tenant 过滤在 DB 唯一约束上纠缠）。建表经 init_db 的
    create_all 自动创建，无需迁移脚本。
    """
    __tablename__ = "custom_skills"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ChatSession(Base, TenantScopedMixin):
    """对话会话表"""
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # tenant-auth：会话归属用户（per-user 隔离）。取 acting_subject_id——
    # 注册用户/用户级 Key=user_id，外部用户=external_user_id；机器级 tenant_level Key 为 None。
    # 会话/消息是个人对话历史，须仅本人可见：tenant_id 之外再按 owner 收敛，
    # 否则同租户用户之间会互相看到对方的对话历史。
    owner_user_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    kb_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    model_config_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # 关联
    messages: Mapped[list["ChatMessageRecord"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="ChatMessageRecord.created_at"
    )


class ChatMessageRecord(Base, TenantScopedMixin):
    """对话消息记录表"""
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    references: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # 检索引用来源
    agent_steps: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # Agent 思考步骤
    # 用户消息携带的会话文件附件元数据（发送时绑定的已上传文件快照）。
    # 仅 user 消息可能非空；每项形如 {file_id, filename, file_size, file_type}。
    # 用于历史回放时在对应用户气泡上方渲染附件 chip（session-file-upload 附件归属）。
    attachments: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    kb_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # 本条消息使用的主知识库 ID
    kb_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # 多知识库联合检索时的知识库 ID 列表
    # 用户对 AI 回答的反馈：'like' | 'dislike' | None（仅 assistant 消息可能非空）。
    # 先落库保留，后续用于 agent 优化/质量评估。
    feedback: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # 关联
    session: Mapped["ChatSession"] = relationship(back_populates="messages")


# ============================================================
# session-file-upload：会话级文件上传新增模型
# 会话上传文件的向量存共享 Milvus collection（kb_session_files，按 session_id 标量隔离），
# 文件元数据与 chunk 文本存以下两张独立关系表（不复用受 FK 约束的 Chunk 表，因 Chunk.kb_id /
# Chunk.doc_id 均 NOT NULL + 外键，无法承载会话文件）。两表为全新表，经 init_db 的
# create_all 自动建表（与 tenant-auth / kb-retrieval-optimization 新表同款做法，无需迁移脚本）。
# ============================================================


class SessionFile(Base, TenantScopedMixin):
    """会话级上传文件元数据。

    与 ChatSession 绑定（session_id FK + ondelete CASCADE），删会话时 DB 自动删本表行；
    共享 collection 中的向量由 SessionUploadService.cleanup_session_files 按 session_id 显式删除
    （DB 级联管不到 Milvus）。`id` 复用为该文件在 Milvus 中向量的 doc_id，使
    delete_by_doc_id 能精准移除单文件向量（Req 1.8）。`chunk_count` 供会话累计 chunk 配额
    聚合（Req 6.4），移除文件即释放（Req 6.7）。继承 TenantScopedMixin（提供 tenant_id 列 +
    方案 B 租户兜底过滤），与既有受隔离模型一致。
    """
    __tablename__ = "session_files"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # 文件 ID（= 用作 Milvus doc_id）
    session_id: Mapped[str] = mapped_column(
        String, ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    owner_user_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    file_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 该文件 child chunk 数（会话累计配额聚合用）；异步路径初始为 0，建索引完成后回填
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 异步路径默认 queued；同步兜底路径可直接写 completed
    status: Mapped[str] = mapped_column(String, default="queued")  # queued | processing | completed | failed
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0-100 建索引进度
    progress_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # 当前阶段人类可读描述
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 失败原因
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SessionChunk(Base, TenantScopedMixin):
    """会话文件的 chunk 文本（父/子块）。

    与 SessionFile 绑定（file_id FK + ondelete CASCADE），删会话（级联删 SessionFile）或删单文件
    时自动清理本表行。不复用正式 `Chunk` 表（其 kb_id / doc_id 均 NOT NULL + 外键，无法承载会话
    文件）。`id` = Milvus 中该 chunk 的 chunk_id；父块扩展时按 parent_id 从本表取内容（与正式库
    走 Chunk 表的父块扩展对称，但查独立表）。无指向 documents / knowledge_bases 的外键，规避
    Chunk 表的 FK 约束问题。
    """
    __tablename__ = "session_chunks"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # = Milvus chunk_id
    file_id: Mapped[str] = mapped_column(
        String, ForeignKey("session_files.id", ondelete="CASCADE"), index=True, nullable=False
    )
    session_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ============================================================
# tenant-auth：租户认证与隔离体系新增模型
# 新分支、库可重建：直接由 create_all 建表，不做历史数据迁移/回填。
# ============================================================


class Tenant(Base):
    """租户表：绝对隔离边界（一个组织 / 一个业务系统）"""
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # default(保留) | external(外部用户内置租户) | business(普通业务租户)
    tenant_type: Mapped[str] = mapped_column(String, default="business", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 租户=企业组织：简介与头像（logo）由超管维护（创建/编辑）。
    # 头像以 data URL 字符串存库（≤2MB，png/jpg/webp），不依赖文件系统。
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    avatar: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """注册用户表（经 JWT 登录的真实人类账号）"""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Super_Admin 不归属任何业务租户 -> 可空
    tenant_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("tenants.id"), index=True, nullable=True)
    # 用户名全局唯一（跨租户唯一）：登录仅凭 用户名+口令，无需再指定租户。
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    # 固定角色：admin | member。创建路径一律显式赋值（建用户=member、建管理员/注册=admin）；
    # Super_Admin 为 None（不参与租户角色）。不设列级 default 以免覆盖 Super_Admin 的显式 None。
    role: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 初始/重置临时口令明文：仅在用户首次改密前保留，供管理员再次查看/复制；
    # 用户改密后由 change_password 置空。安全权衡：明文窗口仅限"首登改密前"。
    temp_password: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # 个人简介与头像：本人自助维护（介绍可改、头像各自维护自己的）。
    # 头像以 data URL 字符串存库（≤2MB，png/jpg/webp）。
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    avatar: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 经哪条邀请链接创建（自助接受邀请建号时写入）；管理员手动建号为 NULL。
    # 供"查询某邀请链接创建了哪些用户"。
    created_via_invitation_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    # 停用/重置口令时自增，使旧 JWT 失效（token 内 token_version 不匹配即拒绝）
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExternalUser(Base):
    """外部用户关联表：独立于 users，避免两类人群混入（R29.5）

    命名空间隔离核心：(key_source, external_user_id) 复合唯一——
    同 external_user_id 但不同代理 Key 来源解析为两个彼此独立的外部用户。
    """
    __tablename__ = "external_users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), index=True, nullable=False)  # = External_User_Tenant
    # 命名空间前缀：签发该请求所用 External_Agent_ApiKey 的标识（api_keys.id）
    key_source: Mapped[str] = mapped_column(String, nullable=False)
    # 调用方通过 X-External-User-Id 传入的原始值
    external_user_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("key_source", "external_user_id", name="uq_external_identity"),
    )


class KnowledgeBaseGrant(Base):
    """统一知识库授权/共享表：把 私有/组织公共/点对点 统一到一套授权数据（R16）"""
    __tablename__ = "knowledge_base_grants"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kb_id: Mapped[str] = mapped_column(String, ForeignKey("knowledge_bases.id"), index=True, nullable=False)
    # 本次重构后语义固定为 user：废弃自定义角色后按角色共享失去依附，
    # organization/tenant 跨租户预留亦一并移除，grantee_type 恒为 "user"。
    grantee_type: Mapped[str] = mapped_column(String, nullable=False, default="user")
    grantee_id: Mapped[str] = mapped_column(String, nullable=False)  # user_id
    permission: Mapped[str] = mapped_column(String, nullable=False)  # read | write
    granted_by: Mapped[str] = mapped_column(String, nullable=False)  # 发起共享的 user_id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("kb_id", "grantee_type", "grantee_id", name="uq_grant_target"),
    )


class AuditLog(Base):
    """审计日志（只追加，不可改删）。仅记录元数据，绝不含业务内容正文。

    actor_tenant_id 为操作者租户（Super_Admin 为 NULL）；租管查询仅限本租户，
    超管可查全局。detail 存动作相关的 id/名称等元数据 JSON。
    """
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    actor_user_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    actor_username: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    actor_tenant_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    actor_is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 操作者写入时刻的固定角色快照（admin/member/None）。审计是不可变事实，故快照而非
    # 展示时 join 当前用户（用户后续改角色/删号不影响历史记录的真实性）。
    actor_role: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, index=True, nullable=False)  # AuditActionEnum
    target_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # tenant|user|role|api_key|kb|invitation
    target_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    target_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # 仅元数据
    result: Mapped[str] = mapped_column(String, default="success", nullable=False)  # success|fail
    ip: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Invitation(Base):
    """邀请链接（带有效期 + 可选次数）。token 只存哈希。

    scope=create_tenant（仅 Super_Admin 签发，被邀请人建租户+自身为租管）；
    scope=create_user（user:manage 签发，锁签发者租户，建普通用户）。
    有效期由 expires_at 强制；max_uses 可选（null=有效期内不限次）。
    """
    __tablename__ = "invitations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # 明文 token：邀请链接需随时可复制/重复使用（管理员会忘记或多次发放），
    # 故保留明文供列表展示与复制。安全权衡：邀请本身受 expires_at + max_uses 约束，
    # 且接受建号仍走口令校验；吊销(is_active=False)即失效。
    token_plain: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)  # InvitationScopeEnum
    # create_user 时为目标租户；create_tenant 时为 NULL
    tenant_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    # create_user 时新用户预设角色名列表；create_tenant 忽略
    role_names: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    max_uses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # null=不限次
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_by_username: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KbShareLink(Base):
    """跨租户知识库分享链接（cross-tenant-kb-share）。

    场景：A 租户用户把自己拥有的某个知识库，通过链接点对点只读分享给任意租户的某个用户。
    被分享方登录后凭 token "领取"——领取动作向 ``KnowledgeBaseGrant`` upsert 一条
    ``grantee_type=user``、``grantee_id=领取者 user_id`` 的授权记录（与租户内点对点分享
    共用同一套授权数据），故授权天然跟人走、换租户不影响。

    与 Invitation 区分：Invitation 用于"建租户/建用户"（免登录建号）；本表用于"已存在用户
    领取一个已存在 KB 的访问权"，必须登录后领取（领取者即被授权主体）。

    安全：token 只存哈希（token_plain 供创建者复制重发，与 Invitation 同款权衡）；
    受 expires_at + max_uses 约束；吊销即 is_active=False。第一版仅 read。
    本表不继承 TenantScopedMixin：分享链接本身按 owner_user_id 归属与查询，
    且领取方跨租户读取本表元数据属正常流程，不应被租户兜底过滤拦截。
    """
    __tablename__ = "kb_share_links"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # 明文 token：供创建者列表展示/复制重发（与 Invitation 同款权衡，受有效期+次数约束）
    token_plain: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # 被分享的知识库
    kb_id: Mapped[str] = mapped_column(String, ForeignKey("knowledge_bases.id"), index=True, nullable=False)
    # 分享发起者（必为 KB owner）所属租户与用户，供展示"谁分享的"与审计
    owner_tenant_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    owner_user_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    # 授予权限：第一版固定 read（保留列以便后续扩展 write）
    permission: Mapped[str] = mapped_column(String, default="read", nullable=False)
    max_uses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # null=有效期内不限次
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ============================================================
# knowledge-graph：知识图谱抽取任务台账新增模型
# 新表，经 init_db 的 Base.metadata.create_all 自动创建，无需迁移脚本。
# ============================================================


class GraphExtractJob(Base, TenantScopedMixin):
    """知识图谱抽取任务台账（按文档一行）。

    借鉴 WeKnora pending_subtasks_count + attempt 机制：
    - 一个文档的图谱抽取按 parent chunk 拆成 N 个子任务；
    - pending_subtasks 初始 = N，每个子任务终态（成功/终败）原子 -1；
    - 归零 → status=completed；
    - attempt 自增使重解析前的陈旧子任务失效（不污染新数据）。

    带 TenantScopedMixin 走现有租户隔离。当前 attempt 的权威值存放在 Document 上
    （Document.graph_attempt），本表的 attempt 是该次任务的快照，用于子任务回写时
    与 Document 当前 attempt 比对判断是否陈旧。
    """
    __tablename__ = "graph_extract_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # job id（uuid）
    kb_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    doc_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|processing|completed|failed|cancelled
    pending_subtasks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_subtasks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    entities_count: Mapped[int] = mapped_column(Integer, default=0)
    relations_count: Mapped[int] = mapped_column(Integer, default=0)
    # 事件中心图谱：本次抽取写入的事件数（event-centric-graph，worker 双写 Neo4j+Milvus 后累加）。
    events_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # 一个 (kb_id, doc_id) 同时只有一个活跃 job；重解析自增 attempt 后旧行保留供审计
        UniqueConstraint("kb_id", "doc_id", "attempt", name="uq_graph_job_doc_attempt"),
    )


class GraphCommunity(Base, TenantScopedMixin):
    """知识图谱社区摘要（GraphRAG Global，阶段 4 扩展点）。

    用 Neo4j GDS Louvain 对某 KB 的实体图做社区发现后，对每个社区用 LLM 生成一段摘要，
    落库本表。回答全局 / 归纳类问题（如「这个库整体讲了什么」）时检索这些社区摘要，与
    chunk 检索结果融合（task 9.2）。

    带 TenantScopedMixin 走现有租户隔离，与 GraphExtractJob 同款约束风格。社区是按 KB
    整体重算的，重建时按 kb_id 先清后插（见 graph_store.build_community_summaries），
    故 (kb_id, community_key, level) 唯一即可。
    """
    __tablename__ = "graph_communities"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # uuid
    kb_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    # Louvain 社区编号（字符串化存储，跨版本/跨重算稳定可比对）
    community_key: Mapped[str] = mapped_column(String, index=True, nullable=False)
    # 层级（预留多级社区；当前单级恒为 0）
    level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    entity_count: Mapped[int] = mapped_column(Integer, default=0)
    relation_count: Mapped[int] = mapped_column(Integer, default=0)
    # 社区成员实体 id 列表（反查 / 调试用）
    member_entity_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # 社区摘要向量在 Milvus 的引用 id（预留 task 9.2 的社区摘要向量检索，可空）
    embedding_ref: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("kb_id", "community_key", "level", name="uq_graph_community_kb_key_level"),
    )
