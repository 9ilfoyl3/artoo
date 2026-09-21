<div align="center">

# 🤖 Artoo — 让知识库自己去检索：ReAct Agent 驱动的 Agentic RAG 框架

**开源 · LLM 驱动 · 可私有化部署的智能知识库系统**

Artoo 以 ReAct Agent 为核心，让大模型自主编排关键词检索、语义检索、深度阅读、网页搜索与 MCP 工具，构建"先检索证据、再作答"的可追溯问答体验。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green.svg)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-61dafb.svg)](https://react.dev/)
[![Milvus](https://img.shields.io/badge/Milvus-2.4+-00a1ea.svg)](https://milvus.io/)

| **简体中文** | [English](./README_EN.md) |

</div>

<p align="center">
  <a href="#-项目介绍">项目介绍</a> •
  <a href="#%EF%B8%8F-架构设计">架构设计</a> •
  <a href="#-功能概览">功能概览</a> •
  <a href="#-界面预览">界面预览</a> •
  <a href="#-快速开始">快速开始</a> •
  <a href="#-文档">文档</a> •
  <a href="#-开发指南">开发指南</a>
</p>

---

## 📌 项目介绍

**Artoo** 是一款开源的、基于大语言模型（LLM）的 Agentic RAG 知识库框架，专为企业级文档理解与可追溯问答场景打造。

框架围绕三大核心能力构建：**ReAct Agent 智能推理**让大模型在 Think → Act → Observe 循环中自主决定检索策略与停止时机；**三路混合检索**通过稠密语义、稀疏向量与 BM25 全文检索并行召回，经 RRF 融合、Rerank 精排、MMR 去冗余与父块扩展输出高质量上下文，并可叠加事件中心知识图谱（GraphRAG）作为第四路召回：从每个 chunk 抽取「主谓宾 + 时地」齐全的事件作为一等检索单元，经事件向量召回与实体桥接双入口种子、多跳扩展、粗排回取关联 chunk，补回纯向量召不回的多跳关联内容；**结构感知文档处理**按文档逻辑结构切分、父子块映射，并对图文混排文档做并发 OCR。文档来源既支持本地多格式文件上传，也支持粘贴网页 / 微信公众号链接一键抓取正文转存入库。配合可视化的多模型管理、Embedding / Rerank / OCR 服务热切换、MCP 工具集成、Agent 技能（Skills）与三层递进式上下文管理，Artoo 把分散文档沉淀为可查询、可推理、可溯源的知识资产。

所有 AI 推理（LLM / Embedding / Rerank / OCR）均通过 HTTP 调用外部服务，后端轻量、易于私有化离线部署，数据完全自主可控。Agent 推理过程通过 EventBus 实时流式推送到前端，思考、工具调用、引用来源、上下文 Token 占用全程可视。

## ✨ 核心亮点

- **真正的 ReAct Agent** —— 大模型通过 function calling 自主调用工具、分析结果、决定继续检索还是提交答案，而非固定流水线编排。
- **Evidence-First 检索纪律** —— 内置 Progressive RAG 系统提示词（Assess-Reconnaissance-Plan-Execute 工作流），强制"先检索、深读 chunk、再作答"，杜绝凭记忆臆造。
- **三路混合检索 + 可选图谱增强** —— Dense + Sparse + BM25 并行召回，RRF 融合 + Rerank 精排 + 复合评分 + MMR 去冗余 + 父块扩展；开启知识图谱后，事件中心 GraphRAG 作为第四路并入 RRF，通过「事件向量召回 + 实体桥接」双入口取种子事件、多跳扩展、粗排后回取关联 chunk，胜任跨文档多跳问答，并提供力导向图交互浏览。
- **多源文档入库** —— 本地多格式文件上传之外，支持粘贴网页 / 微信公众号链接抓取正文转存为知识库文档，自动留存原文链接便于溯源。
- **三层递进式上下文管理** —— BPE Token 估算 + API Usage 增量追踪 + LLM 摘要合并 + 分组截断兜底，长对话不超窗。
- **可扩展工具生态** —— 内置知识检索、关键词匹配、深度阅读、附件阅读、网页搜索、思考、技能加载等工具，并支持接入远程 MCP Server。
- **多租户与权限治理** —— 固定角色（admin / member）+ 归属轴的 RBAC 模型，知识库私有 / 组织可见 + 点对点共享，超级管理员、邀请注册与审计日志，三类 API Key 凭据模型。
- **全程可视化** —— Agent 思考、工具调用、引用溯源、Token 占用通过 SSE 实时推送，前端逐 token 渲染。

## 🏗️ 架构设计

```
┌─────────────────────────────────────────────────────────────┐
│                          接入层                              │
│   Chat API (OpenAI 兼容 · SSE)  │  Admin API (RESTful)       │
│   MCP Server (对外暴露知识库能力)                            │
├─────────────────────────────────────────────────────────────┤
│                     ReAct Agent 引擎                         │
│   Think(流式LLM) → Analyze(终止判定) → Act(工具) → Observe   │
│   EventBus 事件总线  │  三层递进式上下文管理  │  Skills 技能  │
├─────────────────────────────────────────────────────────────┤
│                         工具层                               │
│  knowledge_search │ grep_chunks │ list_knowledge_chunks      │
│  read_attachment │ read_skill │ thinking │ web_search        │
│  final_answer │ MCP Tools                                    │
├─────────────────────────────────────────────────────────────┤
│                        检索工具层                            │
│   Dense + Sparse + BM25 (+ Graph) → RRF 融合 → Rerank → MMR  │
│                          → 父块扩展                          │
├─────────────────────────────────────────────────────────────┤
│                      索引 / 存储层                           │
│   Milvus (稠密 + 稀疏向量)  │  PostgreSQL (元数据 + 配置)     │
│   Neo4j (知识图谱，可选)                                     │
├─────────────────────────────────────────────────────────────┤
│                       数据处理层                             │
│   Loader → OCR → Chunker → Embedder → Indexer                │
│   Worker (Redis Stream 消费者，异步处理)                     │
├─────────────────────────────────────────────────────────────┤
│                    模型服务层（外部 HTTP）                    │
│   LLM API  │  Embedding API  │  Rerank API  │  OCR API       │
└─────────────────────────────────────────────────────────────┘
```

从文档解析、向量化、检索到大模型推理，全流程模块化解耦，组件可灵活替换与扩展。支持本地 / 私有云部署，数据完全自主可控，零门槛 Web UI 快速上手。

## 🧩 功能概览

**智能对话**

| 能力 | 详情 |
|------|------|
| ReAct 推理 | 大模型在 Think → Act → Observe 循环中自主决策，调用工具、分析结果、决定停止时机 |
| 工具调用 | 内置知识检索、关键词匹配、深度阅读、附件阅读、网页搜索、思考、技能加载，支持远程 MCP 工具 |
| Evidence-First | Progressive RAG 提示词强制"先检索、深读 chunk、再作答"，引用溯源、拒绝臆造 |
| Agent 预设 | 内置「快速问答」（hybrid 单轮）与「智能推理」（agent 多步），系统提示词可在线 AI 改写；超级管理员可将任一平台级预设设为全局默认 |
| 会话附件 | 对话中上传文件即时入库为会话级检索源，Agent 通过 `read_attachment` 确定性整篇读取，不与正式知识库竞争排序 |
| 上下文管理 | 三层递进式压缩（Token 估算 + Usage 追踪 + LLM 摘要 + 分组截断），长对话不超窗 |
| 流式可视化 | 思考、工具调用、引用、Token 占用通过 SSE 实时推送，逐 token 渲染 |

**知识管理**

| 能力 | 详情 |
|------|------|
| 文档格式 | PDF / Word / Excel / PPT / TXT / Markdown / 图片 / 音频 |
| 文件预览 | Artifact 侧栏统一预览（open-file-viewer）：PDF / Word / Excel / PPT / 图片 / 文本 / Markdown / CSV / 邮件 / 压缩包 / EPUB 等 30 类格式，聊天附件与文档列表一键侧滑预览，Word 默认适宽，颜色跟随应用主题 |
| 链接转存 | 粘贴网页 / 微信公众号链接，后端抓取正文提取为 Markdown 转存入库，自动留存原文链接与封面图，移动端友好 |
| 文档组织 | 文件夹层级管理、重命名、缩略图预览（鉴权拉取） |
| 图文混排 | 自动提取嵌入图片并并发 OCR，按页位置插入识别文本，内容 hash 去重 |
| 结构感知切片 | 按文档逻辑结构切分，表格整块保护，父块（上下文）/ 子块（精检）映射 |
| 三路混合检索 | Dense 语义 + Sparse 稀疏 + BM25 全文，RRF 融合 + Rerank + MMR + 父块扩展 |
| 知识图谱（可选） | 文档入库后异步「三步抽取」实体 → 关系 → 事件，作为第四路并入 RRF。事件中心召回：从每个 chunk 抽取「主谓宾 + 时地」齐全的事件为一等检索单元，双写 Neo4j 事件节点（多跳遍历强）+ Milvus 事件向量集合（向量召回），经「入口A 事件向量召回 + 入口B 实体桥接事件」双种子入口 → 多跳扩展 → 粗排 → 回取关联 chunk，胜任跨文档多跳问答。支持 `enable_events` 开关切换「事件中心（新）」与「纯实体桥接（旧）」两态做 A/B 基准；力导向图支持「实体 / 事件」双层节点（`include_events=true` 显式并入，`node_type` 区分）、总览 / 邻居钻取 / 类型过滤 / 实体详情溯源，文档详情页可查看抽取事件（标题 / 摘要 / 关联实体）。全局与 KB 级双开关控制，默认整体关闭、零额外成本，故障自动降级（图存储不可用 → 返回空；事件集合不可用 → 入口A 跳过仅走入口B；事件未抽到 → 社区摘要仍可单独返回）不影响主链路 |
| 异步 Pipeline | Redis Stream 任务队列 + 独立 Worker，API 与处理解耦，断点续处理 |
| 检索测试 | 独立检索测试页（纯检索，不经过大模型），可视化三路召回 / RRF / Rerank / MMR 链路与多维分数，专为调参设计 |

**集成与扩展**

| 能力 | 详情 |
|------|------|
| LLM | 任意 OpenAI 兼容 API（vLLM / DeepSeek / Qwen / …）/ Ollama |
| Embedding / Rerank | 任意 OpenAI 兼容远程服务（TEI / Infinity / vLLM），前端可视化热切换 |
| OCR 服务 | TextIn / 通用外部 API（远程），多 Provider + 默认 + Fallback 自动切换 |
| ASR 服务 | OpenAI 兼容语音识别（/v1/audio/transcriptions），音频文件自动转写入库，多 Provider + 默认 + Fallback 自动切换 |
| 向量数据库 | Milvus 2.4+（稠密 + 稀疏向量） |
| MCP | 对外暴露知识库能力（MCP Server），对内接入远程 MCP 工具（服务发现 + 自动注册） |
| 技能 Skills | Progressive Disclosure 渐进式加载，按需读取 SKILL.md 完整指令 |

**平台能力**

| 能力 | 详情 |
|------|------|
| 部署 | 本地 / Docker / 内网离线部署（ARM64 + AMD64 镜像打包） |
| 界面 | Web UI / RESTful API / OpenAI 兼容 API / MCP Server |
| 多租户 | 固定角色（admin / member）+ 归属轴 RBAC，知识库私有 / 组织可见 + 点对点读写共享，超级管理员跨租户治理 |
| 用户与注册 | 邀请注册（默认）/ 自助注册两种模式，首登强制改密，个人资料 / 头像，审计日志 |
| 多模型管理 | 数据库持久化多 LLM 配置，前端创建 / 编辑 / 设默认 / 连通性测试，对话动态切换 |
| 安全 | JWT 登录 + 三类 API Key 凭据（租户级 / 用户级 / 外部代理，SHA256 哈希存储，仅 `/v1/` 路径鉴权），超管业务内容可见边界可控，MCP 外部工具输出标记为不可信 |
| 容错降级 | Agent 异常 → hybrid → 纯检索；LLM 不可用 → 返回检索原文；Reranker 异常 → RRF 结果 |
| 可观测性 | Agent 思考 / 工具 / Token 用量 SSE 事件，会话历史持久化 agent_steps |

## 🖼️ 界面预览

<div align="center">

<img src="./docs/images/355ffa79-d70b-4d5e-a624-6a825635cdfe.png" width="90%" alt="界面预览 1" />

<br/><br/>

<img src="./docs/images/0d6832dd-41a6-40a4-8cf8-ea3193dec5cb.png" width="90%" alt="界面预览 2" />

<br/><br/>

<img src="./docs/images/4bc34abf-8545-4898-984d-822f28b4ee75.png" width="90%" alt="界面预览 3" />

</div>

## 🚀 快速开始

### 🛠 环境要求

- [Docker](https://www.docker.com/) & [Docker Compose](https://docs.docker.com/compose/)（20.10+，用于 Milvus / PostgreSQL / Redis）
- [Python 3.12](https://www.python.org/)（⚠️ 3.13+ 存在兼容问题）
- [Node.js 18+](https://nodejs.org/)
- 任意 OpenAI 兼容 LLM API
- Embedding 远程服务（TEI / Infinity / vLLM，可启动后在前端配置）

### 📦 安装与启动

```bash
git clone <repo-url>
cd artoo

# 配置环境变量（本地开发读 backend/.env）
cp backend/.env.example backend/.env
# 编辑 backend/.env：至少填 JWT_SECRET（必填，缺失则 fail-fast）；
# 初始超管 SUPER_ADMIN_USERNAME / PASSWORD 首启自动创建并强制改密；
# LLM / Embedding 可启动后在前端配
```

<details open>
<summary><b>macOS / Linux</b></summary>

```bash
# 1. 启动中间件（Milvus + PostgreSQL + Redis；自动暴露端口到本机）
make infra

# 2. 安装依赖（自动创建 .venv）
make install

# 3. 启动服务（API + Worker + 前端，一条命令并行拉起）
make dev
```

</details>

> **可选：启用知识图谱（GraphRAG）。** 默认整体关闭、零成本。开启需三步：
> ```bash
> make install-graph    # 安装 Neo4j 驱动（可选依赖）
> make infra-graph      # 起中间件 + Neo4j（替代 make infra）
> # 在 backend/.env 设 GRAPH_ENABLE=true（NEO4J_URI 本地直连用 bolt://localhost:7687）
> make dev              # Worker 检测到开关后自动启动图谱抽取
> ```
> 仅当全局开关与知识库的图谱开关同时开启时，该知识库才出现「知识图谱」入口。

<details>
<summary><b>Windows (PowerShell)</b></summary>

Windows 没有 make，分步执行（中间件仍用 Docker）：

```powershell
# 1. 启动中间件
docker compose --profile infra up -d

# 2. 创建 Python 环境（必须 3.12）并装依赖
conda create -n artoo python=3.12 -y
conda activate artoo
pip install --upgrade pip
pip install -r backend/requirements.txt
cd frontend; npm install; cd ..

# 3. 开三个终端分别启动
# 终端 1：API（端口必须 8000，前端代理写死了它）
cd backend; python -m uvicorn app.main:app --reload --port 8000

# 终端 2：Worker
cd backend; python -m app.worker_main

# 终端 3：前端
cd frontend; npm run dev
```

</details>

启动后访问 **http://localhost:3000** 即可使用。

> **Embedding / Rerank / LLM 都可以启动后再配置。** 通过前端「Embedding & Rerank 配置」「模型管理」页面添加远程服务地址，即时生效无需重启。

### 🌐 服务地址

| 服务 | 地址 |
|------|------|
| 前端界面 | `http://localhost:3000` |
| API 文档 | `http://localhost:8000/docs` |
| MCP Server | `http://localhost:8000/mcp` |

### 🧭 使用流程

1. **登录** → 使用初始超管账号（`SUPER_ADMIN_*`）登录，首次强制改密
2. **Embedding 配置** → 添加远程 Embedding 服务地址 → 连通性测试 → 启用
3. **模型管理** → 添加 LLM 配置 → 连通性测试 → 设为默认
4. **知识库** → 创建 → 上传文档 → 等待 Worker 处理完成
5. **对话** → 选择知识库与 Agent 预设 → 提问

## 🐳 部署打包（生产 / 内网离线）

一份统一 `docker-compose.yml`（`profiles: infra / app`）+ 一份 `.env`，打包与部署都走 `deploy/` 下脚本：

```bash
# ① 在有网的机器构建离线包（应用镜像 + 中间件镜像 + compose + .env.example）
#    macOS / Linux：
make build                  # 当前架构
make build ARCH=arm64       # 指定架构（amd64 | arm64）
make build-app              # 仅应用镜像的更新包（迭代更新，不含中间件）

#    Windows (PowerShell)：
.\deploy\build.ps1                 # 当前架构
.\deploy\build.ps1 -Arch arm64     # 指定架构（amd64 | arm64）
.\deploy\build.ps1 -AppOnly        # 仅应用镜像的更新包
# 产物均在 dist/

# ② 把 dist/ 整体拷到（Linux）服务器，一键部署
cd dist && ./install.sh     # 加载镜像 → 引导填 .env → 起中间件(infra) → 起应用(app)
```

`install.sh` 首次运行会从 `.env.example` 生成 `.env` 并提示填写必填项（`JWT_SECRET`、`SUPER_ADMIN_*`、`LLM_*`、`EMBED_BASE_URL`、`RERANK_BASE_URL`），填好后再次执行即可拉起全部服务。

手动等价命令（在 `dist/` 内）：

```bash
docker compose -f docker-compose.yml --profile infra up -d   # 起中间件，等 healthy
docker compose -f docker-compose.yml --profile app up -d     # 起应用
```

> **可选：部署时启用知识图谱。** 打包时加 `--with-graph`，应用镜像内会装 Neo4j 驱动、离线包额外导出 Neo4j 镜像：
> ```bash
> deploy/build.sh --with-graph              # 当前架构
> deploy/build.sh --arch arm64 --with-graph # 指定架构
> ```
> 部署侧无需额外命令：`install.sh` 读取 `.env` 的 `GRAPH_ENABLE`，为 `true` 时自动叠加 `--profile graph` 一并拉起 Neo4j。

## 🔍 检索模式

| 模式 | 流程 | 适用场景 |
|------|------|----------|
| **direct** | 稠密向量 ANN 检索 | 简单查询、低延迟 |
| **hybrid**（快速问答） | Dense + Sparse + BM25 并行 → RRF 融合 → Rerank 精排 → MMR → 父块扩展（KB 开启图谱时叠加事件中心 Graph 第四路：双入口种子事件 → 多跳扩展 → 粗排 → 回取关联 chunk） | 通用场景 |
| **agent**（智能推理） | ReAct 循环：自主调用 grep / 语义检索 / 深读 / 思考 / 网搜，迭代直至提交答案 | 复杂多跳查询、需推理综合 |

### 事件中心图谱召回（GraphRAG 第四路）

开启知识图谱后，第四路从旧的「实体→邻居子图→关联 chunk」**实体桥接**升级为以**事件**为中心的召回流程，补回纯向量召不回的多跳关联：

```
query
  │
  ├─ 入口A（事件向量召回）: query 向量在 Milvus event 集合做 ANN → 召回事件
  │   （event_store 不可用时跳过，仅走入口B，渐进可用）
  ├─ 入口B（实体桥接事件）: query → 抽实体名 → 命中实体 → (:Entity)<-[:MENTIONS]-(:Event)
  │
  ├─ 种子事件（两入口各自 seed_k 上限，去重）
  ├─ 多跳扩展: (:Event)-[:MENTIONS]->(:Entity)<-[:MENTIONS]-(:Event2)，可配置跳数
  ├─ 粗排 → 取 top-k 事件回取其关联的原文 chunk
  └─ RetrievalResult(match_type='graph')，并入 RRF 第四路（评分 1/(1+rank)，量纲一致）
```

- **事件为一等检索单元**：抽取时把「主谓宾 + 时地」齐全的完整语义单元作为事件，双写 Neo4j（事件节点 + `MENTIONS` 实体边，多跳遍历强）与 Milvus 独立事件向量集合 `kb_event_<kb_id>`（向量召回），两者用 `event_id` 对齐。
- **A/B 可切**：KB 图谱开启但 `enable_events=false` 时，本路退回纯实体桥接旧逻辑，便于在同一 KB 上对比两种召回效果。
- **降级矩阵**：图存储不可用 → 返回空；事件集合不可用 → 入口A 跳过仅走入口B；无种子事件 → 事件结果为空，社区摘要仍可单独返回。任一降级都不影响其余三路。
- **可视化**：力导向图新增「事件」层节点（`include_events=true` 显式并入，`node_type` 区分 `entity` / `event`），文档处理结果页可查看每个 chunk 抽取的事件（标题 / 摘要 / 关联实体）。

### Agent ReAct 循环

```
用户查询 →（有历史时）改写指代、脱敏历史检索结果强制重检
  │
  └─ while 未完成 且 未达 max_iterations：
       ├─ Think：流式调用 LLM，实时发射 THOUGHT 事件
       ├─ 上下文管理：Usage 估算 →（>50%）LLM 摘要合并 →（>80%）分组截断
       ├─ Analyze：判定终止
       │    ├─ final_answer 工具 → 提交答案
       │    ├─ 自然停止 → 引导调用 final_answer
       │    └─ stuck loop / 空响应 → 重试或合成答案
       ├─ Act：执行工具调用（可并行），发射 TOOL_CALL / TOOL_RESULT 事件
       └─ Observe：工具结果追加到消息，进入下一轮
  │
  └─ 异常 → 降级到 hybrid 快路径
```

更多关于工具、提示词、上下文管理与切片策略的细节，见 [技术架构详解](./ARCHITECTURE.md)。

## 🔧 技术栈

| 组件 | 选型 |
|------|------|
| 后端框架 | FastAPI + Uvicorn |
| 向量数据库 | Milvus 2.4+ |
| 关系数据库 | PostgreSQL 16 |
| 任务队列 | Redis Stream |
| 前端 | React 18 + TypeScript + Tailwind CSS v4 |
| 文档解析 | PyMuPDF / python-docx / openpyxl / python-pptx |
| Token 估算 | tiktoken（cl100k_base） |
| LLM | 任意 OpenAI 兼容 API / Ollama |
| Embedding / Rerank | 任意 OpenAI 兼容远程服务 |

## 📂 项目结构

```
artoo/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口
│   │   ├── worker_main.py       # Worker 入口
│   │   ├── config.py            # 配置
│   │   ├── api/                 # API 路由（chat / kb / document / auth / 邀请 / *config …）
│   │   ├── auth/                # 多租户认证与 RBAC（JWT / API Key / 知识库授权 / 审计）
│   │   ├── agent/               # ReAct Agent 引擎
│   │   │   ├── engine.py        #   核心 ReAct 循环
│   │   │   ├── events.py        #   EventBus 事件总线
│   │   │   ├── tools/           #   工具层（检索 / 深读 / 附件 / 网搜 / 技能 / MCP …）
│   │   │   ├── memory/          #   三层递进式上下文管理
│   │   │   ├── skills/          #   技能（Progressive Disclosure）
│   │   │   └── prompts/         #   Progressive RAG 系统提示词
│   │   ├── retrieval/           # 检索工具（hybrid / vector / sparse / bm25 …）
│   │   ├── pipeline/            # 文档处理管道（loader / ocr / chunker …）
│   │   ├── models/              # 模型 Provider 抽象（LLM / Embedding / Rerank）
│   │   └── storage/             # 存储层（Milvus / PostgreSQL）
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/                    # React 前端
├── docker-compose.yml           # 统一编排（profiles: infra / app）
├── docker-compose.override.yml  # 本地开发覆盖（暴露中间件端口）
├── deploy/                      # 离线部署（build.sh / install.sh / milvus 调优）
├── .env.example                 # 部署配置清单
└── Makefile
```

## 🛠️ 常用命令

```bash
make install            # 安装所有依赖（自动创建 .venv）
make dev                # 启动前后端 + Worker
make dev-backend        # 仅后端 API
make dev-worker         # 仅 Worker
make dev-frontend       # 仅前端
make infra              # 启动基础设施（Milvus + Redis + PostgreSQL）
make infra-graph        # 启动基础设施 + Neo4j（启用知识图谱时用）
make infra-down         # 停止基础设施
make install-graph      # 安装知识图谱可选依赖（Neo4j 驱动）
make test               # 运行后端测试
make build              # 构建离线部署包（deploy/build.sh）
make build ARCH=arm64   # 指定架构构建（amd64 | arm64）
make build-app          # 仅应用镜像的更新包（不含中间件）
```

## 🚀 部署

完整的生产 / 内网离线部署打包流程见上文 [部署打包](#-部署打包生产--内网离线)。

## 📘 文档

| 文档 | 说明 |
|------|------|
| [技术架构详解](./ARCHITECTURE.md) | ReAct 引擎、工具层、上下文管理、切片策略、OCR 扩展 |

## 🗺️ Roadmap

- [ ] 端到端检索评测体系（RAGAS，量化召回与生成质量）
- [ ] Chunk 元数据增强（Enricher 生成摘要 / 关键词）+ 过滤检索
- [ ] 数据库迁移管理（Alembic）
- [ ] 数据源连接器（飞书 / Notion）
- [ ] 多 Worker 水平扩展

## 🧭 开发指南

快速开发模式无需每次重建 Docker 镜像：`make infra` 启动基础设施后，分别运行 `make dev-backend`、`make dev-worker`、`make dev-frontend`，后端支持 `--reload` 热重载，前端 Vite 自动热更新。Windows 下无 make，按上文「快速开始 → Windows」分三个终端手动启动。

## 🤝 贡献

欢迎提交 [Issue](../../issues) 和 Pull Request。

**流程：** Fork → 创建分支 → 提交变更 → 发起 PR

## 📄 许可证

本项目基于 [MIT](./LICENSE) 协议发布。你可以自由使用、修改和分发本项目代码，但需保留原始版权声明。

## 📈 Star 趋势

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=9ilfoyl3/artoo&type=Date&theme=dark" />
  <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=9ilfoyl3/artoo&type=Date" />
  <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=9ilfoyl3/artoo&type=Date" />
</picture>
