# Agent Note: Build image pull local fallback

Status: implemented

[English](2026-10-29-build-image-pull-local-fallback.md) | 中文

## Problem

`deploy/build.sh` 之前通过配置的镜像源拉取镜像，失败后回退直连 `docker pull`。当上游
tag 下线或 registry 临时不可达时，整个部署构建会直接失败——即使构建主机上早已存在
目标架构的正确镜像。

## Decision

`pull_image` 现在把复用本地镜像作为最后手段。顺序为：先走配置的镜像源，再直连拉取，
两者都失败时，若本地已有镜像且其 `Architecture`（通过 `docker image inspect` 读取）
与 `$ARCH` 一致则复用。`$ARCH` 未设置（单平台本地构建）时接受任意本地镜像。跨架构
替换始终被禁止，因此该回退不会把 arm64 镜像交给 amd64 主机，反之亦然。

## Alternatives considered

**固定镜像 digest。** 拒绝：registry 本身不可达时 digest 无济于事，且每次上游更新
都需要额外的协调步骤。

**构建失败后要求运维手动预拉取。** 拒绝：这正是本次改动要消除的失败模式，而最常见的
场景——主机上已有正确镜像——仍会在每次构建时需要人工介入。

**不校验架构直接复用任意本地镜像。** 拒绝：构建主机上可能存有其他平台的镜像；架构
不匹配的镜像要到容器启动时才会失败（或静默走模拟），离决策点太远。

## Consequences

只要构建主机上已有镜像，部署就能在 registry 短暂故障或上游 tag 下线时继续完成。代价
是：当远端本应带来更新时，可能复用了过期的本地 tag；且除架构校验外没有内容校验，因此
构建主机必须可信。本地镜像的来源可信性由调用方自行确认。

## Testing

`bash -n deploy/build.sh` 通过；拉取与回退分支经过代码走查，未在 CI 中模拟真实的
registry 故障。
