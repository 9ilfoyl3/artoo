# Agent Note: Controlled timeout document retry

Status: implemented

English | [中文](2026-10-23-controlled-timeout-document-retry.zh.md)

## Problem

Bulk ingestion can leave documents whose worker task exceeded the 60-minute
pipeline timeout. Retrying them indiscriminately can immediately recreate the
same Milvus and worker pressure. Operators need a safe, transferable procedure
that retries only timeout failures, uses the normal batch-retry business path,
and throttles submissions based on current document backlog.

## Decision

`deploy/retry-timeout-failed-docs.py` can summarize failed error messages and
retry one explicit LIKE category. Its default remains the safe timeout pattern
`处理超时%`; retrying all failures requires the explicit `--allow-all-failed`
flag. It submits IDs through `POST /api/documents/batch-retry`, preserving the
endpoint's chunk/vector cleanup and MinIO queue semantics. Before every batch it
waits until pending+processing documents are below `--max-backlog`; it also
supports dry-run, batch size, batch pause, limit and maximum wait limits. API
credentials are read from an environment variable or the deployment `.env`.

## Alternatives considered

**Direct PostgreSQL status updates plus Redis enqueue.** Rejected: that would
duplicate the batch-retry endpoint's cleanup and consistency responsibilities.

**Retry every failed document.** Rejected: non-timeout failures have different
causes, and mass retry can recreate the overload.

**Submit all IDs at once.** Rejected: it bypasses backpressure and can bring the
Milvus index/load path back to the failure state.

## Consequences

Operators can execute a dry run and small controlled batch before resuming all
timeout failures. The retry process may take longer because it deliberately
waits for the backlog to drain. The script assumes host access to the
`alegal-postgres` container and a valid maintenance API key.

## Testing

`python3 -m py_compile deploy/retry-timeout-failed-docs.py`, `--help`, the
status/backlog/ID parsing paths, SQL literal escaping, and failed-reason summary
parsing were exercised with a fake subprocess; no production database was
modified.
