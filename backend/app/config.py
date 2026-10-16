"""配置管理"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 服务
    host: str = "0.0.0.0"
    port: int = 8000

    # 数据库
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/artoo"
    # 数据库连接池（每进程）。单进程连接上限 = pool_size + max_overflow。
    # 部署须满足：上限 ×（backend 进程数 + worker 进程数）≤ Postgres max_connections
    # （中间件已设 200）。按服务器内存与并发上调，但别超过 PG 上限。
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # 线程池（asyncio.to_thread 的默认 executor）。承载同步阻塞调用：
    # 文档解析/切片、pymilvus 同步检索、bcrypt 口令哈希等。
    # 0 = 用 Python 默认 min(32, CPU+4)；设正整数则显式固定上限（按 CPU 核数调）。
    thread_pool_max_workers: int = 0

    # Milvus
    milvus_host: str = "localhost"
    milvus_port: int = 19530

    # ============================================================
    # Milvus Collection 拓扑（共享 collection + Partition Key + 按维度分表）
    # ============================================================
    # 物理 collection 名 = `<base>_<dim>`，例如 artoo_chunks_1024。
    #
    # 三条设计约束：
    # 1) 知识库数量**无上限**：全部正式知识库共用同一个物理 collection，`kb_id` 作为
    #    Partition Key 由 Milvus 自动 hash 分桶。collection 数量只随「向量维度种类」增长，
    #    与知识库数量完全解耦，因此新建知识库是零物理开销的（只是一个标量值）。
    # 2) 检索可裁剪：每次检索/删除都带 `kb_id == "..."`（或 `kb_id in [...]`）标量条件，
    #    Milvus 据 Partition Key 只扫相关分区，而不是全表扫描后过滤。
    # 3) 换 embedding 模型不停机：不同维度落不同 collection，新模型写新表、旧数据仍可查，
    #    重建索引可灰度进行，不需要「先清空再重灌」的停机窗口。
    milvus_collection: str = "artoo_chunks"
    # 会话附件独立一套 collection，以 `session_id` 作为 Partition Key：
    # 会话是短生命周期、高基数的隔离维度，若与正式库合表会全部落进同一个 kb_id 分桶
    # 形成热点分区，独立成表后每个会话都能按分区裁剪。
    milvus_session_collection: str = "artoo_session_chunks"
    # 当前 embedding 维度，决定读写落到哪个 `<base>_<dim>` collection。
    # BGE-M3 = 1024；OpenAI text-embedding-3-* = 1536/3072；Qwen3-Embedding = 1024/2560/4096。
    # 换模型时改这里 + 重建索引；旧维度 collection 会被保留（删除操作仍会跨全部维度清理，
    # 不留孤儿向量），确认无用后可用 `python -m scripts.init_milvus --prune-dims` 清掉。
    embed_dim: int = 1024
    # Partition Key 的物理分区数（**建表时固定，之后不可修改**；改动需重建 collection）。
    #
    # 关键认知：分区数**不是**知识库数量上限——无上限个 kb_id 会 hash 进这 N 个分桶。
    # 它只决定「一次检索最少要扫多少比例的数据」（≈ 1/N）与写入侧的 segment/内存开销。
    #
    # 容量参考（按单个 collection 的向量总量选，不是按知识库个数）：
    #   < 5M 向量   -> 64（默认，单机 + mmap 部署的稳妥值）
    #   5M ~ 50M    -> 256
    #   > 50M       -> 512 ~ 1024（上限 1024）
    # 分区数偏大在数据量小时会放大 growing segment 与内存开销，故默认取 64 而非更大值。
    milvus_num_partitions: int = 64
    milvus_session_num_partitions: int = 16
    # 写入分片数（num_shards，建表时固定）。决定写入并行度上限：单分片是单点写入瓶颈。
    # 0 = 用 Milvus 默认（standalone 通常为 1）。多副本集群 / 高并发灌库场景建议 2~4。
    # 注意：分片数 = vchannel 数，过大会加重 message queue 与 dispatcher 压力。
    milvus_shards_num: int = 0
    # 读副本数（replica_number，LoadCollection 时生效，可随时改无需重建表）。
    # >1 让多个 QueryNode 各持一份数据，提升读吞吐与可用性；单机 standalone 保持 0/1。
    # 每增加一个副本，常驻内存需求翻一倍。
    milvus_replica_number: int = 0

    # MinIO / 对象存储（知识库源文件权威存储）
    # endpoint 形如 "localhost:9000"（不带 scheme）；secure=True 时走 https。
    # 复用为 Milvus 部署的同一 MinIO，但用独立 bucket 隔离业务文件与 Milvus 内部数据。
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_bucket: str = "aladdin-documents"
    # 启动期对账孤儿对象的宽限期（秒）：仅删除 last_modified 早于 now-grace 的孤儿对象，
    # 避免误删正在上传/建索引中（DB 行尚未提交）的新对象。
    minio_orphan_grace_seconds: int = 3600

    # LLM
    llm_provider: str = "ollama"  # ollama | vllm
    llm_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b"
    llm_api_key: str = ""  # 远端 API 的密钥（vllm provider 使用）
    # 未在模型配置 / 请求里显式指定时的单次输出上限；None 表示沿用模型服务默认。
    llm_max_output_tokens: int | None = None

    # Embedding（远程服务）
    embed_model: str = "BAAI/bge-m3"
    embed_base_url: str = ""  # 远程 Embedding 服务地址（如 http://server:8080/v1）
    embed_api_key: str = ""  # API 密钥（可选）
    embed_sparse_enabled: bool = True  # 是否启用 sparse 向量（需服务支持 /embed_sparse 端点）

    # Rerank（远程服务）
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_base_url: str = ""  # 远程 Rerank 服务地址（如 http://server:8001/v1）
    rerank_api_key: str = ""  # API 密钥（可选）

    # Agent
    agent_max_iterations: int = 10
    # 平台级上限：同一 Agent 轮次内并行执行的工具调用数量。
    agent_tool_call_max_concurrency: int = 4
    agent_timeout: float = 30.0
    searxng_url: str = "http://localhost:8080"

    # Chunking
    parent_chunk_size: int = 2500
    child_chunk_size: int = 450
    chunk_overlap: int = 70

    # Redis（检索缓存 + 任务队列）
    redis_url: str = "redis://localhost:6379/0"
    retrieval_cache_ttl: int = 1800  # 缓存 TTL（秒），默认 30 分钟

    # OCR 配置
    ocr_enabled: bool = True
    ocr_provider: str = "external_api"
    ocr_fallback_provider: str = ""

    # 外部 OCR API 配置
    ocr_external_api_url: str = ""
    ocr_external_api_key: str = ""
    ocr_external_api_timeout: float = 30.0

    # ASR（语音识别）配置
    asr_enabled: bool = True
    asr_api_url: str = ""
    asr_api_key: str = ""
    asr_model_name: str = ""
    asr_language: str = ""
    asr_timeout: float = 300.0

    # Pipeline Worker
    # 文档准入并发数：Worker 同时推进处理的文档数（含 load/chunk/index 等阶段）。
    # 注意：准入信号量在单个文档「全程」（含 Embedding/OCR I/O 等待）持有，不会中途释放，
    # 所以小文件并不会自动趁大文件 I/O 间隙插队。保护小文件不被大文件饿死，依赖的是
    # 「慢道预留」机制（见 pipeline_slow_lane_min_mb / pipeline_slow_max_concurrent）：
    # 大文件走慢道并受 slow_max_concurrent 限流，快道始终保留
    # (max_concurrent - slow_max_concurrent) 个名额给小文件。
    pipeline_max_concurrent: int = 4
    pipeline_max_retries: int = 3
    pipeline_slow_threshold_ms: int = 30000
    pipeline_task_timeout_minutes: int = 60  # 单个文档处理总超时（分钟）
    pipeline_circuit_breaker_threshold: int = 5  # 连续失败 N 次触发熔断
    pipeline_health_check_interval: int = 30  # 健康检查/熔断恢复轮询间隔（秒）
    # PEL 孤儿任务周期性回收（崩溃恢复）
    pipeline_claim_interval_seconds: int = 60  # 周期性认领 PEL 中超时消息的间隔（秒）
    # 消息 idle 超过此值才回收。必须大于 task_timeout，否则会抢走正在被合法处理的消息
    # 导致同一文档重复处理。运行时会强制提升到 task_timeout+5 以下不生效。
    pipeline_claim_min_idle_minutes: int = 65
    pipeline_embed_batch_size: int = 32  # Embedding 每批文本数
    # Embedding 全局并发：所有文档共享的并发上限，保护远程 Embedding 服务不被打爆
    pipeline_embed_concurrency: int = 6
    # 单文档 Embedding 并发上限：限制单个文档最多占用多少个全局 slot，
    # 必须小于 pipeline_embed_concurrency，保证多文档之间交错执行、小文件不被大文件饿死
    pipeline_embed_per_doc_concurrency: int = 2
    # OCR 全局并发：所有文档共享的 OCR 调用并发上限
    pipeline_ocr_concurrency: int = 4
    # ASR 全局并发：所有文档共享的 ASR（语音转写）调用并发上限
    pipeline_asr_concurrency: int = 2
    pipeline_embed_max_connections: int = 20  # httpx 连接池上限

    # 会话文件异步上传（session-upload-async-ws）——与文档入库快/慢道物理隔离，
    # 全部带安全默认值，缺配置不影响启动。
    session_upload_max_concurrent: int = 4  # 会话上传 worker 并发建索引数
    session_upload_max_retries: int = 3  # 单任务失败重试上限（超限进 DLQ）
    session_upload_task_timeout_minutes: int = 30  # 单任务建索引总超时（分钟）
    session_upload_ws_max_conn_per_session: int = 20  # 单会话 WS 连接数上限（超限拒绝新连接）
    session_upload_ws_ping_interval: int = 30  # WS 服务端心跳/keepalive 间隔（秒）

    # 大文件分道（fast/slow 双队列）：文件大小 ≥ 此阈值走 slow 道，避免大文件占满快道。
    # 该值须低于业务中常见「大文件」的体积，否则大文件全部落入快道、占满 max_concurrent，
    # 慢道机制失效、小文件被队头阻塞。
    pipeline_slow_lane_min_mb: int = 10
    # slow 道在单个 Worker 内的最大在途文档数（小于 max_concurrent，给快道留出固定额度）
    pipeline_slow_max_concurrent: int = 1

    # 前端配置（通过 /api/system/frontend-config 下发）
    upload_max_concurrent: int = 3  # 前端并发上传数
    upload_max_file_size_mb: int = 500  # 单文件最大 MB

    # ============================================================
    # 认证与授权（tenant-rbac-refactor）
    # ============================================================
    # 鉴权始终强制：已移除 auth_enabled 旁路后门（清理 E），任何受保护端点缺有效凭据恒 401。

    # JWT（HS256）。jwt_secret 为启动期硬依赖：缺失则在 get_settings() fail-fast。
    jwt_secret: str = ""
    jwt_expire_minutes: int = 720  # JWT 有效期（分钟），默认 12 小时

    # ---- API Key AK/SK 签名（aksk-signing）：供无后端的可信调用方免明文密钥上行 ----
    # 请求以 HMAC-SHA256 签名认证：AK=api_key.id（可公开），SK 由服务端从
    # jwt_secret 派生（不落库，DB 泄露也无法伪造）。此处仅时间窗；nonce 防重放走 Redis。
    apikey_sign_window_seconds: int = 300  # 签名时间窗（秒），|now-ts|>窗口即拒绝（防重放）

    # Super_Admin 引导（SuperAdminBootstrap）。首次启动且无 Super_Admin 时据此创建，
    # 并强制改密。缺失时 fail-fast，禁止用默认口令静默兜底。
    super_admin_username: str = ""
    super_admin_password: str = ""

    # 注册模式（env 可配置）：
    #   invite_only（默认）—— 关闭自助注册：登录页无注册入口，/api/auth/register 返回 403；
    #     建号仅由租户管理员在本租户内创建，或经邀请链接。
    #   self_serve —— 开放“租户自助注册”：任何人可注册并**自动开通一个独立租户**，
    #     注册人成为该租户管理员（不暴露/穿透他人租户，符合硬隔离）。
    registration_mode: str = "invite_only"

    # 超管业务内容可见边界：False（默认）= Super_Admin 不可查看业务内容正文。
    content_view_boundary_open: bool = False

    # ============================================================
    # MCP（Model Context Protocol）
    # ============================================================
    # ---- inbound：Artoo 作为 MCP server（标准 JSON-RPC over Streamable HTTP）----
    # 单次工具调用最多并发检索的知识库数。身份可读库很多时，逐库检索会把一次调用放大
    # 成几十次检索并拖到客户端超时；取分数最高的前 N 个库并发检索后统一归并。
    mcp_max_kb_fanout: int = 8
    # 每把 API Key 每分钟的 tools/call 上限（进程内令牌桶）。0 = 关闭限流。
    mcp_rate_limit_per_minute: int = 120
    # MCP 会话（Mcp-Session-Id）空闲过期时间（秒）
    mcp_session_ttl_seconds: int = 3600

    # ---- outbound：Artoo 作为 MCP client（调用第三方 MCP server）----
    # 是否禁止连接内网/环回地址的 MCP server。默认 False：MCP server 与 Artoo 同处
    # 内网/同 docker 网络是主流部署形态。置 True 得到更严的 SSRF 策略（仅公网可达地址）。
    # 无论此项如何，link-local（云元数据端点）恒被阻断。
    mcp_block_private_network: bool = False
    mcp_call_timeout: float = 60.0       # 单次远端工具调用超时（秒）
    mcp_discovery_timeout: float = 10.0  # 工具发现 / 连通性测试超时（秒）

    # ============================================================
    # 知识图谱（knowledge-graph）：全局 env 开关与 Neo4j 连接/抽取参数
    # ============================================================
    # 全局总开关：不为 true 则整体关闭图谱功能（不连 Neo4j、不抽取、API 返回明确不可用），
    # 主链路零额外成本（Req 9.3）。映射 env GRAPH_ENABLE。
    graph_enable: bool = False
    # Neo4j 连接配置（仅 graph_enable=true 时使用）
    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = "password"
    neo4j_max_pool_size: int = 50  # 连接池上限（抗压，Req 9.2）
    neo4j_conn_timeout: float = 5.0  # 连接超时（秒）
    # 单次图查询事务超时（秒），防恶意 depth/limit（Req 9.1）
    graph_query_timeout: float = 10.0
    # 抽取慢道并发信号量上限（与文档入库 worker 物理隔离，不挤占主链路）
    graph_extract_concurrency: int = 2
    # 抽取子任务最大重试次数（超过进 DLQ）
    graph_extract_max_retries: int = 3
    # housekeeping 巡检：超过此时长仍未到达终态（pending/processing）的 GraphExtractJob
    # 视为卡死（worker 硬崩溃导致 pending_subtasks 不归零），置 failed 并零化计数器（Req 4.4）。
    graph_job_timeout_minutes: int = 30
    # housekeeping 巡检周期（秒）。仅 graph_enable=true 时启动巡检循环。
    graph_housekeeping_interval_seconds: int = 300
    # ---- 阶段 4（GraphRAG Global，可选）：GDS Louvain 社区发现 ----
    # 社区发现需 Neo4j GDS 插件（enterprise 或社区 GDS）。GDS 不可用时社区发现优雅降级
    # （返回空 / 跳过，warning 不 crash），不影响阶段 1~3。
    # 社区最小成员数：小于此规模的社区不生成摘要（噪声社区过滤）。
    graph_community_min_size: int = 3
    # 单 KB 生成摘要的社区数上限（控制 LLM 调用成本）。
    graph_community_max_communities: int = 100
    # 喂给 LLM 生成单社区摘要的成员实体数上限（控制 prompt 长度）。
    graph_community_max_members_for_summary: int = 30

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache()
def get_settings() -> Settings:
    """获取全局配置单例。

    启动期 fail-fast：jwt_secret 为空即抛 RuntimeError，禁止用空密钥静默兜底进入
    可服务状态（清理 E 后鉴权始终强制，密钥缺失必须显式失败）。
    """
    settings = Settings()
    if not settings.jwt_secret:
        raise RuntimeError(
            "jwt_secret 未配置：请设置环境变量 JWT_SECRET（HS256 签名密钥），"
            "缺失时服务拒绝启动（fail-fast）。"
        )
    return settings
