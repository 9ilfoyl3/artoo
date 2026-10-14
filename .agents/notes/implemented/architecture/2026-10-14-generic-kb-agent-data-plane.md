# Agent Note: Generic knowledge-base Agent data plane

Status: implemented

## Problem

The Agent had search and read tools but no deterministic document-discovery
surface. Collection requests therefore depended on broad searches and sometimes
inferred missing files from filename numbering. The formal-document read tool
also counted and returned both parent and child chunks, so repeated text could
enter the model context, and no state recorded whether a document had been read
completely.

The problem is domain-neutral. It affects any knowledge base containing a set
of documents that must be inventoried, summarized, audited, or covered. Business
specific facts, calculations, document anchors, and approval rules do not belong
in this layer.

## Decision

The generic knowledge-base Agent gains three deterministic data-plane surfaces.

`list_knowledge_documents` enumerates the authorized formal KBs and optional
session-file source. It returns stable `doc_id`, filename, type, status, source,
KB ID, and indexed chunk count, with pagination. Results are stored in
`AgentState.document_catalog`.

`list_knowledge_chunks` reads formal documents through parent chunks only,
matching the document-preview path and avoiding parent/child duplication. It
returns `has_more`, `next_page`, `read_chunks`, and `fully_read`, and records
read ranges in `AgentState.document_read_ranges`.

`summarize_documents` is a domain-neutral map-reduce tool. Each selected
document is loaded and summarized independently. Oversized documents are split
into deterministic character batches, summarized, and reduced. Concurrency is
bounded, each document is isolated from the others, and the result carries the
stable document identity and full-read status.

For collection-oriented requests, the system prompt directs the model to list
documents before broad searches, use only returned doc_ids, never infer files
from numbering, and report unread or unavailable documents. If retrieval has
already exhausted its bounded rewrite opportunity and still produces no new
evidence, the loop deterministically points the model to the document catalog.

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
and return compact per-document results to the main loop.

Custom presets that explicitly whitelist tools must add the new tool names.
Existing presets without an `allowed_tools` list continue to see the complete
tool set. Map-reduce adds model calls inside the tool, but those calls replace
larger main-loop context growth and avoid repeated broad searches.

Open API request and SSE event structures are unchanged. The new tool results
remain ordinary Agent tool observations with structured `ToolResult.data`.

## Testing

`backend/tests/test_generic_kb_agent_data_plane.py` covers authorized document
listing, pagination, session-file inclusion, parent-only reads, cross-page
coverage, the bounded empty-retrieval inventory fallback, and per-document
map-reduce summarization.

`backend/benchmarks/generic_kb_agent_ab.py` runs against local PostgreSQL KBs.
On a 7-document local KB, search-only discovery used 4 search calls and 11
queries to find 6 readable documents, while the catalog used 1 call to identify
6 readable and 1 failed document before summarizing all 6 readable documents.
On a 48-document KB, search-only discovery found 38 documents without exposing
2 failures; the catalog identified all 46 available documents and the
map-reduce path summarized all 46, reducing parent content from 154,304 to
10,038 characters in the deterministic benchmark.
