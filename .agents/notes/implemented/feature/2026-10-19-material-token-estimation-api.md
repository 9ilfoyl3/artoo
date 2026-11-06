# Agent Note: Material token estimation API

Status: implemented

## Problem

Third-party systems needed to estimate the size of knowledge-base material
before sending it to another model, billing system, or workflow. They could
enumerate documents and call the file-content endpoint for each one, but that
forced N+1 HTTP calls and moved tokenizer choice and chunk-assembly rules into
every client. Whole-KB estimates also needed to respect the same KB read
authorization as document listing while avoiding duplicate child-chunk text.

## Decision

Artoo exposes `POST /api/materials/token-estimate` on the existing protected
API surface. The request must choose exactly one scope:

- `knowledge_base_id` estimates every document visible to the caller in that
  knowledge base;
- `file_ids` estimates up to 1000 explicitly selected knowledge-base document
  ids, de-duplicated while preserving request order.

`file_ids` is limited to formal `Document.id` values. Session temporary files
continue to use their dedicated read endpoints.

The service reuses the parent-chunk projection from
`GET /api/files/{file_id}/content`: parent rows are ordered by `chunk_index`
and joined with `\n\n`; child rows are excluded because they duplicate context.
It reports parsed Python character count and `tiktoken` `cl100k_base` token
count. If `tiktoken` cannot load, the existing `TokenEstimator` fallback is
used and the response sets `fallback=true`.

Every response is stable under a `result` envelope with aggregate totals and a
per-file breakdown. Each requested KB is authorized with `KbAccessEnum.READ`;
missing, cross-tenant, or unauthorized scopes return `404`. Readers only see
non-completed documents that document listing would also hide, while writers
receive requested or KB-listed documents with zero length until their parsed
content exists.

## Alternatives considered

**Require clients to call the existing file-content endpoint and tokenize
locally.** Rejected because it creates an N+1 request pattern, duplicates
chunk-assembly rules, and allows inconsistent tokenizer choices across
integrators.

**Estimate from raw file bytes or `file_size`.** Rejected because PDFs,
office files, images, and audio have binary sizes unrelated to the parsed
material length used downstream.

**Expose separate KB-level and file-level endpoints.** Rejected because one
scope-discriminated request is smaller to document, easier for third-party
clients to call, and keeps aggregate response semantics consistent.

**Allow `knowledge_base_id` and `file_ids` together as a filter.** Rejected
because it makes "all docs in the KB" versus "only these files" ambiguous and
creates a second authorization path with little practical value.

## Consequences

Clients get one deterministic estimate request and an explicit aggregate
object suitable for programmatic use. The endpoint does not persist estimates
or trigger parsing, so results track the current indexed text and may change
after retry or reindex.

Tokenization is CPU work. The implementation streams parent chunks and keeps
only one document's text at a time, but a whole-KB response still contains one
breakdown row per visible document. Very large KBs may produce a large JSON
response and consume CPU proportional to indexed text length. `fallback=true`
signals environments where the result is only a character-based approximation.

## Testing

`backend/tests/test_material_token_estimate_api.py` covers whole-KB estimation,
selected-file ordering and de-duplication, parent-only text assembly,
non-completed documents, scope validation, and not-found semantics.
