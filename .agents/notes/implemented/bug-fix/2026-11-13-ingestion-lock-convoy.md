# Agent Note: Bounded ingestion slots and short pipeline transactions

Status: implemented

## Problem

The document pipeline kept a SQLAlchemy session transaction open across OCR,
Embedding, Milvus cleanup, and vector insertion after reading document metadata.
At the same time, the worker created an unbounded task for every consumed message
and performed the document-status query before acquiring its processing semaphore.

During a restart, backend DDL requested an `ACCESS EXCLUSIVE` lock on `documents`
while four indexing transactions held `ACCESS SHARE`. Subsequent status and list
queries queued behind the DDL; the worker exhausted its 30-connection pool, summary
tasks timed out after 180 seconds, and poison-pill callbacks could no longer persist
failure state.

## Decision

The pipeline now snapshots `KnowledgeBase.tenant_id` and `Document.filename` in one
short transaction and commits before any CPU, OCR, Embedding, or Milvus work. It
persists chunks and the completed document status in a second transaction only after
vector indexing finishes.

Worker dispatch is bounded by `PIPELINE_MAX_CONCURRENT`; no new task is created while
all slots are occupied. Each task acquires its slow-lane and global processing
semaphores before doing the idempotency status lookup, logging "processing", or
opening the pipeline work. Stale `processing` recovery runs only during worker
startup.

## Alternatives considered

**Increase the database pool.** Rejected as a fix because additional connections
would also wait behind the same DDL lock and increase pressure on PostgreSQL.

**Only reduce `PIPELINE_MAX_CONCURRENT`.** Rejected because it narrows but does not
remove the long transaction or unbounded task accumulation.

**Rely on operators to stop the worker before every restart.** Rejected as the sole
fix because direct Compose deployments and crashes would remain unsafe.

## Consequences

Database connections are no longer held during long document processing. Worker
backlog remains in Redis/PEL until a processing slot is available, making the
semaphore the authoritative admission boundary. A document can still leave vectors
if Milvus succeeds and the final database commit fails; the existing pre-index orphan
cleanup remains the recovery mechanism.

## Testing

`backend/tests/test_pipeline_empty_document.py` verifies that no transaction remains
open when `process_to_vectors` runs. `backend/tests/test_ingestion_lock_safety.py`
verifies that the status lookup waits for a processing slot, dispatch waits when the
slot set is full, and the worker still ACKs successful work.
