# Agent Note: Serialized one-shot database migration

Status: implemented

## Problem

每个 uvicorn worker 和摄入 Worker 都在应用启动时执行 schema DDL。一次部署可能并行
执行多条 `ALTER TABLE`，同时摄入事务仍持有长期表锁。2026 年 11 月事故中，
`ALTER TABLE documents` 在四个索引事务后等待了 35 分钟，所有 API worker 一直停在
启动阶段，直到进程被重启。

## Decision

生产 Compose 在 `backend`/`worker` 启动前运行独立的 one-shot `migrate` 服务。迁移入口
获取 PostgreSQL transaction advisory lock，并设置有限的 `lock_timeout` 与
`statement_timeout`。`ADD COLUMN` 会改写为 `ADD COLUMN IF NOT EXISTS`，使重放幂等，
不再产生预期内的重复列错误。

`backend`/`worker` 设置 `AUTO_MIGRATE_ON_STARTUP=false`，启动时只验证数据库可达。
开发与手工宿主机运行默认仍启用自动迁移。残留 `processing` 文档的恢复移动到 Worker
启动阶段，API worker 不再执行该写操作。

## Alternatives considered

**保留应用启动迁移，只增加 advisory lock。** 否决，因为所有应用进程的可用性仍与 DDL
耦合，一个长期摄入事务就能阻止全部 API worker 就绪。

**只调大 `lock_timeout`，不拆分迁移。** 否决，因为启动会因无关业务流量而失败，或者
跳过所需 schema 变更。

**立即引入完整外部迁移框架。** 这是可行的长期方向，但现有应用内迁移首先需要明确的
运行隔离边界。后续可替换 one-shot 入口而不改变部署契约。

## Consequences

部署多一个 one-shot 容器，并且启动或重建 backend/worker 前必须运行 `migrate`。迁移
失败会阻止应用启动，而不是让 API 在未知 schema 上继续服务。直接使用 Compose 时由
`depends_on: service_completed_successfully` 保证顺序；`install.sh` 也会显式运行并
等待迁移完成。更新会先把当前应用镜像标记为 `rollback-pre-update-<时间>`，并默认保留
被替换的镜像，使上一版可以立即回滚；显式设置 `CLEAN_OLD_IMAGES=1` 才执行清理。部署脚本只恢复顶层 shell 脚本的执行位，
并强制 `.env` 为 `0600`，不会递归放开部署密钥的读取权限。

## Testing

`backend/tests/test_ingestion_lock_safety.py` 验证 `ADD COLUMN` 可安全重放以及超时格式。
部署验证覆盖 Compose 解析与 `install.sh` shell 语法。一次性 PostgreSQL 16 实例验证
迁移命令连续执行两次均成功，并创建了预期的 document/session 列。当另一个会话持有
`documents` 的 `ACCESS SHARE` 锁时，重放迁移会在 5 秒锁超时后退出，而不是无限等待。
