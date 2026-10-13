# Agent Note: PDF scan text-layer OCR deduplication

Status: implemented

## Problem

Scanned PDFs commonly contain a full-page raster image plus a hidden OCR text layer. The
PDF loader extracted both the page text layer and the embedded page image. Although the
pipeline skipped whole-document OCR when extracted text was already present, it still sent
every embedded image through OCR and appended the result as `[图片内容]`.

Hybrid scan pages therefore produced two versions of the same page in one document. The
text layer and the new OCR result could differ in spacing, table ordering, or numbers
because they came from different OCR passes over different representations.

## Decision

Use page-level source mutual exclusion for PDF scan backgrounds. `PdfLoader` marks an
embedded image as `is_page_background` only when all of these conditions hold:

- the page has at least 80 characters in its extracted text layer;
- text block area covers at least 1% of the page;
- text blocks span at least 3% of the page height;
- the displayed image covers at least 70% of the page;
- at least 60% of text-layer characters are inside the image area.

`DocumentPipeline._process_embedded_images` excludes page-background images from embedded
image OCR. The hidden text layer remains the single authoritative text for that page.

Pure scans and ordinary embedded images are unchanged: when a page has no usable text
layer, the scan image remains eligible for the existing whole-document OCR path; when an
image is a normal figure or screenshot, it still goes through OCR and is appended.

## Alternatives considered

**Fuzzy-dedupe the native text and OCR text after both exist.** Rejected because OCR
engines differ in spacing, reading order, and table reconstruction, so similarity
thresholds are brittle and still pay the OCR cost.

**Skip every embedded image OCR whenever a page has any text.** Rejected because mixed
pages intentionally extract useful text from charts, screenshots, and figures.

**Always re-OCR the full page and discard the embedded text layer.** Rejected because
existing text layers are often accurate and this would add cost while replacing a
known-good source with an unverified one.

## Consequences

Hybrid scan pages no longer duplicate their content and avoid an unnecessary OCR request.
The heuristic is deliberately conservative, so pages that do not meet every threshold
retain the old behavior. Low-quality text layers that still meet the thresholds are trusted;
there is no page-level quality override or OCR confidence comparison in this change.

`EmbeddedImage.is_page_background` is an internal pipeline field. It does not change the
Open API, persisted chunk schema, document metadata contract, or cleanup behavior.

## Testing

`backend/tests/test_pdf_scan_text_layer.py` builds synthetic PDFs and verifies:

- a full-page scan image with a usable text layer is not OCR'd again;
- the legacy forced behavior still demonstrates the duplicate control output;
- a small embedded image on a text page is still OCR'd and appended;
- a full-page figure with only a one-line caption is still OCR'd;
- a pure scan image remains eligible for full-document OCR.
