# Agent Note: DOC WordDocument payload recovery

Status: implemented

## Problem

Some office systems export a `.doc` file as an OLE Compound File whose only
`WordDocument` stream contains HTML rather than a Word binary. The stream has no
valid Word FIB magic, so LibreOffice rejects the file with `source file could
not be loaded`. `DocLoader` surfaced only a generic DOC-to-DOCX conversion
error, even though the document contained a complete machine-readable text
payload.

Sending the whole document to OCR would be both slower and less accurate for
these text-native files. Adding a separate HTML loader or changing pipeline
routing would duplicate the existing DOCX text, table, image, and page-level
OCR paths.

## Decision

`DocLoader` keeps the native LibreOffice DOC-to-DOCX conversion as the primary
path. Only after that conversion fails does it inspect the OLE root
`WordDocument` stream. Recovery requires all of the following:

- the file starts with the OLE Compound File signature;
- the root `WordDocument` stream exists and is at most 64 MiB;
- the stream does not start with the valid Word FIB magic `0xA5EC`;
- the stream prefix identifies HTML or RTF content.

A recognized payload is written to a temporary file with the matching
extension. HTML is recovered through LibreOffice's `HTML (StarWriter)` input
filter and both HTML and RTF use the explicit `Office Open XML Text` output
filter. The resulting DOCX is passed through the existing `DocxLoader`, so
tables, embedded images, and page-level OCR behavior remain unchanged.

The recovery subprocess receives a blocked HTTP(S) proxy environment. UTF-8
BOMs in recovered HTML are removed before conversion so they cannot become
visible body text. Recovered documents retain their original filename,
`file_type=doc`, and file size, and gain `doc_recovery` and
`doc_recovery_stream` metadata.

LibreOffice conversion failures now retain bounded stderr/stdout and timeout
diagnostics. A `source file could not be loaded` result is treated as
deterministic and is not retried three times.

## Alternatives considered

**Force the original OLE bytes through the HTML input filter directly.** Rejected
because LibreOffice treats container bytes as document content and emits
corrupted leading paragraphs.

**Create a new HTML loader and reroute the pipeline.** Rejected because the
conversion is a recovery concern inside legacy DOC loading, while the existing
DOCX path already owns text, tables, images, and page-level OCR.

**OCR the original DOC or rendered pages unconditionally.** Rejected because the
files already contain exact text; OCR adds cost and risks errors in case numbers,
names, amounts, and punctuation.

**Adopt Tika or another JVM parser solely for this fallback.** Rejected because it
adds a larger runtime and a second parser architecture. The useful principle is
container-aware payload sniffing, which this change implements locally.

**Inspect every DOC before attempting native conversion.** Rejected because valid
DOC files remain the overwhelmingly common case and native conversion is
already the best path for them.

## Consequences

Mislabeled OLE-HTML and OLE-RTF DOC files recover into the normal DOCX pipeline
without a special upload endpoint or OCR detour. Unknown or genuinely corrupted
DOC files still fail with conversion diagnostics. The recovery path costs one
extra OLE inspection only after native conversion failure and is bounded by the
64 MiB stream limit.

The backend now depends on the dependency-free Python package `olefile`.
Recovery is intentionally conservative: it does not scan arbitrary OLE streams,
does not attempt arbitrary text decoding, and does not add untested input
filters. Further payload formats require a documented detector and tested
LibreOffice filter mapping.

## Testing

`backend/tests/test_doc_loader.py` covers native success, native failure
diagnostics, deterministic no-retry behavior, filtered conversion commands,
blocked-network environment, HTML/RTF detection, FIB rejection, BOM removal,
metadata preservation, and recovery failures.

Two external DingTalk-exported DOC samples were parsed through the complete
`DocLoader` path. Both were recovered as embedded HTML, produced 152 and 8335
characters respectively, remained `file_type=doc`, and required no OCR. A
separate LibreOffice HTML fixture with a base64 PNG produced `word/media` in the
generated DOCX, and an extracted RTF payload converted with clean text.
