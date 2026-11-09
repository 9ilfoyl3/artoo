# Artoo 后端镜像（多阶段构建）
# Embedding/Rerank/OCR 统一使用远程服务，无需 ML 模型库。
#
#   docker build -t artoo-backend:latest .

# ============ 阶段一：安装依赖 ============
FROM python:3.12-slim AS builder

WORKDIR /app

# build-essential 仅用于个别无预编译 wheel 的包；装在 builder 阶段，不进最终镜像。
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 如果下载超时，可加镜像源：
#   -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
COPY requirements.txt .
COPY requirements-graph.txt .

# 知识图谱可选依赖（Neo4j 驱动）默认不装，保持镜像精简、不开图谱零成本。
# 需要图谱时构建：docker build --build-arg WITH_GRAPH=true -t artoo-backend:latest .
ARG WITH_GRAPH=false
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt && \
    if [ "$WITH_GRAPH" = "true" ]; then \
      pip install --no-cache-dir --prefer-binary -r requirements-graph.txt; \
    fi

# 预下载 tokenizer 词表，保证离线/内网环境也能使用精确 cl100k_base 计数。
# tiktoken 默认会在首次调用时联网下载；运行期无外网时会静默降级为字符估算。
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken
RUN mkdir -p /opt/tiktoken && \
    python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

# ============ 阶段二：运行镜像 ============
FROM python:3.12-slim

WORKDIR /app

# 运行时系统依赖：
#   libglib2.0-0        pymupdf 等
#   curl                healthcheck
#   libreoffice-writer  旧版 Word (.doc) 转 .docx（soffice --headless）
#   fonts-noto-cjk      LibreOffice 转换中日韩文档时的字体，避免中文乱码/丢字
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    curl \
    libreoffice-writer \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app/data

# 从 builder 复制已装好的 Python 包
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /opt/tiktoken /opt/tiktoken
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken

COPY app/ ./app/
# 运维脚本（Milvus 拓扑初始化 / 数据清除 / 重建索引）。
# 必须进镜像：deploy/reset-knowledge-data.sh 是在 backend 容器内执行
# `python -m scripts.purge_data` 的，容器里没有它就无法完成拓扑切换。
COPY scripts/ ./scripts/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
