# Artoo 离线部署运维手册

面向运维人员的生产 / 内网离线部署说明。开发侧已在有网机器上构建好离线包（`dist/` 目录），本手册涵盖从拿到离线包到部署、验证、日常运维的完整流程。

---

## 一、环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Linux（x86_64 / arm64，与离线包架构一致） |
| Docker Engine | 20.10 及以上 |
| Docker Compose | V1（`docker-compose`）或 V2（`docker compose`）任一即可，脚本自动探测 |
| CPU / 内存 | 建议 8 核 16G 起步（Milvus + Neo4j 内存占用较高，开图谱建议 32G） |
| 磁盘 | 建议 100G 以上（镜像 + 向量库 + 上传文件 + 日志） |

> 架构必须匹配：`arm64` 服务器只能部署 `arm64` 架构的离线包，反之亦然。拿包前先与开发确认目标架构。

---

## 二、交付物说明

开发交付的 `dist/` 目录结构如下：

```
dist/
├── app-images.tar          # 应用镜像（backend + frontend）
├── infra-images.tar        # 中间件镜像（首次完整包才有；--app-only 更新包无此文件）
├── docker-compose.yml      # 编排文件（唯一真源）
├── .env.example            # 配置模板（首次运行自动复制为 .env）
├── install.sh              # 部署 / 运维一体化脚本
├── deploy/
│   └── milvus-user.yaml    # Milvus mmap 调优配置
└── frontend/public/config.js  # 前端运行时配置（挂载覆盖，改完刷新即生效）
```

将整个 `dist/` 目录拷贝到目标服务器任意路径（如 `/opt/artoo`），后续所有命令均在该目录内执行。

---

## 三、首次部署

进入 `dist/` 目录，执行：

```bash
cd /opt/artoo        # 你拷贝 dist/ 的实际路径
bash install.sh
```

脚本会按以下步骤自动执行：

1. **加载镜像** — 加载目录下所有 `*.tar` 镜像包
2. **检查配置** — 首次运行从 `.env.example` 生成 `.env`，并校验必填项
3. **兼容处理** — 自动适配 Compose V1/V2，处理网络冲突
4. **启动中间件** — 启动 etcd/minio/milvus/postgres/redis，等待全部 healthy（最多 150 秒）
5. **启动应用** — 启动 backend/worker/frontend

部署完成后脚本会打印访问地址：

```
前端: http://<服务器IP>:8888
后端: http://<服务器IP>:8000
```

### 关于必填配置

`.env.example` 已内置示例默认值，可直接启动。但**生产环境务必修改以下两项后重新部署**：

| 配置项 | 说明 | 生成方式 |
| --- | --- | --- |
| `JWT_SECRET` | JWT 签名密钥 | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `SUPER_ADMIN_PASSWORD` | 初始超级管理员密码 | 自定义强密码，如 `Admin@xxxxxx` |

修改方式：

```bash
vi .env          # 编辑配置
bash install.sh  # 重新执行部署
```

若必填项为空，脚本会 fail-fast 并明确提示缺失项，不会带着空配置启动。

---

## 四、配置项说明（.env）

以下为运维常关注的配置，完整项见 `.env.example` 内注释。

### 端口与进程

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `FRONTEND_PORT` | 8888 | 前端访问端口 |
| `BACKEND_PORT` | 8000 | 后端 API 端口 |
| `BACKEND_WORKERS` | 2 | 后端进程数，单机约 50 人建议 2，最多 2~4 |
| `POSTGRES_PASSWORD` | postgres | 数据库密码，生产建议修改 |
| `TZ` | Asia/Shanghai | 时区，影响日志切分与时间显示 |

### 认证（必填）

| 配置项 | 说明 |
| --- | --- |
| `JWT_SECRET` | JWT 签名密钥，缺失则启动失败 |
| `SUPER_ADMIN_USERNAME` | 初始超管用户名，默认 `superadmin` |
| `SUPER_ADMIN_PASSWORD` | 初始超管密码，强制首次登录改密 |
| `REGISTRATION_MODE` | 注册模式：`invite_only`（默认，仅邀请）/ `self_serve`（开放注册） |

### 模型服务（选填，也可部署后在前端「模型管理」页配置）

- `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` — 大模型服务
- `EMBED_BASE_URL` / `EMBED_MODEL` — Embedding 服务（地址填到 `/v1`）
- `RERANK_BASE_URL` / `RERANK_MODEL` — Rerank 服务

> 模型地址不填也能启动，登录后可在前端界面配置。

### 性能调优（按硬件调整，不配用默认值）

关键项：`PIPELINE_MAX_CONCURRENT`（文档并发，GPU 推 3 / CPU 推 1-2）、`PIPELINE_EMBED_CONCURRENCY`（Embedding 并发，远程服务报 429 时调小）。其余见 `.env.example` 注释。

---

## 五、日常运维命令

所有命令在 `dist/` 目录内执行。`start/stop/restart/update` 末尾可加服务名，只操作单个服务。

| 命令 | 说明 |
| --- | --- |
| `bash install.sh status` | 查看所有服务状态 |
| `bash install.sh logs [服务名] [条数]` | 查看日志（默认应用日志 100 条，实时跟踪，Ctrl+C 退出） |
| `bash install.sh restart [服务名]` | 重启应用 / 指定服务（不重建、不加载镜像） |
| `bash install.sh start [服务名]` | 启动全部 / 指定服务 |
| `bash install.sh stop [服务名]` | 停止全部 / 指定服务（**数据保留**） |
| `bash install.sh down` | 停止并删除容器（**数据卷保留**） |
| `bash install.sh down-all` | 停止并删除容器 + 数据卷（⚠️ **清除所有数据**，需二次确认） |

可用服务名：`migrate` / `backend` / `worker` / `frontend` / `postgres` / `milvus` / `redis` / `etcd` / `minio` / `neo4j`

示例：

```bash
bash install.sh logs backend 200     # 看后端最近 200 条日志
bash install.sh restart backend      # 先停 Worker、执行迁移、重启后端、再恢复 Worker
bash install.sh status               # 查看服务健康状态
```

---

## 六、应用更新升级

开发提供**仅应用镜像的更新包**（`app-images.tar`，通过 `make build-app` 构建，不含中间件）。更新流程：

1. 用新的 `app-images.tar` 替换 `dist/` 目录内的旧文件
2. 执行更新命令：

```bash
bash install.sh update            # 更新全部应用
bash install.sh update backend    # 只更新后端
```

`update` 会加载新镜像 → 停止 Worker → 执行一次性数据库迁移 → 重建应用容器 →
恢复 Worker，并默认保留被顶替的旧镜像。迁移失败时不会启动新 Backend/Worker。
中间件不受影响，数据完整保留。

更新前会自动为当前 backend/frontend 打 `rollback-pre-update-<时间>` 标签，旧镜像默认保留用于回滚；磁盘空间紧张时显式执行
`CLEAN_OLD_IMAGES=1 bash install.sh update` 才会清理被顶替的镜像。

> 更新只动 `migrate/backend/worker/frontend`（`migrate` 复用 backend 镜像），不重建中间件，无数据丢失风险。

---

## 七、数据与备份

所有持久化数据存放在 Docker 命名卷中，`down`（不带 `-v`）不会删除：

| 数据卷 | 内容 |
| --- | --- |
| `postgres_data` | 业务数据库（用户、知识库元数据等） |
| `milvus_data` | 向量库 |
| `minio_data` | 对象存储（Milvus 后端 + 知识库源文件） |
| `upload_data` | 上传文档 |
| `etcd_data` / `redis_data` | Milvus 元数据 / 缓存队列 |
| `neo4j_data` | 知识图谱数据（仅开图谱时） |

备份建议：定期备份 `postgres_data`、`milvus_data`、`minio_data`、`upload_data` 四个卷。可用 `docker run --rm -v <卷名>:/data -v $(pwd):/backup alpine tar czf /backup/<卷名>.tar.gz -C /data .` 导出。

> ⚠️ `bash install.sh down-all` 会删除全部数据卷，属不可逆操作，执行前务必确认已备份。

---

## 八、端口清单

对外暴露端口（可在 `.env` 调整前两个）：

| 端口 | 服务 | 说明 |
| --- | --- | --- |
| 8888 | frontend | 前端 Web 访问（`FRONTEND_PORT`） |
| 8000 | backend | 后端 API（`BACKEND_PORT`） |
| 7474 | neo4j | 图谱浏览器管理台（仅开图谱时） |
| 7687 | neo4j | 图谱 Bolt 端口（仅开图谱时） |

中间件（etcd/minio/milvus/postgres/redis）在生产编排下**不对外暴露端口**，仅走容器内网 `arag-network` 互通。

---

## 九、可选：启用知识图谱

知识图谱默认关闭，不开零成本。启用需满足两个条件：

1. **离线包需含图谱支持** — 开发打包时须带 `--with-graph`（应用镜像内装 Neo4j 驱动，离线包额外导出 `neo4j:5-community` 镜像）。若拿到的是普通包，需向开发索要图谱版离线包。
2. **在 `.env` 设开关**：

```bash
GRAPH_ENABLE=true
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<自定义强密码>
```

改完后重新执行 `bash install.sh`。脚本读取到 `GRAPH_ENABLE=true` 会自动把 Neo4j 一并纳入启动，无需额外命令。

> `NEO4J_URI` 由 compose 固定注入（`bolt://neo4j:7687`），无需配置。

---

## 十、常见排查

| 现象 | 排查方向 |
| --- | --- |
| 脚本报「未找到 Docker Compose」 | 服务器未装 Docker Compose，先安装 |
| 部分中间件未就绪 | `docker ps --filter name=arag-` 查看容器状态；Milvus 依赖 etcd/minio healthy 才启动，耐心等待 |
| 应用启动失败 | 先看 `bash install.sh logs migrate`，再看 backend；迁移失败时 Backend/Worker 不会启动 |
| 启动卡在数据库迁移 | 检查是否有 Worker 长事务；新版迁移有 5 秒 `lock_timeout`，会快速失败而不是无限等待 |
| 前端能开、问答报错 | 检查前端「模型管理」是否已配置 LLM / Embedding / Rerank 服务地址 |
| 改了 `config.js` 不生效 | 该文件挂载覆盖，改完刷新浏览器即可，无需重建 |
| 架构不匹配无法启动 | 确认离线包架构与服务器 CPU 架构一致（amd64 / arm64） |

---

## 附：手动等价命令

`install.sh` 首次部署等价于（在 `dist/` 内）：

```bash
docker compose -f docker-compose.yml up -d etcd minio milvus postgres redis   # 起中间件，等 healthy
docker compose -f docker-compose.yml up -d migrate                            # 一次性迁移，必须成功退出
docker compose -f docker-compose.yml up -d backend worker frontend            # 起应用
```

> 生产脚本按显式服务名启动（不依赖 profiles），以兼容 Compose V1/V2。
