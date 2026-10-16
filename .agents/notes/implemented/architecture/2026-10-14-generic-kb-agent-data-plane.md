# Agent Note: Generic knowledge-base Agent data plane

Status: implemented

## Problem

The Agent had search and read tools but no deterministic document-discovery
surface. Collection requests therefore depended on broad searches and sometimes
inferred missing files from filename numbering. The formal-document read tool
also counted and returned both parent and child chunks, so repeated text could
enter the model context, and no state recorded whether a document had been read
completely. Retrieval results had no stable evidence identity, repeated
multi-document summaries had no reusable cache, and exact arithmetic still had
to happen in model reasoning.

The problem is domain-neutral. It affects any knowledge base containing a set
of documents that must be inventoried, summarized, audited, or covered. Business
specific facts, calculations, document anchors, and approval rules do not belong
in this layer.

## Decision

Superseded by
[Unified recall and durable summary artifacts](2026-10-16-unified-recall-and-summary-artifacts.md)
for the model-facing tool names and summary lifecycle described below.

The generic knowledge-base Agent gains a deterministic retrieval and collection
data plane.

`list_knowledge_documents` enumerates the authorized formal KBs and optional
session-file source. It returns stable `doc_id`, filename, type, status, source,
KB ID, and indexed chunk count, with pagination. Results are stored in
`AgentState.document_catalog`.

`read_document` is the preferred full-document reader. It reads formal documents
through parent chunks only, matching the document-preview path and avoiding
parent/child duplication. It returns `has_more`, `next_page`, `read_chunks`, and
`fully_read`, and records read ranges in `AgentState.document_read_ranges`.
`list_knowledge_chunks` remains as the legacy-compatible alias.

`summarize_documents` is a domain-neutral map-reduce tool. Each selected
document is loaded and summarized independently. Oversized documents are split
into deterministic character batches, summarized, and reduced. Concurrency is
bounded, each document is isolated from the others, and the result carries the
stable document identity and full-read status. Successful summaries are cached
in Redis by document content version, model identity, and task instruction; the
cache is best-effort and unavailable Redis degrades to direct summarization.

Retrieval and reading register evidence with a stable
`evidence_id = hash(doc_id, chunk_id)`. `AgentState` indexes evidence by ID and
chunk, so repeated retrieval does not create duplicate evidence identities.

`calculate` provides bounded Decimal arithmetic, percentage, min/max/sum, and
explicit rounding without business formulas or semantic interpretation.

Long document collection runs stream `tool_progress` events through the engine
rather than blocking silently until the whole tool call returns. Per-document
summary calls run with a bounded concurrency of six, explicitly disable model
thinking, and use the same stable `cache_scope` across repeated core-summary
tasks. Progress is metadata only and contains no document body.

For collection-oriented requests, the system prompt directs the model to list
documents before broad searches, use only returned doc_ids, never infer files
from numbering, and report unread or unavailable documents. If retrieval has
already exhausted its bounded rewrite opportunity and still produces no new
evidence, the loop deterministically points the model to the document catalog.

The built-in `preset-smart-reasoning` preset is platform-owned and merges the
current canonical `allowed_tools` list during startup synchronization, so old
database rows do not keep the Agent on the legacy search/read tool set. User
presets are not rewritten; their owners must opt in explicitly. Preloaded
summary and document-analysis skills also prefer the catalog, `read_document`,
and `summarize_documents`, with the legacy search/read path only as a fallback.

These tools are generic. They contain no legal, financial, case-management, or
document-approval semantics.

## Alternatives considered

**Keep using semantic and keyword searches as document discovery.** Rejected
because search recall is not an inventory. A search miss cannot prove that a
document is absent, and broad searches waste model iterations.

**Add a semantic duplicate guard.** Rejected because it is nondeterministic and
does not address the missing data surface. Exact catalog and coverage states
provide a stronger invariant without guessing intent.

**Read every document into the main Agent context.** Rejected because document
size and cross-document interference grow linearly. Per-document map-reduce
keeps the final synthesis context bounded.

**Leave exact arithmetic to model reasoning.** Rejected because decimal
rounding, percentages, and multi-step arithmetic are deterministic operations.
A bounded calculator reduces reasoning length and avoids silently divergent
results; choosing the inputs or business formula remains the caller's job.

**Keep collect-all summarization as one blocking tool call.** Rejected because
the user gets no actionable feedback for several minutes and one long call
delays every later result in the iteration. Bounded per-document progress and
per-tool result emission preserve the same map-reduce boundary without hiding
execution progress.

**Put document inventory in the business MCP server.** Rejected for the generic
knowledge-base path. Artoo owns its KB and session-file inventory; business MCP
servers may add their own domain inventory without changing Artoo.

**Expose every intermediate summary through a public API event.** Rejected
because the tool call and its bounded summary are sufficient for the Agent loop.
No public SSE or persistence contract needs to change.

## Consequences

Collection tasks now start from an authoritative document catalog, can account
for failed or unavailable documents, and can distinguish complete from partial
reads. Multi-document summaries process each document in an isolated context
and return compact per-document results to the main loop. Repeated summaries
can reuse version-keyed cache entries, and exact arithmetic stays outside the
reasoning channel.

Custom presets that explicitly whitelist tools must add
`list_knowledge_documents`, `read_document`, `summarize_documents`, and
`calculate`. Existing presets without an `allowed_tools` list continue to see
the complete tool set. The built-in smart-reasoning preset is synchronized to
this list automatically; custom presets remain owner-controlled. Map-reduce
adds model calls inside the tool, but those calls replace larger main-loop
context growth and avoid repeated broad searches. Redis is an optional
acceleration path, not a correctness dependency.

The Open API gains an additive `tool_progress` event and
`complete.first_answer_ms`; `complete.total_duration_ms` now reports real total
elapsed time. Old clients can ignore progress events. Custom preset owners
still control whether the summarize tool is visible.

Open API request and SSE event structures are unchanged. The new tool results
remain ordinary Agent tool observations with structured `ToolResult.data`.

## Testing

`backend/tests/test_generic_kb_agent_data_plane.py` covers authorized document
listing, pagination, session-file inclusion, parent-only reads, cross-page
coverage, stable evidence identity, the bounded empty-retrieval inventory
fallback, deterministic Decimal calculation, cache-key invalidation, and
per-document map-reduce summarization. The regression suite also verifies that
legacy built-in preset rows are upgraded without discarding unrelated config
keys, and that preloaded summary skills prefer the catalog and document tools.
Summary tests assert that thinking is disabled for per-document model calls and
that document progress is emitted before the final tool result.

`backend/benchmarks/generic_kb_agent_ab.py` runs against local PostgreSQL KBs.
On a 7-document local KB, search-only discovery used 4 search calls and 11
queries to find 6 readable documents, while the catalog used 1 call to identify
6 readable and 1 failed document before summarizing all 6 readable documents.
The deterministic map-reduce path reduced 9,977 parent characters to 1,157
summary characters (88.4%), and a second identical summary pass produced 6
cache hits with no additional map calls.
On a 48-document KB, search-only discovery found 38 documents without exposing
2 failures; the catalog identified all 46 available documents and the
map-reduce path summarized all 46, reducing parent content from 154,304 to
10,038 characters (93.5%). The second identical pass produced 46 cache hits
with no additional map calls.
