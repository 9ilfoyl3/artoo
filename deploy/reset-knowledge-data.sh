#!/usr/bin/env bash
# ============================================================
# Milvus 拓扑切换：知识内容全量清除脚本（在已部署的服务器上执行）
#
# 背景
#   向量拓扑从「每个知识库一个 collection」切换为「共享 collection + Partition Key
#   + 按维度分表」。Milvus 的 Partition Key 字段、num_partitions、num_shards 与向量
#   维度都是**建表时固定**的，无法事后修改，因此必须重建物理表。
#   本脚本走「不向前兼容」路径：直接清空知识内容，让系统以新拓扑从零开始。
#
# 会做什么
#   1. 前置检查（compose 可用、容器在跑、镜像已是新版本）
#   2. 备份 PostgreSQL 到 ./backups/（失败即中止，保证有回滚依据）
#   3. 停止 backend / worker（避免清除过程中有新数据写入）
#   4. 在 backend 容器内执行 scripts/purge_data.py
#   5. 重启 backend / worker（启动时自动按新配置建表）
#   6. 打印清除后的拓扑供复核
#
# 会清空：Milvus 全部向量（含旧 kb_* 与 kb_event_*）、documents / chunks / folders /
#         session_files / session_chunks / 图谱任务与社区、对象存储全部原件、
#         Neo4j 图谱、Redis 在途任务与检索缓存。
# 会保留：租户 / 用户 / API Key / 知识库本体及授权 / 模型与检索配置 / 对话记录。
#
# 用法：
#   bash deploy/reset-knowledge-data.sh --dry-run     # 只体检，不改任何东西（先跑这个）
#   bash deploy/reset-knowledge-data.sh               # 交互确认后执行
#   bash deploy/reset-knowledge-data.sh --yes         # 跳过交互确认（自动化）
#   bash deploy/reset-knowledge-data.sh --include-kbs --include-chats   # 连库与对话一起清
#   bash deploy/reset-knowledge-data.sh --keep-objects # 保留源文件（之后可重建索引而非重传）
#   bash deploy/reset-knowledge-data.sh --skip-backup  # 跳过备份（不推荐）
# ============================================================
set -euo pipefail

# 定位到 docker-compose.yml 所在目录。兼容两种布局：
#   开发仓库：  <repo>/deploy/reset-knowledge-data.sh  -> compose 在上一级
#   离线部署包：<pkg>/deploy/reset-knowledge-data.sh   -> compose 也在上一级
#   若有人把脚本直接放到包根，则当前目录就有 compose。
_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$_SCRIPT_DIR/../docker-compose.yml" ]; then
  cd "$_SCRIPT_DIR/.."
elif [ -f "$_SCRIPT_DIR/docker-compose.yml" ]; then
  cd "$_SCRIPT_DIR"
else
  echo "✗ 未找到 docker-compose.yml（已在 $_SCRIPT_DIR 及其上级查找）。" >&2
  echo "  请在项目根目录或离线部署包根目录下执行本脚本。" >&2
  exit 1
fi

COMPOSE_FILE="docker-compose.yml"
BACKEND_SERVICE="backend"
WORKER_SERVICE="worker"
BACKUP_DIR="./backups"

# install.sh 在开发仓库位于 deploy/，在离线包被 build.sh 平铺到包根。
if [ -f "./install.sh" ]; then
  INSTALL_SH="./install.sh"
else
  INSTALL_SH="deploy/install.sh"
fi

DRY_RUN=0
SKIP_BACKUP=0
ASSUME_YES=0
PURGE_ARGS=()

for arg in "$@"; do
  case "$arg" in
    --dry-run)       DRY_RUN=1; PURGE_ARGS+=("--dry-run") ;;
    --skip-backup)   SKIP_BACKUP=1 ;;
    # --yes 单独记标志位：下面要据此决定 compose exec 是否分配 TTY，
    # 不能靠事后 grep 数组（空数组的展开行为不可靠，见 run_purge 注释）。
    --yes)           ASSUME_YES=1; PURGE_ARGS+=("--yes") ;;
    --include-kbs|--include-chats|--keep-objects)
                     PURGE_ARGS+=("$arg") ;;
    --scope=*)       PURGE_ARGS+=("--scope" "${arg#*=}") ;;
    -h|--help)       sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "✗ 未知参数: ${arg}（用 --help 查看用法）" >&2; exit 1 ;;
  esac
done

# ------------------------------------------------------------
# Compose 命令探测（与 install.sh 一致：优先 V1 二进制，回退 V2 插件）
# ------------------------------------------------------------
if command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD="docker-compose"
elif docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
else
  echo "✗ 未找到 Docker Compose（既无 docker-compose 也无 docker compose 插件）。" >&2
  exit 1
fi

compose() { $COMPOSE_CMD -f "$COMPOSE_FILE" "$@"; }

# 在 backend 容器内执行清除脚本。
#
# ★ 这里必须按数组长度分支，不能写 "${PURGE_ARGS[@]:-}"：
#   bash 的 `:-` 作用在「整个展开结果」上，空数组会被它补成**一个空字符串参数**，
#   于是 argparse 收到一个空的位置参数并报 `unrecognized arguments: `。
#   （不带任何可选参数直接跑本脚本时必然触发。）
run_purge() {
  local -a tty_flags=("$@")   # 传 -T 表示不分配 TTY
  if [ "${#PURGE_ARGS[@]}" -eq 0 ]; then
    compose exec "${tty_flags[@]}" "$BACKEND_SERVICE" python -m scripts.purge_data
  else
    compose exec "${tty_flags[@]}" "$BACKEND_SERVICE" python -m scripts.purge_data "${PURGE_ARGS[@]}"
  fi
}

log()  { echo -e "\033[36m[reset]\033[0m $*"; }
warn() { echo -e "\033[33m[reset]\033[0m $*"; }
err()  { echo -e "\033[31m[reset]\033[0m $*" >&2; }

# ------------------------------------------------------------
# 1. 前置检查
# ------------------------------------------------------------
log "使用 Compose 命令: $COMPOSE_CMD"

if [ ! -f "$COMPOSE_FILE" ]; then
  err "未找到 ${COMPOSE_FILE}，请在项目根目录（或 deploy/ 的上一级）执行本脚本。"
  exit 1
fi

# backend 容器必须存在——purge 脚本要在容器内跑，才能复用容器里的 .env 与依赖
if ! compose ps --services --filter status=running 2>/dev/null | grep -qx "$BACKEND_SERVICE"; then
  warn "backend 容器未在运行，尝试启动（清除脚本需要在容器内执行）..."
  compose up -d "$BACKEND_SERVICE"
  sleep 5
fi

# 校验镜像里已经是新版代码：新拓扑脚本必须存在，否则会清了数据但建不出新表
if ! compose exec -T "$BACKEND_SERVICE" test -f scripts/purge_data.py 2>/dev/null; then
  err "backend 容器内没有 scripts/purge_data.py。"
  err "说明容器镜像还是旧版本（新拓扑需要镜像里带 scripts/ 目录）。"
  err "请先更新镜像再执行本脚本："
  err "    bash $INSTALL_SH update backend"
  exit 1
fi
log "前置检查通过（容器内已是含新拓扑的版本）"

# ------------------------------------------------------------
# 2. 备份 PostgreSQL
# ------------------------------------------------------------
if [ "$DRY_RUN" -eq 0 ] && [ "$SKIP_BACKUP" -eq 0 ]; then
  mkdir -p "$BACKUP_DIR"
  STAMP="$(date +%Y%m%d-%H%M%S)"
  DUMP_FILE="$BACKUP_DIR/artoo-pg-$STAMP.sql.gz"
  log "备份 PostgreSQL 到 $DUMP_FILE ..."
  # postgres 容器内的库名/用户从 compose 环境变量取，默认 postgres/artoo
  PG_USER="$(compose exec -T postgres printenv POSTGRES_USER 2>/dev/null | tr -d '\r' || echo postgres)"
  PG_DB="$(compose exec -T postgres printenv POSTGRES_DB 2>/dev/null | tr -d '\r' || echo artoo)"
  if compose exec -T postgres pg_dump -U "${PG_USER:-postgres}" -d "${PG_DB:-artoo}" \
      | gzip > "$DUMP_FILE"; then
    log "备份完成：$(du -h "$DUMP_FILE" | cut -f1)"
  else
    err "PostgreSQL 备份失败，已中止（可加 --skip-backup 强制跳过，但不推荐）。"
    rm -f "$DUMP_FILE"
    exit 1
  fi
  warn "注意：Milvus 向量与对象存储原件**不在**本次备份范围内，清除后无法恢复。"
else
  [ "$DRY_RUN" -eq 0 ] && warn "已跳过 PostgreSQL 备份（--skip-backup）"
fi

# ------------------------------------------------------------
# 3. 停止应用（dry-run 不停）
# ------------------------------------------------------------
if [ "$DRY_RUN" -eq 0 ]; then
  log "停止 worker（避免清除期间继续写入）..."
  compose stop "$WORKER_SERVICE" || warn "worker 停止失败（可能本就未运行）"
  # backend 需要保持运行，因为 purge 脚本要在它的容器内执行；
  # 但先把它的 API 流量断掉不现实，故依赖 purge 脚本内的确认闸门 + 运维在维护窗口执行。
  warn "backend 保持运行以承载清除脚本；请确保当前处于维护窗口、无用户在上传。"
fi

# ------------------------------------------------------------
# 4. 执行清除
# ------------------------------------------------------------
ARGS_DISPLAY=""
[ "${#PURGE_ARGS[@]}" -gt 0 ] && ARGS_DISPLAY="${PURGE_ARGS[*]}"
log "执行清除脚本：python -m scripts.purge_data ${ARGS_DISPLAY}"
# dry-run 或 --yes 时脚本不读 stdin，用 -T 关闭 TTY 分配（避免非交互环境报错）；
# 否则保留 TTY，让 purge_data 内部的 PURGE 二次确认能读到输入。
if [ "$DRY_RUN" -eq 1 ] || [ "$ASSUME_YES" -eq 1 ]; then
  run_purge -T
else
  run_purge
fi

if [ "$DRY_RUN" -eq 1 ]; then
  log "dry-run 结束，未做任何修改。确认无误后去掉 --dry-run 重跑。"
  exit 0
fi

# ------------------------------------------------------------
# 5. 重启应用
# ------------------------------------------------------------
log "重启 backend / worker（启动时会按新配置幂等建表）..."
compose restart "$BACKEND_SERVICE" || compose up -d "$BACKEND_SERVICE"
compose up -d "$WORKER_SERVICE"
sleep 8

# ------------------------------------------------------------
# 6. 复核拓扑
# ------------------------------------------------------------
log "清除后的 Milvus 拓扑："
compose exec -T "$BACKEND_SERVICE" python -m scripts.init_milvus --describe || \
  warn "拓扑复核失败，请手动执行：compose exec backend python -m scripts.init_milvus --describe"

log "完成。现在可以登录前端重新上传文档，向量会写入新拓扑。"
log "如需回滚 PostgreSQL：gunzip -c $BACKUP_DIR/artoo-pg-*.sql.gz | \\"
log "    $COMPOSE_CMD -f $COMPOSE_FILE exec -T postgres psql -U postgres -d artoo"
