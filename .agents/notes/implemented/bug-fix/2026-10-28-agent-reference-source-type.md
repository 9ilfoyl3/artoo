# Agent Note: Agent reference source type

Status: implemented

## Problem

Agent references could contain both formal knowledge-base documents and files
uploaded to the current chat session, but the reference item exposed only
`doc_id`, `chunk_id`, and `filename`. Both source kinds are identified by
`doc_id` in retrieval results, while their raw-file preview APIs differ:

- knowledge-base documents use `GET /api/documents/{doc_id}/raw`;
- session files use `GET /api/sessions/{session_id}/files/{doc_id}/raw`.

Without a source discriminator, a citation consumer could not safely choose the
correct preview endpoint. The public multi-source retrieval response already
solved this with `source_type`, but chat and Agent references did not expose the
same information.

## Decision

Every `ReferenceItem` now includes `source_type` with one of two values:

- `knowledge_base` when `doc_id` belongs to `documents`;
- `session` when `doc_id` belongs to `session_files`.

`_build_references` determines the value from the same table lookup used to
resolve filenames: formal documents are queried first, then unmatched IDs are
looked up in `session_files`. The field is included in non-streaming responses,
SSE `references` frames, `/api/retrieval/agent`, and persisted assistant-message
references. Its vocabulary matches `/api/retrieval/search`.

`source_type` defaults to `knowledge_base` on the response model for backward
compatibility. The session ID is not duplicated into every reference because it
is already the conversation identifier supplied by the caller and is available
to history consumers from the session endpoint.

## Alternatives considered

**Infer the source from whether `session_id` is present.** Rejected because a
session can search both a knowledge base and its attachments in the same turn,
so the request shape cannot classify individual references.

**Return a ready-to-use preview URL per reference.** Rejected because URLs would
couple the response contract to deployment routing and would need to encode
authorization or lifetime semantics that do not belong in a citation item.

**Collapse both source kinds behind a new raw-file endpoint.** Rejected because
the existing document and session-file raw endpoints already own distinct
authorization and storage paths; adding another proxy surface would not remove
the need for clients to understand the source.

**Use the tool-file vocabulary (`document` / `session-file`).** Rejected for
references because `/api/retrieval/search` already exposes
`knowledge_base` / `session`; keeping one public citation vocabulary avoids a
second mapping at every integration boundary.

## Consequences

Clients can select the correct preview API from a reference without another
metadata lookup. Mixed knowledge-base and session-file citations are classified
per item, not per request. No extra database round trip is introduced because
the source lookup reuses the existing filename-resolution query.

New responses always carry `source_type`. References persisted before this
change may omit it and consumers should treat the omission as
`knowledge_base`. The field is additive, so clients that ignore it continue to
read existing reference fields unchanged.

## Testing

`backend/tests/test_chat_reference_sources.py` verifies that a reference backed
by `documents` is marked `knowledge_base` and one backed by `session_files` is
marked `session`, while both retain their resolved filenames.
