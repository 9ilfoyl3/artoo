# Agent Note: 双架构离线发布包

Status: implemented

[English](2026-09-29-dual-arch-offline-release-packages.md)

## 问题

`deploy/build.sh` 把所有产物写进同一个 `dist/` 目录，amd64 和 arm64 先后构建会互相覆盖 `app-images.tar` 和 `infra-images.tar`。交付双架构需要人工搬运文件，导出的 tar 没有校验和，运维手册也没有给实施人员明确区分「首次部署完整包」和「仅应用更新包」。

## 决策

- `build.sh` 支持 `--out <dir>`（默认 `dist`），每个发布产物构建到独立交付目录：`artoo-deploy-amd64`、`artoo-deploy-arm64`、`artoo-update-amd64`、`artoo-update-arm64`。默认的 `make build` / `make build-app` 行为不变。
- 每次构建为导出的 tar 生成 `SHA256SUMS`，并随包附带新的 `deploy/DELIVERY.md` 交付手册；手册区分首次部署（完整包）与更新部署（仅应用包）两条流程，提供可直接复制的命令和验证步骤。
- 中间件镜像拉取支持可选的 `ARTOO_PULL_MIRROR` 前缀（例如 `docker.m.daocloud.io`），拉取失败自动回退直连：部分构建网络无法访问 Docker Hub，而镜像源也会间歇性拒绝个别镜像。
- `install.sh` 改用 awk 改写 compose 文件，替代 GNU `sed -i`，同一脚本可在 Linux 与 macOS 运行；紧邻全角标点的变量统一加花括号，规避 bash 3.2 的多字节变量名解析缺陷。

## 备选方案

**每个应用只打一份多平台镜像 tar。** 否决：导出多平台 manifest 要求所有构建宿主机启用 containerd 镜像存储，且每次部署加载时间翻倍；按架构拆分 tar 保持了离线流程和既有 `install.sh` 契约。

**继续单一 `dist/` 输出，文档里写人工搬运步骤。** 否决：人工搬文件正是实施人员最容易出错的环节；自包含目录让交付物自描述。

**拉取无条件走镜像源。** 否决：镜像源会间歇性对个别镜像返回 403（实际遇到过 `quay.io/coreos/etcd` 和 arm64 的 `minio/minio` tag），必须保留直连回退才能保证构建无人值守完成。

## 后果

发布工程师可以无冲突地产出四个自包含交付目录；实施人员部署前用 `sha256sum -c SHA256SUMS` 校验完整性。更新包有意不含中间件镜像，既有部署的数据卷不受影响。代价是：不同 `--out` 的构建会重复导出 tar；校验和按目录生成；`install.sh` 的 compose 改写依赖 awk，所有受支持环境均自带 awk。

## 测试

- `deploy/build.sh`、`deploy/install.sh`、`deploy/reset-knowledge-data.sh` 均通过 `bash -n`。
- 从当前工作区构建全部四个包（`deploy`/`update` × `amd64`/`arm64`），各包 `SHA256SUMS` 校验通过。
- 在 Docker Desktop 的临时目录中对 arm64 完整包做端到端首次部署：八个服务全部 Up/healthy，后端 `GET /` 返回 JSON 健康信息，前端返回 HTTP 200。
- 用更新包替换 `app-images.tar` 后执行 `bash install.sh update`：镜像加载、应用容器重建、服务验证再次全部通过。
- 两种架构的 `app-images.tar` 与 `infra-images.tar` 均通过 `docker load` 加载（digest 校验），并在模拟器下冒烟运行 amd64 backend 与 frontend 镜像（backend 内报告 `x86_64`，frontend 执行 `nginx -v`）。
