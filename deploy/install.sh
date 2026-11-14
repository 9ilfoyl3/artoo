#!/usr/bin/env bash
# ============================================================
# Artoo 服务管理脚本（兼容 Docker Compose V1 与 V2）
#
# 自动探测可用的 Compose 命令：优先 docker-compose（V1 独立二进制），
# 回退 docker compose（V2 插件）。两者皆无则报错退出。
#
# 不依赖 compose profiles：统一按「显式服务名」启动，对 V1/V2 行为一致，
# 同时规避新版 V2 对跨 profile depends_on 的 "undefined service" 校验报错。
#
# 用法：
#   bash install.sh              # 首次部署（加载镜像 + 启动全部）
#   bash install.sh start [服务] # 启动全部 / 指定服务
#   bash install.sh stop [服务]  # 停止全部 / 指定服务（保留数据）
#   bash install.sh restart [服务] # 重启应用 / 指定服务（不重建、不加载镜像）
#   bash install.sh update [服务]  # 更新应用 / 指定服务（加载新镜像 + 重建容器）
#   bash install.sh down         # 停止并删除容器（数据卷保留）
#   bash install.sh down-all     # 停止并删除容器 + 数据卷（⚠️ 清除所有数据）
#   bash install.sh logs [服务名] [条数]  # 查看日志（默认全部应用，100条）
#   bash install.sh status       # 查看服务状态
#
# 服务名: migrate / backend / worker / frontend / postgres / milvus / redis / etcd / minio / neo4j
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

# 只补脚本执行位，不能用 chmod -R 755：那会让 .env 中的密码/密钥对所有人可读。
find . -maxdepth 1 -type f -name '*.sh' -exec chmod 755 {} + 2>/dev/null || true
[[ -f .env ]] && chmod 600 .env 2>/dev/null || true

COMPOSE_FILE="docker-compose.yml"
ACTION="${1:-install}"

# ============================================================
# Compose 命令探测：优先 V1 独立二进制（docker-compose），回退 V2 插件（docker compose）。
# 存进 COMPOSE_CMD 后，全脚本统一用 "$COMPOSE_CMD" 调用。
# ============================================================
if command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD="docker-compose"
elif docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
else
  echo "✗ 未找到 Docker Compose（既无 'docker-compose' 命令，也无 'docker compose' 插件）。" >&2
  echo "  请先安装 Docker Compose 后重试。" >&2
  exit 1
fi

# 中间件 / 应用服务清单（按显式服务名启动，不依赖 profiles）。
# 图谱开启时 neo4j 会被追加进中间件清单（见 resolve_infra_services）。
INFRA_SERVICES="etcd minio milvus postgres redis"
APP_SERVICES="backend worker frontend"
MIGRATE_SERVICE="migrate"
BACKEND_IMAGE="artoo-backend:latest"
FRONTEND_IMAGE="artoo-frontend:latest"

# ============================================================
# 公共函数
# ============================================================

# 读取 GRAPH_ENABLE 并据此决定是否把 neo4j 纳入中间件清单。
# 解析健壮处理：取首个匹配行、剥行内注释、去引号/空白、统一小写，缺省 false。
resolve_infra_services() {
  local graph_enable=""
  if [[ -f .env ]]; then
    graph_enable=$(grep -E '^GRAPH_ENABLE=' .env | head -n1 | cut -d= -f2- | sed 's/#.*//' | tr -d '[:space:]"'"'"'' | tr 'A-Z' 'a-z')
  fi
  graph_enable=${graph_enable:-false}
  if [[ "$graph_enable" == "true" ]]; then
    case " $INFRA_SERVICES " in
      *" neo4j "*) : ;;                       # 已含则不重复
      *) INFRA_SERVICES="$INFRA_SERVICES neo4j" ;;
    esac
    echo "  GRAPH_ENABLE=true：将一并启动 Neo4j（知识图谱）"
  fi
}

ensure_compose_compat() {
  local NETWORK_NAME="arag-network"
  # 统一用 awk 原地改写：GNU sed 与 BSD sed(macOS) 的 -i 与 \n 行为不同，awk 两端一致。
  # 1) 去掉 profiles 行（V1 不支持；V2 下按显式服务名启动也不需要）。
  # 2) 若 arag-network 已存在（复用外部网络）则标记 external，避免重复创建冲突；否则删除残留标记。
  local tmp_file="${COMPOSE_FILE}.tmp"
  if docker network inspect "$NETWORK_NAME" >/dev/null 2>&1; then
    if grep -q "external: true" "$COMPOSE_FILE"; then
      awk '/^[[:space:]]*profiles:/ { next } { print }' "$COMPOSE_FILE" > "$tmp_file"
    else
      awk -v net="$NETWORK_NAME" '
        /^[[:space:]]*profiles:/ { next }
        $0 ~ "^[[:space:]]*name: " net "[[:space:]]*$" && !ext_added {
          print
          print "    external: true"
          ext_added = 1
          next
        }
        { print }
      ' "$COMPOSE_FILE" > "$tmp_file"
    fi
  else
    awk '/^[[:space:]]*profiles:/ { next } /external: true/ { next } { print }' "$COMPOSE_FILE" > "$tmp_file"
  fi
  mv "$tmp_file" "$COMPOSE_FILE"
}

check_env() {
  if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "  已从 .env.example 生成 .env（含示例默认值，可直接启动）。"
    echo "  ⚠️ 生产环境建议修改 JWT_SECRET 与 SUPER_ADMIN_PASSWORD 后重新部署。"
  fi

  # 用 grep 直接从文件提取值（避免 source 解析注释或特殊字符的问题）
  _get_env_val() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' ; }

  local MISSING=()

  if [[ -z "$(_get_env_val JWT_SECRET)" ]]; then
    MISSING+=("JWT_SECRET（JWT 签名密钥，生成：python3 -c \"import secrets; print(secrets.token_urlsafe(48))\"）")
  fi
  if [[ -z "$(_get_env_val SUPER_ADMIN_USERNAME)" ]]; then
    MISSING+=("SUPER_ADMIN_USERNAME（初始超管用户名，如 admin）")
  fi
  if [[ -z "$(_get_env_val SUPER_ADMIN_PASSWORD)" ]]; then
    MISSING+=("SUPER_ADMIN_PASSWORD（初始超管密码，如 Admin@123456）")
  fi

  if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo ""
    echo "  ❌ 以下必填配置项为空，服务无法启动："
    for item in "${MISSING[@]}"; do
      echo "    - $item"
    done
    echo ""
    echo "  请编辑 .env 填写后重新执行："
    echo "    vi .env"
    echo ""
    echo "  选填项（不填也能启动，后续可在前端配置）："
    echo "    - LLM_BASE_URL / LLM_MODEL（大模型服务地址）"
    echo "    - EMBED_BASE_URL（Embedding 服务地址）"
    echo "    - RERANK_BASE_URL（Rerank 服务地址）"
    exit 1
  fi
  echo "  配置校验通过 ✓"
}

wait_infra_healthy() {
  echo "  等待中间件 healthy（最多 150 秒）..."
  for i in $(seq 1 30); do
    # 检查中间件容器是否都在运行（neo4j 仅在图谱开启时纳入匹配）
    local pattern='etcd|minio|milvus|postgres|redis'
    case " $INFRA_SERVICES " in *" neo4j "*) pattern="$pattern|neo4j" ;; esac
    NOT_RUNNING=$(docker ps --filter "name=arag-" --format '{{.Names}} {{.Status}}' | grep -E "$pattern" | grep -cvE 'Up' || true)
    if [[ "$NOT_RUNNING" -eq 0 ]]; then
      # 再检查有没有 health: starting 的
      STARTING=$(docker ps --filter "name=arag-" --format '{{.Status}}' | grep -ci 'health: starting' || true)
      if [[ "$STARTING" -eq 0 ]]; then
        echo "  中间件全部就绪 ✓"
        return 0
      fi
    fi
    echo "  等待中... ($i/30)"
    sleep 5
  done
  echo "  ⚠️ 部分中间件可能未就绪，请检查: docker ps --filter name=arag-"
}

# 在启动/重建 backend 或 worker 前执行一次性迁移。迁移服务带 advisory lock 和
# lock_timeout，不会让多个 uvicorn worker 并行 DDL 或无限等待 Worker 长事务。
run_migrations() {
  echo "=== 执行数据库迁移 ==="
  $COMPOSE_CMD -f "$COMPOSE_FILE" up -d --force-recreate "$MIGRATE_SERVICE"
  local container_id
  container_id=$($COMPOSE_CMD -f "$COMPOSE_FILE" ps -q "$MIGRATE_SERVICE")
  if [[ -z "$container_id" ]]; then
    echo "✗ 无法获取迁移容器 ID" >&2
    exit 1
  fi
  local exit_code
  exit_code=$(docker wait "$container_id")
  if [[ "$exit_code" != "0" ]]; then
    echo "✗ 数据库迁移失败（exit=$exit_code），应用不会启动。" >&2
    $COMPOSE_CMD -f "$COMPOSE_FILE" logs --tail=200 "$MIGRATE_SERVICE" >&2 || true
    exit 1
  fi
  echo "  ✓ 数据库迁移完成"
}

show_info() {
  local FRONTEND_PORT BACKEND_PORT IP
  FRONTEND_PORT=$(grep -E '^FRONTEND_PORT=' .env | cut -d= -f2 || true)
  BACKEND_PORT=$(grep -E '^BACKEND_PORT=' .env | cut -d= -f2 || true)
  IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "服务器IP")
  echo ""
  echo "  前端: http://${IP}:${FRONTEND_PORT:-8888}"
  echo "  后端: http://${IP}:${BACKEND_PORT:-8000}"
  echo ""
  echo "  常用命令（在本目录执行，末尾可加服务名指定单个服务）："
  echo "    查看状态: bash install.sh status"
  echo "    查看日志: bash install.sh logs [服务名] [条数]"
  echo "    迁移日志: bash install.sh logs migrate 200"
  echo "    重启应用: bash install.sh restart [服务名]   # 仅重启，不重建"
  echo "    更新应用: bash install.sh update [服务名]    # 替换 app-images.tar 后执行"
  echo "    停止服务: bash install.sh stop [服务名]"
  echo "    清理容器: bash install.sh down               # 数据卷保留"
}

# ============================================================
# 子命令
# ============================================================

do_install() {
  echo "=== Artoo 首次部署（Compose: ${COMPOSE_CMD}）==="

  echo "[1/5] 加载 Docker 镜像..."
  for f in *.tar; do
    [[ -f "$f" ]] || continue
    echo "  load $f"
    docker load -i "$f"
  done

  echo "[2/5] 检查配置..."
  check_env
  resolve_infra_services

  echo "[3/5] 处理 compose 文件兼容性..."
  ensure_compose_compat

  echo "[4/5] 启动中间件..."
  $COMPOSE_CMD -f "$COMPOSE_FILE" up -d $INFRA_SERVICES
  wait_infra_healthy

  echo "[5/5] 启动应用..."
  run_migrations
  $COMPOSE_CMD -f "$COMPOSE_FILE" up -d $APP_SERVICES

  echo ""
  echo "=== 部署完成 ==="
  show_info
}

do_start() {
  local SERVICE="${2:-}"
  resolve_infra_services
  ensure_compose_compat
  if [[ -n "$SERVICE" ]]; then
    echo "=== 启动服务: $SERVICE ==="
    if [[ "$SERVICE" == "backend" || "$SERVICE" == "worker" ]]; then
      run_migrations
    fi
    $COMPOSE_CMD -f "$COMPOSE_FILE" up -d "$SERVICE"
  else
    echo "=== 启动全部服务 ==="
    $COMPOSE_CMD -f "$COMPOSE_FILE" up -d $INFRA_SERVICES
    wait_infra_healthy
    run_migrations
    $COMPOSE_CMD -f "$COMPOSE_FILE" up -d $APP_SERVICES
  fi
  echo "=== 启动完成 ==="
  show_info
}

do_stop() {
  local SERVICE="${2:-}"
  if [[ -n "$SERVICE" ]]; then
    echo "=== 停止服务: ${SERVICE}（数据保留）==="
    $COMPOSE_CMD -f "$COMPOSE_FILE" stop "$SERVICE"
  else
    echo "=== 停止全部服务（数据保留）==="
    $COMPOSE_CMD -f "$COMPOSE_FILE" stop
  fi
  echo "=== 已停止 ==="
}

do_restart() {
  local SERVICE="${2:-}"
  if [[ -n "$SERVICE" ]]; then
    echo "=== 重启服务: $SERVICE(不重建容器、不加载镜像)==="
    case "$SERVICE" in
      backend)
        $COMPOSE_CMD -f "$COMPOSE_FILE" stop worker backend || true
        run_migrations
        $COMPOSE_CMD -f "$COMPOSE_FILE" up -d backend
        $COMPOSE_CMD -f "$COMPOSE_FILE" up -d worker
        ;;
      worker)
        $COMPOSE_CMD -f "$COMPOSE_FILE" stop worker || true
        run_migrations
        $COMPOSE_CMD -f "$COMPOSE_FILE" up -d worker
        ;;
      frontend)
        $COMPOSE_CMD -f "$COMPOSE_FILE" restart frontend
        ;;
      *)
        $COMPOSE_CMD -f "$COMPOSE_FILE" restart "$SERVICE"
        ;;
    esac
  else
    echo "=== 重启应用(不重建容器、不加载镜像)==="
    # 先停 Worker/Backend，迁移完成后再恢复，避免文档长事务或 API 查询阻塞 DDL。
    $COMPOSE_CMD -f "$COMPOSE_FILE" stop worker backend || true
    run_migrations
    $COMPOSE_CMD -f "$COMPOSE_FILE" up -d backend frontend
    $COMPOSE_CMD -f "$COMPOSE_FILE" up -d worker
  fi
  echo ""
  echo "=== 重启完成 ==="
  show_info
}

do_update() {
  local SERVICE="${2:-}"
  echo "=== 更新应用（加载新镜像 + 重建容器）==="

  # 记录加载前的应用镜像 ID（仅用于打印变化提示，不作为终止依据）
  local before_backend before_frontend
  before_backend=$(docker images -q "$BACKEND_IMAGE" 2>/dev/null)
  before_frontend=$(docker images -q "$FRONTEND_IMAGE" 2>/dev/null)
  local rollback_suffix="rollback-pre-update-$(date '+%Y%m%d-%H%M%S')"
  if [[ -n "$before_backend" ]]; then
    docker tag "$before_backend" "${BACKEND_IMAGE%:*}:$rollback_suffix"
    echo "  已保留 backend 回滚镜像: ${BACKEND_IMAGE%:*}:$rollback_suffix"
  fi
  if [[ -n "$before_frontend" ]]; then
    docker tag "$before_frontend" "${FRONTEND_IMAGE%:*}:$rollback_suffix"
    echo "  已保留 frontend 回滚镜像: ${FRONTEND_IMAGE%:*}:$rollback_suffix"
  fi

  echo "[1/3] 加载应用镜像..."
  local app_tar="${APP_IMAGE_ARCHIVE:-app-images.tar}"
  if [[ ! -f "$app_tar" ]]; then
    echo "  ❌ 当前目录没有 ${app_tar}，无法更新"
    exit 1
  fi
  local mtime_h
  mtime_h=$(date -d "@$(stat -c %Y "$app_tar" 2>/dev/null || echo 0)" "+%Y-%m-%d %H:%M:%S" 2>/dev/null || echo "未知")
  echo "  load ${app_tar}（修改于 ${mtime_h}）"
  docker load -i "$app_tar"

  # 镜像 ID 变化仅作信息提示：手动 docker load 过镜像等场景下 ID 可能不变，
  # 但 update 仍应照常重建容器，不再因“镜像未变化”终止。
  local after_backend after_frontend
  after_backend=$(docker images -q "$BACKEND_IMAGE" 2>/dev/null)
  after_frontend=$(docker images -q "$FRONTEND_IMAGE" 2>/dev/null)
  if [[ "$before_backend" == "$after_backend" && "$before_frontend" == "$after_frontend" ]]; then
    echo "  ℹ️ 镜像 ID 未变化（可能已手动 load 过），仍将继续重建容器。"
  else
    echo "  ✓ 镜像已更新："
    [[ "$before_backend" != "$after_backend" ]] && echo "    backend : ${before_backend:-无} → $after_backend"
    [[ "$before_frontend" != "$after_frontend" ]] && echo "    frontend: ${before_frontend:-无} → $after_frontend"
  fi

  echo "[2/3] 处理 compose 文件兼容性..."
  ensure_compose_compat

  # 仅 backend/worker 更新需要迁移。重建前先停 Worker；迁移成功后才允许其恢复，
  # 避免长期文档事务与 backend 启动 DDL 形成锁队列。
  if [[ -z "$SERVICE" || "$SERVICE" == "backend" || "$SERVICE" == "worker" ]]; then
    if [[ -z "$SERVICE" || "$SERVICE" == "backend" ]]; then
      $COMPOSE_CMD -f "$COMPOSE_FILE" stop worker backend || true
    else
      $COMPOSE_CMD -f "$COMPOSE_FILE" stop worker || true
    fi
    run_migrations
  fi

  if [[ -n "$SERVICE" ]]; then
    echo "[3/3] 重建容器: $SERVICE..."
    if [[ "$SERVICE" == "backend" ]]; then
      $COMPOSE_CMD -f "$COMPOSE_FILE" up -d --force-recreate backend
      $COMPOSE_CMD -f "$COMPOSE_FILE" up -d worker
    else
      $COMPOSE_CMD -f "$COMPOSE_FILE" up -d --force-recreate "$SERVICE"
    fi
  else
    echo "[3/3] 重建应用容器..."
    $COMPOSE_CMD -f "$COMPOSE_FILE" up -d --force-recreate $APP_SERVICES
  fi

  # 默认保留旧镜像，确保 rollback tag 和上一版可立即恢复。只有显式设置
  # CLEAN_OLD_IMAGES=1 时才删除本次被顶替的旧镜像。
  for old in "$before_backend" "$before_frontend"; do
    if [[ -n "$old" && "$old" != "$after_backend" && "$old" != "$after_frontend" ]]; then
      if [[ "${CLEAN_OLD_IMAGES:-0}" == "1" ]]; then
        if docker rmi "$old" >/dev/null 2>&1; then
          echo "  清理旧镜像 $old"
        fi
      else
        echo "  保留旧镜像供回滚: $old（如需清理设置 CLEAN_OLD_IMAGES=1）"
      fi
    fi
  done

  echo ""
  echo "=== 更新完成 ==="
  show_info
}

do_down() {
  echo "=== 停止并删除容器（数据卷保留）==="
  $COMPOSE_CMD -f "$COMPOSE_FILE" down
  echo "=== 已清理 ==="
}

do_down_all() {
  echo "⚠️  即将删除所有容器和数据卷（数据库、向量库、上传文件全部清除）"
  read -p "确认？(y/N): " confirm
  if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "已取消"
    exit 0
  fi
  $COMPOSE_CMD -f "$COMPOSE_FILE" down -v
  echo "=== 已清除所有数据 ==="
}

do_logs() {
  # 参数：$2=服务名（可选），$3=条数（可选，默认100）
  local SERVICE="${2:-}"
  local LINES="${3:-100}"

  if [[ -n "$SERVICE" ]]; then
    echo "=== 查看 $SERVICE 日志（最近 $LINES 条，实时跟踪）==="
    $COMPOSE_CMD -f "$COMPOSE_FILE" logs -f --tail="$LINES" "$SERVICE"
  else
    echo "=== 查看应用日志（最近 $LINES 条，实时跟踪）==="
    $COMPOSE_CMD -f "$COMPOSE_FILE" logs -f --tail="$LINES" $APP_SERVICES
  fi
}

do_status() {
  $COMPOSE_CMD -f "$COMPOSE_FILE" ps
}

# ============================================================
# 路由
# ============================================================

case "$ACTION" in
  install)   do_install ;;
  start)     do_start "$@" ;;
  stop)      do_stop "$@" ;;
  restart)   do_restart "$@" ;;
  update)    do_update "$@" ;;
  down)      do_down ;;
  down-all)  do_down_all ;;
  logs)      do_logs "$@" ;;
  status)    do_status ;;
  *)
    echo "未知命令: $ACTION"
    echo "可用命令: start | stop | restart | update | down | down-all | logs [服务] [条数] | status"
    echo "提示: start/stop/restart/update 可在末尾加服务名，如: bash install.sh restart backend"
    exit 1
    ;;
esac
