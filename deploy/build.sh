#!/usr/bin/env bash
# ============================================================
# Artoo 离线部署包构建（在有网的开发机执行）
#
# 产物 dist/ 目录可整体拷到内网服务器，再执行 install.sh 部署。
#
# 用法：
#   deploy/build.sh                  # 当前架构，含中间件镜像（首次部署）
#   deploy/build.sh --arch arm64     # 指定目标架构（amd64 | arm64）
#   deploy/build.sh --app-only       # 只打应用镜像（迭代更新，不含中间件）
#   deploy/build.sh --with-graph     # 额外安装知识图谱依赖（Neo4j 驱动）并导出 Neo4j 镜像
#   deploy/build.sh --out <目录>     # 指定输出目录（默认 dist），双架构交付时各建一个目录
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."   # 切到项目根目录

ARCH=""           # 留空 = 跟随本机架构
APP_ONLY=false
WITH_GRAPH=false  # 是否在后端镜像内安装知识图谱依赖（Neo4j 驱动）。--with-graph 可开启
OUT="dist"        # 输出目录，可用 --out 覆盖（如 --out dist/artoo-deploy-amd64）
# 中间件镜像拉取代理（可选）。Docker Hub 直连受限的网络环境可设置，
# 如：ARTOO_PULL_MIRROR=docker.m.daocloud.io（支持 docker.io 与 quay.io 路径代理）。
PULL_MIRROR="${ARTOO_PULL_MIRROR:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --arch) ARCH="$2"; shift 2 ;;
    --app-only) APP_ONLY=true; shift ;;
    --with-graph) WITH_GRAPH=true; shift ;;
    --out) OUT="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

[[ -n "$OUT" ]] || { echo "错误: --out 不能为空"; exit 1; }
mkdir -p "$OUT"

PLATFORM_ARG=""
SAVE_PLATFORM_ARG=""   # 跨架构 save 必须指定平台，否则 containerd 存储会找不到其它架构 manifest 而报错
[[ -n "$ARCH" ]] && PLATFORM_ARG="--platform linux/${ARCH}" && SAVE_PLATFORM_ARG="--platform linux/${ARCH}"

echo "=== Artoo 离线包构建 (${ARCH:-本机架构}) ==="

# 经代理拉取后重打标准 tag，后续 docker save 仍按原始镜像名导出。
pull_image() {
  local img="$1"
  if [[ -n "$PULL_MIRROR" ]]; then
    if docker pull $PLATFORM_ARG "$PULL_MIRROR/$img"; then
      docker tag "$PULL_MIRROR/$img" "$img"
      return 0
    fi
    echo "  镜像源拉取失败(${PULL_MIRROR}/${img}), 回退直连..."
  fi
  docker pull $PLATFORM_ARG "$img"
}

echo "[1/4] 构建应用镜像..."
GRAPH_BUILD_ARG=""
[[ "$WITH_GRAPH" == true ]] && GRAPH_BUILD_ARG="--build-arg WITH_GRAPH=true" && echo "  （含知识图谱依赖：Neo4j 驱动）"
docker build $PLATFORM_ARG $GRAPH_BUILD_ARG -t artoo-backend:latest backend/
docker build $PLATFORM_ARG -t artoo-frontend:latest frontend/

echo "[2/4] 导出应用镜像..."
docker save $SAVE_PLATFORM_ARG artoo-backend:latest artoo-frontend:latest -o "$OUT/app-images.tar"

if [[ "$APP_ONLY" == false ]]; then
  echo "[3/4] 拉取并导出中间件镜像..."
  INFRA_IMAGES=(
    postgres:16-alpine
    redis:7-alpine
    milvusdb/milvus:v2.5.4
    quay.io/coreos/etcd:v3.5.25
    minio/minio:RELEASE.2024-05-28T17-19-04Z
  )
  # 开图谱时额外导出 Neo4j 镜像（与 docker-compose.yml graph profile 一致）。
  [[ "$WITH_GRAPH" == true ]] && INFRA_IMAGES+=(neo4j:5-community)
  for img in "${INFRA_IMAGES[@]}"; do
    # containerd 镜像存储只保留“实际 pull 过的架构”的层。若本地残留的是其它架构副本，
    # 后续 docker save --platform 会因找不到目标架构的导出目标而报
    # “no suitable export target found ... does not provide the specified platform”。
    # 因此跨架构打包前先删除架构不符的本地副本，确保 pull 抓取到目标架构的层。
    if [[ -n "$ARCH" ]]; then
      local_arch=$(docker image inspect "$img" --format '{{.Architecture}}' 2>/dev/null || true)
      if [[ -n "$local_arch" && "$local_arch" != "$ARCH" ]]; then
        echo "  本地 ${img} 为 ${local_arch}，与目标 ${ARCH} 不符，删除后重新拉取..."
        docker rmi "$img" >/dev/null 2>&1 || true
      fi
    fi
    pull_image "$img"
  done
  docker save $SAVE_PLATFORM_ARG "${INFRA_IMAGES[@]}" -o "$OUT/infra-images.tar"
else
  echo "[3/4] 跳过中间件镜像（--app-only）"
fi

echo "[4/4] 复制部署文件..."
cp docker-compose.yml "$OUT/"
cp .env.example "$OUT/"
mkdir -p "$OUT/deploy"
cp deploy/milvus-user.yaml "$OUT/deploy/"
cp deploy/install.sh "$OUT/"
chmod +x "$OUT/install.sh"
# 首次部署 / 更新部署交付手册：随包交付，实施人员按场景直接照做。
cp deploy/DELIVERY.md "$OUT/"
# Milvus 拓扑切换用的数据清除脚本：必须随包交付，否则运维在服务器上无脚本可执行。
# 放在 deploy/ 下（脚本自身会向上一级找 docker-compose.yml，两种布局都兼容）。
cp deploy/reset-knowledge-data.sh "$OUT/deploy/"
chmod +x "$OUT/deploy/reset-knowledge-data.sh"
# 运维部署手册：随包交付，运维在服务器上可直接查阅
cp deploy/DEPLOY.md "$OUT/"
# 前端运行时配置：compose 把 ./frontend/public/config.js 只读挂载进容器覆盖镜像内默认。
# 离线包必须带上此文件，否则宿主路径不存在时 Docker 会按目录创建，导致挂载失败
# （Are you trying to mount a directory onto a file）。
mkdir -p "$OUT/frontend/public"
cp frontend/public/config.js "$OUT/frontend/public/config.js"

echo "[5/5] 生成镜像校验和..."
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$OUT" && sha256sum ./*.tar > SHA256SUMS)
else
  (cd "$OUT" && shasum -a 256 ./*.tar > SHA256SUMS)
fi

echo ""
echo "=== 完成 ==="
du -sh "$OUT"
echo "将 $OUT/ 整体拷到服务器（校验 SHA256SUMS 后），执行 ./install.sh"
