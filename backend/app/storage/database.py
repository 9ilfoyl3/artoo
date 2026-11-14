"""PostgreSQL 数据库初始化与会话管理"""

import logging
import re
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.schema.db import Base

_settings = get_settings()
_database_url = _settings.database_url
logger = logging.getLogger(__name__)

# PostgreSQL advisory lock 的任意稳定 int64 标识，仅用于串行化 schema migration。
_MIGRATION_ADVISORY_LOCK_KEY = 0x4152544F4F4D4947  # "ARTOOMIG"

# 仅支持 PostgreSQL。配置不带驱动的 URL 时自动补异步驱动。
if _database_url.startswith("postgresql://"):
    _database_url = _database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

# 异步引擎（PostgreSQL 连接池，池大小由配置项控制，按服务器硬件调）
engine = create_async_engine(
    _database_url,
    echo=False,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_timeout=_settings.db_pool_timeout_seconds,
    pool_recycle=_settings.db_pool_recycle_seconds,
)

# 异步会话工厂
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# 安装租户隔离兜底（方案 B）：对所有 TenantScopedMixin 模型按 contextvar 三态自动
# 注入 tenant 过滤。幂等，API 与 Worker 各 import 一次本模块即生效。
from app.repositories.tenant_repo import install_tenant_loader_criteria  # noqa: E402

install_tenant_loader_criteria()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话（用于 FastAPI 依赖注入）"""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _migration_statements() -> list[str]:
    """Return the ordered, application-managed schema migration statements."""
    return [
        "ALTER TABLE llm_configs ADD COLUMN stream_enabled BOOLEAN DEFAULT TRUE",
        "ALTER TABLE llm_configs ADD COLUMN max_context_tokens INTEGER",
        "ALTER TABLE llm_configs ADD COLUMN max_output_tokens INTEGER",
        "ALTER TABLE llm_configs ADD COLUMN chat_visible BOOLEAN NOT NULL DEFAULT TRUE",
        # 模型厂商（vendor）+ 思考模式参数格式：显式化，取代脆弱的 base_url/模型名自动匹配
        # （思考是否开启由智能体预设独占控制，thinking_control 仅决定写入 API 的字段格式）
        "ALTER TABLE llm_configs ADD COLUMN vendor VARCHAR",
        "ALTER TABLE llm_configs ADD COLUMN thinking_control VARCHAR",
        # 文档表新增字段
        "ALTER TABLE documents ADD COLUMN folder_id VARCHAR REFERENCES folders(id)",
        # 文档表新增进度追踪字段
        "ALTER TABLE documents ADD COLUMN progress INTEGER DEFAULT 0",
        "ALTER TABLE documents ADD COLUMN progress_message VARCHAR",
        "ALTER TABLE documents ADD COLUMN file_hash VARCHAR",

        # 链接转存（url-import）：记录网页正文转存文档的原文链接，便于溯源展示。
        "ALTER TABLE documents ADD COLUMN source_url VARCHAR",

        # 知识图谱（knowledge-graph）：文档抽取状态与权威 attempt 计数。
        # NOT NULL 带 DEFAULT，已有行自动回填（none / 0）。
        "ALTER TABLE documents ADD COLUMN graph_status VARCHAR NOT NULL DEFAULT 'none'",
        "ALTER TABLE documents ADD COLUMN graph_attempt INTEGER NOT NULL DEFAULT 0",
        # 清理历史遗留列：旧版本 embed_configs 表带 device NOT NULL 列（本地推理设备），
        # 当前模型已移除该字段（统一走 remote），插入时不再赋值，会触发 NOT NULL 约束错误。
        # 解除其 NOT NULL 约束以兼容旧库（列保留，值留空，无数据丢失）。
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'embed_configs'
                  AND column_name = 'device'
            ) THEN
                ALTER TABLE embed_configs ALTER COLUMN device DROP NOT NULL;
            END IF;
        END $$;
        """,
        # Embedding 配置表新增 sparse 支持字段
        "ALTER TABLE embed_configs ADD COLUMN sparse_enabled BOOLEAN DEFAULT TRUE",
        # Embedding/Rerank 配置表新增厂商（vendor）列：仅用于 UI 记忆所选服务商
        "ALTER TABLE embed_configs ADD COLUMN vendor VARCHAR",
        # 对话消息表新增知识库追踪字段
        "ALTER TABLE chat_messages ADD COLUMN kb_id VARCHAR",
        "ALTER TABLE chat_messages ADD COLUMN kb_ids JSON",
        # Agent typed response steps used by history replay.
        "ALTER TABLE chat_messages ADD COLUMN agent_steps JSON",
        # 对话消息表新增附件字段：用户消息发送时绑定的会话文件快照（session-file-upload）
        "ALTER TABLE chat_messages ADD COLUMN attachments JSON",
        # 对话消息表新增反馈字段：用户对 AI 回答的点赞/踩（chat-message-actions，保留供 agent 优化）
        "ALTER TABLE chat_messages ADD COLUMN feedback VARCHAR(10)",
        # 会话表新增归属用户列：会话/消息为个人对话历史，须按 owner 收敛（per-user 隔离）。
        # 已存在的历史会话 owner_user_id 留空（NULL），将不再出现在任何用户的列表中
        # （无主会话对所有人不可见），避免修复前的跨用户泄露在旧数据上残留。
        "ALTER TABLE chat_sessions ADD COLUMN owner_user_id VARCHAR",
        # 清理历史遗留列：旧版本 knowledge_bases 表带 retrieval_mode NOT NULL 列，
        # 当前模型已移除该字段，插入时不再赋值，会触发 NOT NULL 约束错误。
        # 解除其 NOT NULL 约束以兼容旧库（列保留，值留空，无数据丢失）。
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'knowledge_bases'
                  AND column_name = 'retrieval_mode'
            ) THEN
                ALTER TABLE knowledge_bases ALTER COLUMN retrieval_mode DROP NOT NULL;
            END IF;
        END $$;
        """,
        # ===== tenant-rbac-refactor：为旧库补齐租户隔离 / 归属 / 可见性列 =====
        # create_all 只新建缺失的表，不会为已存在的表补列；下列 ALTER 让升级前建立的
        # 旧库（已有业务数据）平滑获得新列。带 NOT NULL 的列给 DEFAULT，已有行自动回填。
        # 受租户隔离的资源表统一补 tenant_id（旧数据归属未知，留 NULL，由后续治理回填）。
        "ALTER TABLE knowledge_bases ADD COLUMN tenant_id VARCHAR",
        "ALTER TABLE knowledge_bases ADD COLUMN owner_user_id VARCHAR",
        "ALTER TABLE knowledge_bases ADD COLUMN visibility VARCHAR NOT NULL DEFAULT 'private'",
        "ALTER TABLE knowledge_bases ADD COLUMN org_permission VARCHAR NOT NULL DEFAULT 'read'",
        "ALTER TABLE folders ADD COLUMN tenant_id VARCHAR",
        "ALTER TABLE documents ADD COLUMN tenant_id VARCHAR",
        "ALTER TABLE chunks ADD COLUMN tenant_id VARCHAR",
        "ALTER TABLE chat_sessions ADD COLUMN tenant_id VARCHAR",
        "ALTER TABLE chat_messages ADD COLUMN tenant_id VARCHAR",
        # API Key 三模型字段（tenant_level / user_level / external_agent）
        "ALTER TABLE api_keys ADD COLUMN tenant_id VARCHAR",
        "ALTER TABLE api_keys ADD COLUMN key_type VARCHAR NOT NULL DEFAULT 'tenant_level'",
        "ALTER TABLE api_keys ADD COLUMN bound_user_id VARCHAR",
        "ALTER TABLE api_keys ADD COLUMN authorized_scope JSON",
        "ALTER TABLE api_keys ADD COLUMN key_source VARCHAR",
        # 索引（与模型 index=True 对齐；租户过滤在每次查询都会用到，缺索引影响性能）
        "CREATE INDEX IF NOT EXISTS ix_knowledge_bases_tenant_id ON knowledge_bases (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_bases_owner_user_id ON knowledge_bases (owner_user_id)",
        "CREATE INDEX IF NOT EXISTS ix_folders_tenant_id ON folders (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_documents_tenant_id ON documents (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_chunks_tenant_id ON chunks (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_chat_sessions_tenant_id ON chat_sessions (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_chat_messages_tenant_id ON chat_messages (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_api_keys_tenant_id ON api_keys (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_api_keys_bound_user_id ON api_keys (bound_user_id)",
        # ===== agent-preset-sharing：智能体预设归属与开放可见性 =====
        # 创建者归属 + 是否开放给本租户。内置预设 tenant_id/owner_user_id 为 NULL、
        # is_shared=TRUE（由 _ensure_builtin_presets 校正）。已存在的用户预设
        # owner_user_id 留 NULL（无归属→不可见于任何用户，按"不考虑存量兼容"重建即可）。
        "ALTER TABLE agent_presets ADD COLUMN tenant_id VARCHAR",
        "ALTER TABLE agent_presets ADD COLUMN owner_user_id VARCHAR",
        "ALTER TABLE agent_presets ADD COLUMN is_shared BOOLEAN NOT NULL DEFAULT FALSE",
        "CREATE INDEX IF NOT EXISTS ix_agent_presets_tenant_id ON agent_presets (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_agent_presets_owner_user_id ON agent_presets (owner_user_id)",
        # ===== session-file-upload：上传限制配置新增列 =====
        # 这两张配置表在 kb-retrieval-optimization / tenant-auth 时期已存在，create_all 只建
        # 缺失的整表、不会给存量表补列，故存量库需在此 ALTER 补列。全部 nullable（缺失语义
        # 由 RetrievalConfig / PlatformConfig.effective_from_raw 读时逐字段兜底 Safe_Default）。
        # 注：会话专属限额（session_max_files / session_chunk_cap / session_chunk_ceiling）已废弃，
        # 临时文件统一由 kb_chunk_cap 约束，不再补这些列（存量库残留列保持 nullable、不被读取）。
        "ALTER TABLE retrieval_configs ADD COLUMN upload_max_file_mb INTEGER",
        "ALTER TABLE platform_configs ADD COLUMN kb_chunk_cap INTEGER",

        # 知识图谱（knowledge-graph）：平台级抗压参数列。模型为 nullable，
        # 空值由 PlatformConfig.effective_from_raw 读时兜底默认值，故加列不带 DEFAULT。
        "ALTER TABLE platform_configs ADD COLUMN graph_overview_max_nodes INTEGER",
        "ALTER TABLE platform_configs ADD COLUMN graph_ego_max_nodes INTEGER",
        "ALTER TABLE platform_configs ADD COLUMN graph_ego_max_depth INTEGER",
        "ALTER TABLE platform_configs ADD COLUMN graph_retriever_hops INTEGER",
        "ALTER TABLE platform_configs ADD COLUMN graph_retriever_max_chunks INTEGER",
        # ===== asr-config：ASR 配置表 vendor 列（存量库若已建表则补列） =====
        "ALTER TABLE asr_configs ADD COLUMN vendor VARCHAR(50)",
        # ===== mcp-standard-protocol：MCP 配置表补齐传输模式 / 凭据 / 上下文透传 =====
        # 存量行取默认值即保持改造前行为：transport=auto（自动探测，老服务端仍走私有
        # REST）、auth_type=none（不带凭据）、forward_context=FALSE（不透传上下文）。
        "ALTER TABLE mcp_configs ADD COLUMN transport VARCHAR(20) NOT NULL DEFAULT 'auto'",
        "ALTER TABLE mcp_configs ADD COLUMN auth_type VARCHAR(20) NOT NULL DEFAULT 'none'",
        "ALTER TABLE mcp_configs ADD COLUMN auth_token_encrypted VARCHAR",
        "ALTER TABLE mcp_configs ADD COLUMN auth_header_name VARCHAR(100)",
        "ALTER TABLE mcp_configs ADD COLUMN forward_context BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE mcp_configs ADD COLUMN tool_prefix VARCHAR(50)",
        # ===== event-centric-graph：抽取台账新增事件计数列（存量库补列，缺省 0） =====
        # create_all 只建缺失整表、不给存量表补列；新增 events_count 供 worker 双写后累加。
        "ALTER TABLE graph_extract_jobs ADD COLUMN events_count INTEGER NOT NULL DEFAULT 0",
        # ===== session-file-upload：会话文件异步处理进度 =====
        "ALTER TABLE session_files ADD COLUMN progress INTEGER DEFAULT 0",
        "ALTER TABLE session_files ADD COLUMN progress_message VARCHAR",
        "ALTER TABLE session_files ADD COLUMN error_message TEXT",
        # ===== utc-timezone-fix：时间列统一为 timestamptz（带时区，内部存 UTC）=====
        # 根因：旧列为 TIMESTAMP WITHOUT TIME ZONE（naive），应用层混用 func.now()（容器
        # 本地东八区）/ datetime.utcnow() / datetime.now()，序列化无时区后缀，前端按浏览器
        # 时区瞎猜 → 跨时区/同表不同列差 8 小时。修复后全链路 UTC：列改 timestamptz，应用写
        # aware UTC，isoformat 自带 +00:00，前端 new Date() 正确转本地。
        #
        # 用 DO 块只转换「仍为 naive（timestamp without time zone）」的列，按 UTC 解释存量值。
        # 幂等且安全：新库经 create_all 已是 timestamptz，本块自动跳过（不会因重复 AT TIME ZONE
        # 造成偏移）；存量老库的 naive 列被一次性按 UTC 提升为 timestamptz（存量数据按用户确认
        # 可接受偏差，仅保证新数据一致）。
        """
        DO $$
        DECLARE r RECORD;
        BEGIN
            FOR r IN
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND data_type = 'timestamp without time zone'
            LOOP
                EXECUTE format(
                    'ALTER TABLE %I ALTER COLUMN %I TYPE TIMESTAMPTZ '
                    'USING %I AT TIME ZONE ''UTC''',
                    r.table_name, r.column_name, r.column_name
                );
            END LOOP;
        END $$;
        """,
    ]


def _idempotent_migration_sql(sql: str) -> str:
    """Make ADD COLUMN migrations replay-safe without emitting duplicate-column errors."""
    if re.search(r"\bADD\s+COLUMN\b", sql, flags=re.IGNORECASE) and not re.search(
        r"\bADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\b", sql, flags=re.IGNORECASE
    ):
        return re.sub(
            r"\bADD\s+COLUMN\b",
            "ADD COLUMN IF NOT EXISTS",
            sql,
            count=1,
            flags=re.IGNORECASE,
        )
    return sql


def _timeout_literal(seconds: float) -> str:
    milliseconds = max(1, int(float(seconds) * 1000))
    return f"{milliseconds}ms"


async def apply_migrations() -> None:
    """Apply schema changes once under a PostgreSQL advisory lock.

    This entrypoint is run by the dedicated ``app.migrate`` job. A short lock timeout
    prevents a startup from waiting indefinitely behind long ingestion transactions.
    """
    lock_timeout = _timeout_literal(_settings.migration_lock_timeout_seconds)
    statement_timeout = _timeout_literal(
        _settings.migration_statement_timeout_seconds
    )

    async with engine.begin() as conn:
        await conn.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout}'"))
        await conn.execute(
            text(f"SET LOCAL statement_timeout = '{statement_timeout}'")
        )
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(CAST(:lock_key AS bigint))"),
            {"lock_key": _MIGRATION_ADVISORY_LOCK_KEY},
        )
        await conn.run_sync(Base.metadata.create_all)

        for raw_sql in _migration_statements():
            sql = _idempotent_migration_sql(raw_sql)
            try:
                async with conn.begin_nested():
                    await conn.execute(text(sql))
            except Exception as exc:  # noqa: BLE001 - idempotent compatibility migration
                message = str(exc)
                if any(
                    keyword in message
                    for keyword in (
                        "does not exist",
                        "UndefinedColumn",
                        "UndefinedTable",
                    )
                ):
                    logger.debug("Migration skipped: %s | %s", sql[:80], exc)
                else:
                    logger.error("Migration failed: %s | %s", sql[:80], exc)
                    raise

    # Bootstrap is idempotent and intentionally runs after schema creation.
    from app.auth.bootstrap import run_bootstrap

    await run_bootstrap(async_session)


async def verify_database_ready() -> None:
    """Fail startup early when PostgreSQL is unreachable, without running DDL."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def init_db() -> None:
    """Prepare database access for API/Worker startup.

    Development/manual startup keeps auto-migration enabled. Production Compose sets
    ``AUTO_MIGRATE_ON_STARTUP=false`` and runs ``python -m app.migrate`` once before
    backend/worker, so multiple uvicorn workers never execute DDL concurrently.
    """
    if _settings.auto_migrate_on_startup:
        await apply_migrations()
    else:
        await verify_database_ready()


async def reset_interrupted_tasks() -> None:
    """重置被中断的任务：将 processing 状态的文档改为 failed

    服务重启时，processing 状态意味着上次处理被中断，不可能自动恢复。
    """
    try:
        async with engine.begin() as conn:
            lock_timeout = _timeout_literal(_settings.migration_lock_timeout_seconds)
            await conn.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout}'"))
            result = await conn.execute(
                text(
                    "UPDATE documents SET status='failed', error_message='服务重启，处理中断' "
                    "WHERE status='processing'"
                )
            )
            if result.rowcount > 0:
                logger.info("重置 %d 个中断的文档为 failed 状态", result.rowcount)
                print(f"[Init] 重置 {result.rowcount} 个中断的文档为 failed 状态")
    except Exception as exc:  # noqa: BLE001 - stale worker state is best-effort
        logger.warning("重置中断任务失败（不阻断 Worker 启动）: %s", exc)
