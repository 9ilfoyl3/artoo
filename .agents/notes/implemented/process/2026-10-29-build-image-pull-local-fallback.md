# Agent Note: Build image pull local fallback

Status: implemented

[English](2026-10-29-build-image-pull-local-fallback.md) | [中文](2026-10-29-build-image-pull-local-fallback.zh.md)

## Problem

`deploy/build.sh` pulled every image through the configured mirror with a direct
`docker pull` fallback. When an upstream tag is retired or a registry is temporarily
unreachable, the whole deployment build fails even when a correct image of the target
architecture already exists on the build host from an earlier run.

## Decision

`pull_image` now reuses a local image as a last resort. The order is: configured mirror
first, then direct pull, and only when both fail, an existing local image whose
`Architecture` (read via `docker image inspect`) matches `$ARCH`. When `$ARCH` is unset
(single-platform local builds), any local image is accepted. Cross-architecture
substitution is never allowed, so the fallback cannot hand an arm64 image to an amd64
host or vice versa.

## Alternatives considered

**Pin image digests.** Rejected: digests do not help when the registry itself is
unreachable, and every upstream update would require a coordination step.

**Fail the build and require the operator to pre-pull manually.** Rejected: that is
exactly the failure mode this change removes, and the common case — the host already
holds the right image — would still need manual work on every rebuild.

**Reuse any local image regardless of architecture.** Rejected: build hosts may hold
images for other platforms; launching a mismatched image fails (or silently emulates)
at container start, far from the point where the mistake was made.

## Consequences

Deployments survive transient registry outages and upstream tag removals whenever the
build host already has the image. The cost is that a stale local tag can be reused when
the remote actually intended an update, and the fallback performs no content
verification beyond the architecture check, so the build host must be trusted. The
caller remains responsible for the provenance of any local image it relies on.

## Testing

`bash -n deploy/build.sh` passes; the pull and fallback branches were reviewed but no
live registry outage was simulated in CI.
