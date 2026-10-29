# Agent Note: Empty document ingestion success

Status: implemented

## Problem

Files whose extracted text was empty after load, OCR, ASR, and cleaning entered the
normal exception path as `EmptyDocumentContentError`. Knowledge-base ingestion retried
those documents and eventually persisted `status=failed` with an OCR-oriented error,
even though an empty file is valid input rather than a processing failure.

## Decision

Once extraction has completed and the pipeline confirms that the document has no text,
`DocumentPipeline.process_to_vectors` returns a valid zero-chunk `ProcessedDocument`
marked with internal metadata `empty_content=True`. It returns before chunking,
embedding, or indexing, so no placeholder text or vector consumes capacity or pollutes
retrieval.

Knowledge-base ingestion then completes normally with `status=completed`,
`chunk_count=0`, `progress=100`, and no `error_message`. OCR and ASR are still attempted
when configured; a missing ASR provider for an audio file remains an error because the
pipeline cannot determine whether that file is empty.

Session-file ingestion accepts a zero-chunk result only when the pipeline marked it as
an explicit empty document. A non-empty document that unexpectedly produces zero chunks
continues to raise `EmptyDocumentContentError`, preserving chunker integrity checks.
Graph extraction and summary scheduling are skipped for zero-chunk documents because
there is no document text to process.

## Alternatives considered

**Continue rejecting empty documents with HTTP 422.** Rejected because it treats valid
empty input as a processing failure and causes unnecessary retries and operator noise.

**Insert a placeholder chunk.** Rejected because synthetic text would consume chunk and
vector capacity and could appear in retrieval results as if it were document content.

**Add a new persisted `empty` document status.** Rejected because `completed` plus
`chunk_count=0` already represents the outcome without changing the database or Open API
contract.

**Accept every zero-chunk result without the `empty_content` marker.** Rejected because
it would hide chunker defects on documents that did contain text.

## Consequences

Empty PDFs, Office files, text files, images, and successfully transcribed silent audio
are visible as completed documents with zero chunks. They produce no search hits and no
graph or summary artifacts. The marker is internal pipeline metadata and does not change
the public document response schema.

Previously failed documents are not migrated automatically; users must retry them if
they want their status changed. Missing extraction dependencies can still fail, because
the pipeline has not established that the source is empty in those cases.

## Testing

`backend/tests/test_pipeline_empty_document.py` covers empty extraction with and without
OCR, zero-chunk processing, knowledge-base status persistence, and completed session
files with a zero-count completed event. The complete session-upload test group plus the
new pipeline regression test passed with `79 passed`.
