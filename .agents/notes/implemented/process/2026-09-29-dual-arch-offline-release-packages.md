# Agent Note: Dual-arch offline release packages

Status: implemented

[中文](2026-09-29-dual-arch-offline-release-packages.zh.md)

## Problem

`deploy/build.sh` wrote every artifact into the single `dist/` directory, so building for amd64 after arm64 silently overwrote `app-images.tar` and `infra-images.tar`. Delivering both architectures required manual file shuffling, exported tars carried no checksums, and the ops manual did not give implementation engineers a clear split between first-deployment full packages and application-only update packages.

## Decision

- `build.sh` accepts `--out <dir>` (default `dist`), so each release artifact is built into its own delivery directory: `artoo-deploy-amd64`, `artoo-deploy-arm64`, `artoo-update-amd64`, and `artoo-update-arm64`. The default `make build` / `make build-app` behavior is unchanged.
- Every build generates `SHA256SUMS` over its exported tars and ships the new `deploy/DELIVERY.md` handbook, which separates the first-deployment (full) and update (app-only) flows with copy-paste commands and verification steps.
- Infra image pulls support an optional `ARTOO_PULL_MIRROR` prefix (for example `docker.m.daocloud.io`) with automatic fallback to direct pulls, because Docker Hub is unreachable from some build networks while mirrors intermittently deny individual images.
- `install.sh` rewrites the compose file with awk instead of GNU `sed -i`, so the same script runs on Linux and macOS; variables adjacent to full-width punctuation are braced to survive bash 3.2's multibyte variable-name parsing bug.

## Alternatives considered

**Ship one multi-platform image tar per app.** Rejected: saving multi-platform manifests requires the containerd image store on every build host and doubles image-load time on every deployment; per-architecture tars keep the offline flow and the existing `install.sh` contract intact.

**Keep the single `dist/` output and document the manual file moves.** Rejected: manual shuffling is exactly the step implementation engineers get wrong; self-contained directories make the deliverable self-describing.

**Use mirrors unconditionally for pulls.** Rejected: mirrors intermittently return 403 for individual images (observed for `quay.io/coreos/etcd` and the arm64 `minio/minio` tag), so a direct-pull fallback is required to keep builds unattended.

## Consequences

Release engineers produce four self-contained delivery directories without collisions, and implementers verify integrity with `sha256sum -c SHA256SUMS` before deploying. Update packages intentionally omit middleware images, so existing deployments keep their data volumes untouched. The costs are that builds with different `--out` values re-export tars, checksum files are per-directory, and `install.sh` compose rewriting now depends on awk, which is present on all supported hosts.

## Testing

- `bash -n` passes for `deploy/build.sh`, `deploy/install.sh`, and `deploy/reset-knowledge-data.sh`.
- Built all four packages (`deploy`/`update` × `amd64`/`arm64`) from the current working tree; each package's `SHA256SUMS` verifies.
- Deployed the arm64 full package end to end against Docker Desktop in a scratch directory: all eight services reached Up/healthy, backend `GET /` returned the JSON health message, and the frontend returned HTTP 200.
- Replaced `app-images.tar` with the update package and ran `bash install.sh update`: images loaded, application containers were recreated, and the service checks passed again.
- Loaded both architectures' `app-images.tar` and `infra-images.tar` via `docker load` (digest validation) and smoke-ran the amd64 backend and frontend images under emulation (`x86_64` reported by the backend container; `nginx -v` for the frontend).
