# Agent Note: PDF hidden text-layer quality recovery

Status: implemented

## Problem

Scanned PDFs can contain a full-page image plus a hidden text layer. The existing
[PDF scan text-layer deduplication](./2026-09-23-pdf-scan-text-layer-deduplication.md)
decision correctly avoided running OCR on that image when the text layer was usable.
However, `TextCleaner` assumed every page was A4 height (`842pt`). Wide or otherwise
non-standard pages can therefore have most of their text treated as footer content and
deleted.

The `20_机动车违法查询.pdf` incident exposed both problems. Its page is `3798 x 2483pt`,
but the cleaner used an `842pt` height, removed every short block below `799.9pt`, and
reduced `474` characters to the malformed fragment
`机动车违法查询\n号牌种类0 2 小型汽车只脑三部A 2 8 8 W B6 0 3 1`.
The hidden text layer also fragmented ASCII identifiers into single-character tokens.
A page-wise Paddle OCR pass recovered the fields more accurately than the native text
layer, but the pipeline never called OCR because the layer was considered usable.

## Decision

PDF loaders now record each page's real height in `LoadResult.page_heights`, and
`TextCleaner` uses those heights when filtering header and footer blocks. If page
heights are unavailable, the cleaner skips bbox filtering rather than applying an A4
assumption. It also returns the original content when cleaning retains less than 30%
of the non-whitespace characters.

For hybrid scan pages, the pipeline prefers OCR when the hidden text layer is both
backed by a full-page background image and fragmented as single-character ASCII
alphanumeric tokens. If OCR returns no text, the original PDF text layer remains the
fallback. If OCR succeeds, its text becomes the document content; PaddleOCR continues
to receive page images prepared by `input_prep`.

## Alternatives considered

**Always OCR every hybrid text layer.** Rejected because the existing deduplication
decision intentionally avoids duplicate OCR for usable layers, and unconditional OCR
would add cost and latency to normal scanned PDFs.

**Always keep the native text layer and only fix the page-height bug.** Rejected after
comparing outputs: the native layer preserved fields but contained fragmented numbers,
incorrect characters such as `黄波区` and `非警运`, reordered labels, and court
watermark text. Page-wise Paddle OCR produced cleaner and more accurate fields.

**Use the native text layer as the first fallback when OCR is empty.** Accepted. OCR is
preferred for quality, but the native layer is safer than replacing a non-empty
document with an empty OCR result.

**Keep the fixed A4 height assumption.** Rejected because the page dimensions in the
source PDF explicitly disprove it and because the same defect can affect other
non-standard scans.

## Consequences

Low-quality hidden text layers now incur page-wise OCR only when a full-page image and
fragmented ASCII text are present. The cleaner becomes less aggressive for missing or
abnormal dimensions, so some header or footer noise may remain instead of risking data
loss. Existing `LoadResult` constructors remain compatible because `page_heights`
defaults to an empty list.

## Testing

Regression tests cover actual non-A4 page heights, missing-height safety, the 30%
content-loss guard, normal A4 header/footer removal, fragmented-text detection, and
the full-page-background precondition. The affected PDF loads with page height
`2483.0`, is detected as low-quality hybrid text, and retains `441` characters after
cleaning instead of the previous 42-character fragment.
