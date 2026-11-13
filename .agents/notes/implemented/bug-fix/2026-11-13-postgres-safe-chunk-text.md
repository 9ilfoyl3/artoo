# Agent Note: PostgreSQL-safe chunk text

Status: implemented

## Problem

PDF extraction and OCR can produce strings containing U+0000 even when the surrounding
text is valid UTF-8. Python, embeddings, and Milvus accept that character, but PostgreSQL
`text` and JSON parameters reject it. The failure surfaced only during SQLAlchemy's
query-invoked autoflush as `CharacterNotInRepertoireError: invalid byte sequence for
encoding "UTF8": 0x00`, after expensive parsing, chunking, embedding, and vector writes
had already completed. Large reference documents reproduced the failure consistently.

## Decision

`DocumentPipeline.process_to_vectors` treats U+0000 as a storage-safety issue rather than
an optional text-cleaner quality preference. It removes NUL characters after all loader,
OCR, ASR, OCR-image merge, and cleaner paths converge, before empty-document handling,
chunking, embedding, and index construction.

The pipeline also sanitizes parent chunks, child chunks, context headers, and enriched
child text after chunking and enrichment. Chunk insertion into PostgreSQL and content
insertion into Milvus apply the same sanitizer as a final defensive boundary. Newlines,
tabs, and all other valid characters are preserved. Each removal is logged with the
source kind, source ID, and count so a broken extractor remains observable instead of
silently corrupting text.

## Alternatives considered

**Drop NUL only in the SQLAlchemy INSERT path.** Rejected because the invalid character
would still reach embedding inputs and metadata extraction, and session or future storage
paths could bypass the same insert code.

**Reject documents containing NUL and mark them failed.** Rejected because the surrounding
text is valid and useful; a single extractor artifact should not make an otherwise
searchable document permanently unavailable.

**Replace every control character with whitespace.** Rejected because PostgreSQL accepts
the other control characters, and changing them would alter valid document structure
beyond the failure being fixed.

**Depend on the knowledge-base `enable_cleaner` option.** Rejected because disabling
denoising must not disable a storage-safety invariant shared by every pipeline path.

## Consequences

Previously failing documents can be retried successfully without changing their source
files. NUL removal can join text on either side of an extractor artifact, but this is the
least destructive safe representation and the warning provides an audit trail. Retrieval
and embedding see the same sanitized content that is persisted. New loaders, OCR
providers, and enrichment steps remain protected by the post-chunk and storage-boundary
checks.

## Testing

`backend/tests/test_pipeline_text_safety.py` verifies that only NUL is removed while
newlines and tabs remain intact, that a NUL-only document completes as an empty document,
and that NUL injected by a chunker is removed from parent chunks, child chunks, context
headers, metadata, and embedding input.

Verification passed:

- `../.venv/bin/python -m pytest tests/test_pipeline_text_safety.py -q` (3 passed)
- `../.venv/bin/python -m pytest tests/test_pipeline_empty_document.py tests/test_pdf_scan_text_layer.py tests/test_pipeline_text_safety.py -q` (23 passed)
- `../.venv/bin/python -m pytest tests/test_session_upload_process_task.py tests/test_session_upload_integration.py tests/test_pipeline_text_safety.py -q` (19 passed)
- `../.venv/bin/python -m compileall -q app/pipeline/cleaner.py app/pipeline/pipeline.py tests/test_pipeline_text_safety.py`
