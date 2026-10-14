# Artoo 部署交付手册（实施人员版）

本手册面向实施人员，区分「首次部署」和「部署更新」两种场景。所有命令在目标 Linux 服务器上执行，可直接复制。更完整的配置项说明与日常运维命令见包内 `DEPLOY.md`。

---

## 一、选择正确的包

交付目录 `dist/` 下共有四个独立包，先在目标服务器上确认 CPU 架构，再使用对应架构的包：

```bash
uname -m
# x86_64  → 使用 amd64 包
# aarch64 → 使用 arm64 包
```

| 目录 | 用途 | 内容 |
| --- | --- | --- |
| `artoo-deploy-amd64/` | 首次部署完整包（x86_64） | 应用镜像 + 中间件镜像 + 编排文件 + 部署脚本 |
| `artoo-deploy-arm64/` | 首次部署完整包（ARM64） | 同上，架构为 arm64 |
| `artoo-update-amd64/` | 部署更新包（x86_64） | 仅应用镜像 + 编排文件 + 部署脚本，**不含中间件** |
| `artoo-update-arm64/` | 部署更新包（ARM64） | 同上，架构为 arm64 |

每个包内都有 `SHA256SUMS`，上传到服务器后先校验完整性：

```bash
cd <包目录>
sha256sum -c SHA256SUMS
```

注意：

- **首次部署**只能使用完整包（`artoo-deploy-*`）。更新包没有中间件镜像，无法在空白服务器上启动。
- **部署更新**使用更新包（`artoo-update-*`），前提是目标服务器已经用完整包部署过、`.env` 和数据卷都还在。
- 架构不能混用，`uname -m` 结果与包名不匹配时请重新取包。

环境要求：Linux（x86_64 / arm64）、Docker Engine 20.10+、Docker Compose V1 或 V2、磁盘 100G 以上。详见 `DEPLOY.md` 第一节。

---

## 二、首次部署（完整包）

以下假设把完整包上传到 `/opt/artoo` 并解压/就位，实际路径可替换。

### 1. 校验并准备配置

```bash
cd /opt/artoo
sha256sum -c SHA256SUMS

# 生成 .env 并编辑必填项
cp .env.example .env
vi .env
```

`.env` 中生产环境必须修改两项：

```bash
JWT_SECRET=<随机长字符串，可用: python3 -c "import secrets; print(secrets.token_urlsafe(48))">
SUPER_ADMIN_PASSWORD=<初始超管密码，如 Admin@xxxxxx>
```

端口、数据库密码、模型服务地址等其余配置项说明见 `DEPLOY.md` 第四节。

### 2. 一键部署

```bash
bash install.sh
```

脚本会自动：加载全部镜像 → 校验 `.env` → 启动中间件（etcd/minio/milvus/postgres/redis，等待 healthy）→ 启动应用（backend/worker/frontend）。

### 3. 部署后验证

```bash
bash install.sh status                 # 全部服务应为 Up / healthy
curl http://127.0.0.1:8000/           # 后端健康检查，应返回 JSON
curl -I http://127.0.0.1:8888         # 前端应返回 200
```

然后浏览器访问 `http://<服务器IP>:8888`，用 `.env` 中的 `SUPER_ADMIN_USERNAME` / `SUPER_ADMIN_PASSWORD` 登录成功即部署完成。

如需启用知识图谱，按 `DEPLOY.md` 第九节操作（需要开发侧提供 `--with-graph` 构建的包）。

---

## 三、部署更新（更新包）

前提：目标服务器已完成首次部署，服务目录（下文以 `/opt/artoo` 为例）内 `.env` 保留完整。更新只替换应用镜像并重建应用容器，中间件与数据卷不受影响；过程中应用会短暂中断，建议在低峰期执行。

### 1. 上传更新包并校验

把与服务器架构匹配的更新包（如 `artoo-update-amd64/`）上传到服务器，例如 `/tmp/artoo-update`：

```bash
cd /tmp/artoo-update
sha256sum -c SHA256SUMS
```

### 2. 备份并同步文件到部署目录

```bash
cd /opt/artoo

# 备份当前配置与脚本
cp .env /tmp/artoo.env.bak.$(date +%Y%m%d%H%M%S)

# 同步更新包内容（注意：不要动服务器上的 .env）
cp -f /tmp/artoo-update/app-images.tar .
cp -f /tmp/artoo-update/docker-compose.yml .
cp -f /tmp/artoo-update/install.sh .
cp -f /tmp/artoo-update/.env.example .
cp -f /tmp/artoo-update/DEPLOY.md .
cp -f /tmp/artoo-update/DELIVERY.md .
cp -f /tmp/artoo-update/frontend/public/config.js frontend/public/config.js
cp -f /tmp/artoo-update/deploy/milvus-user.yaml deploy/
cp -f /tmp/artoo-update/deploy/reset-knowledge-data.sh deploy/
```

### 3. 执行更新

```bash
bash install.sh update
```

脚本会加载新镜像 → 强制重建 backend/worker/frontend 容器 → 自动清理被顶替的旧应用镜像。

### 4. 更新后验证

```bash
bash install.sh status
curl http://127.0.0.1:8000/
curl -I http://127.0.0.1:8888
bash install.sh logs backend 100      # 确认无报错后 Ctrl+C 退出
```

回滚方式：更新前保留上一版的完整包或更新包；若新版本异常，用上一版包的 `app-images.tar` 按上述「同步文件 → `bash install.sh update`」流程重新执行即可回退。

---

## 四、常见问题速查

| 现象 | 处理 |
| --- | --- |
| 容器反复重启且日志报 exec format error | 架构不匹配，确认 `uname -m` 与包架构一致，换对应架构的包 |
| `sha256sum -c` 校验失败 | 传输损坏，重新上传该包 |
| 提示未找到 Docker Compose | 安装 Docker Compose（V1/V2 均可）后重试 |
| 中间件长时间未 healthy | `docker ps --filter name=arag-` 查看状态，Milvus 依赖 etcd/minio 就绪，可等待或查 `bash install.sh logs milvus 200` |
| 更新后页面异常 | 浏览器强刷（config.js 有缓存）；仍异常时 `bash install.sh logs frontend 100` 排查 |

其余排查项见 `DEPLOY.md` 第十节。
