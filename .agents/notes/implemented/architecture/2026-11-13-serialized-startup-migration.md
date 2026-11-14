# Agent Note: Serialized one-shot database migration

Status: implemented

## Problem

Every uvicorn worker and the ingestion worker ran schema DDL during application
startup. A deployment could therefore execute several `ALTER TABLE` statements in
parallel while ingestion transactions still held long-lived table locks. In the
November 2026 incident, `ALTER TABLE documents` waited behind four indexing
transactions for 35 minutes, and all API workers remained in startup until the
processes were restarted.

## Decision

Production Compose runs schema changes in a dedicated one-shot `migrate` service
before `backend` or `worker` starts. The migration entrypoint acquires a PostgreSQL
transaction advisory lock and sets bounded `lock_timeout` and `statement_timeout`
values. `ADD COLUMN` statements are rewritten to `ADD COLUMN IF NOT EXISTS` so
replays are idempotent and do not emit expected duplicate-column errors.

`backend` and `worker` set `AUTO_MIGRATE_ON_STARTUP=false` and only verify database
reachability. Development and manual host runs keep auto-migration enabled by
default. Recovery of stale `processing` documents moved to worker startup and is no
longer performed by API workers.

## Alternatives considered

**Keep migrations in application startup and add only an advisory lock.** Rejected
because every application process would still couple availability to DDL, and a
single long ingestion transaction could keep all API workers from becoming ready.

**Only increase `lock_timeout` without separating migration.** Rejected because
startup would fail or skip required schema changes depending on unrelated business
traffic.

**Adopt a full external migration framework immediately.** This remains a viable
long-term direction, but the existing application-managed migrations needed an
operational isolation boundary first. The one-shot entrypoint can be replaced by a
framework without changing the deployment contract.

## Consequences

Deployments gain one one-shot container and must run `migrate` before starting or
recreating backend/worker. A failed migration blocks application startup instead of
leaving an API running against an unknown schema. Direct Compose users are covered
by `depends_on: service_completed_successfully`; `install.sh` also runs and waits for
the migration explicitly. Update tags the current applications as
`rollback-pre-update-<timestamp>` and keeps replaced images by default so the
immediately previous version remains available; `CLEAN_OLD_IMAGES=1` opts into
cleanup. Deployment scripts only restore execute bits on top-level shell scripts and
force `.env` to mode `0600`; they never recursively make deployment secrets readable.

## Testing

`backend/tests/test_ingestion_lock_safety.py` verifies replay-safe `ADD COLUMN`
rewriting and timeout formatting. Deployment validation covers Compose parsing and
`install.sh` shell syntax. A disposable PostgreSQL 16 instance verifies that the
migration command succeeds twice and creates the expected document/session columns.
With another session holding an `ACCESS SHARE` lock on `documents`, a replay exits
after the five-second lock timeout instead of hanging.
