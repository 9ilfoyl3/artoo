# Agent Note: Unified recall and durable summary artifacts

Status: implemented

## Problem

The Agent exposed several recall and reader tools with overlapping semantics:
`knowledge_search`, `grep_chunks`, `list_knowledge_chunks`,
`list_knowledge_documents`, and `summarize_documents`. This increased prompt
surface, made tool selection unstable on smaller models, and allowed collection
tasks to start long model calls with no durable result to reuse on the next
turn. Retrieval results also lacked a single deterministic post-processing
pipeline for near-duplicate removal, neighbour expansion, sequential merging,
and cutoff auditing. The Agent loop still relied on static token estimates,
short total-time stream limits, unchecked JSON parsing, uniform tool-output
budgets, and unrestricted parallel tool execution.

## Decision

Artoo now exposes one model-facing recall primitive: `search_knowledge`.
It supports `hybrid`, `semantic`, `keyword`, and `summary` modes. The old
recall tools are no longer registered into Agent presets or built-in presets.

The document plane is intentionally small:

- `list_documents` returns authorized formal and session documents with
  pagination, filename keyword filtering, and summary-artifact status.
- `read_document` resolves `doc_id` or `chunk_id`, supports `offset` / `limit`
  sequential reads and `query` / `regex` context windows, and enforces the same
  KB/session authorization scope as retrieval.
- `todo_write` is the only additional planning tool. It owns a bounded task list
  in `AgentState` and is serialized against reads because it mutates loop state.

`search_knowledge` applies deterministic post-processing: exact chunk-ID
deduplication, normalized content signatures, token-overlap near-duplicate
removal, child-to-parent context expansion, bounded neighbour expansion,
adjacent parent-chunk merging, stable source/position ordering, FAQ answer
injection, and a retrieval audit attached to the tool result and SSE event.

Formal documents and session files now have a durable `DocumentSummary`
artifact. Indexing completion schedules a `pipeline:summaries` Redis Stream
task; a separate worker generates a thinking-disabled JSON profile with
`summary`, `gist`, `topics`, `doc_type`, and `typical_question`. The artifact is
versioned by parent-chunk IDs and content. Summary retrieval reads the
authoritative PostgreSQL row; Redis is not a correctness dependency.

The Agent core also now uses provider usage to calibrate token estimates,
persists compacted checkpoints by session and prompt fingerprint, uses a
per-chunk stall watchdog instead of a total stream timeout, conservatively
repairs JSON but refuses to execute truncated or invalid tool arguments,
applies per-tool output budgets, and separates read-only parallelism from
mutation barriers. Unknown external MCP tools are treated as mutations unless a
future trust annotation proves otherwise.

## Alternatives considered

**Keep the old recall tools as aliases.** Rejected because aliases keep the
model-facing choice problem, duplicate prompt schemas, and make retrieval
post-processing drift across implementations.

**Generate summaries synchronously during document upload.** Rejected because
model latency would delay indexing and make ingestion reliability depend on
the LLM service. The durable task lane lets the source document become
available immediately.

**Store summaries only in Redis.** Rejected because a cache eviction or Redis
restart would discard the artifact and force expensive recomputation. Redis is
used for queue transport and optional acceleration, while PostgreSQL owns the
versioned profile.

**Search summaries through a second Milvus collection.** Rejected for this
change because profiles are small metadata records and a second consistency
domain would add operational work without improving the primary retrieval
path. Lexical profile ranking in PostgreSQL is sufficient for inventory and
document discovery; exact evidence still comes from the main hybrid retriever.

**Treat every tool call as parallelizable.** Rejected because external tools
and state mutations may have side effects. The scheduler runs contiguous
read-only calls concurrently, then serializes mutation boundaries.

**Repair and execute truncated tool JSON.** Rejected because a syntactically
repairable object may still be semantically incomplete. Truncated arguments
produce a tool error observation and are never executed.

## Consequences

Presets that explicitly whitelist tools must use the new names. Built-in
presets are synchronized automatically; custom presets remain owner-controlled.
Business systems that consume SSE tool names must map the old names to the new
surface. The request schema and MCP transport do not change.

Summary generation adds one Redis Stream lane and worker concurrency, plus two
new PostgreSQL tables (`document_summaries` and
`agent_compaction_checkpoints`). Summary failures are isolated from ingestion.
The model sees an audit object on retrieval results, which is additive and may
be ignored by older clients.

The unified retriever reduces prompt and tool-selection ambiguity and makes
whole-KB questions fast once profiles are ready. Missing profiles degrade to
the existing document reader rather than inventing a summary. Neighbour
expansion and merging may increase one result's context, but character bounds
keep the total tool output within its per-tool budget.

## Testing

`backend/tests/test_unified_knowledge_agent_core.py` covers JSON repair and
truncation rejection, per-tool output budgets, Todo validation, deterministic
retrieval post-processing, token calibration, checkpoint round trips, unified
summary mode, mutation barriers, stall watchdogs, document filtering and
summary status, authorized document reading, and summary profile
normalization. Existing Agent, retrieval, structured-result, and data-plane
suites cover the compatibility-sensitive loop and protocol paths.

The real 48-document KB A/B run (`backend/benchmarks/results/2026-10-16-unified-agent-ab.json`)
shows the search-only baseline answering in 30.5 seconds after 4 retrieval calls
but covering only 1 of 46 readable documents. The summary-artifact path used
`list_documents` plus one 42 ms `search_knowledge(mode="summary")` call, covered
all 46 documents, and produced a 3,308-character grounded answer in 40.2
seconds. Tool calls fell from 5 to 2 and file coverage increased by 45; the
extra answer time was model generation, not retrieval.
