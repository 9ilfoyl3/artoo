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

The `动火作业票（测试1）.pdf` incident exposed a second routing gap. Pages 1 and 2
contained only 6 and 5 native characters, while their printed form fields were visual
content and their handwritten signatures were 16 small embedded images. The document
had more than 10 native characters and no full-page scan image, so the pipeline skipped
whole-page OCR and only OCR'd the signatures separately, producing fragments wrapped
as `[图片内容]`.

## Decision

PDF loaders now record each page's real height in `LoadResult.page_heights`, and
`TextCleaner` uses those heights when filtering header and footer blocks. If page
heights are unavailable, the cleaner skips bbox filtering rather than applying an A4
assumption. It also returns the original content when cleaning retains less than 30%
of the non-whitespace characters.

PDF loaders also record `page_has_usable_text` per page. The pipeline selects only
pages that lack a usable text layer and contain image content, then asks the OCR
manager to process those page numbers. Image-only providers render only the selected
pages; providers that accept PDF directly may still receive the whole file, but only
the requested pages are merged. OCR page numbers are preserved through
`PreparedInput` and `OCRManager` rather than being renumbered from one.

Selective OCR replaces only pages whose OCR result is non-empty. Native text pages and
failed OCR pages keep their original text. Embedded images on successfully OCR'd pages
are removed from the embedded-image pass, while images on native text pages still use
the existing OCR-and-append behavior. `TextCleaner` preserves OCR page text when its
page block list is empty instead of rebuilding that page as blank.

For other hybrid scan pages, the existing low-quality hidden-text-layer rule remains:
OCR is preferred when a full-page background image is paired with fragmented
single-character ASCII alphanumeric tokens. If OCR returns no text anywhere, the
original PDF text layer remains the fallback.

## Alternatives considered

**Always OCR every hybrid text layer.** Rejected because the existing deduplication
decision intentionally avoids duplicate OCR for usable layers, and unconditional OCR
would add cost and latency to normal scanned PDFs.

**Run whole-document OCR whenever any page has a sparse text layer.** Rejected because
a single problematic page in a long native PDF would re-OCR every healthy page and
replace native text with a less precise OCR result.

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

Sparse pages with images, including form pages whose only images are signatures, now
enter selective whole-page OCR without re-OCR'ing healthy text pages. Pure short text
pages without images, genuinely blank pages, and pages with usable text remain on the
existing path. The cleaner becomes less aggressive for missing or abnormal dimensions,
so some header or footer noise may remain instead of risking data loss. Existing
`LoadResult` constructors remain compatible because the new page metadata defaults to
empty lists.

## Testing

Regression tests cover actual non-A4 page heights, missing-height safety, the 30%
content-loss guard, normal A4 header/footer removal, fragmented-text detection, and
the full-page-background precondition. Additional tests cover page-level usable-text
flags, selective page selection, preservation of native pages during merge, rendering
only requested PDF pages, original OCR page-number restoration, and cleaner behavior
for OCR pages without blocks. `20_机动车违法查询.pdf` loads with page height `2483.0`,
is detected as low-quality hybrid text, and retains `441` characters after cleaning
instead of the previous 42-character fragment. `动火作业票（测试1）.pdf` selects pages
1 and 2 for OCR while leaving blank page 3 untouched.
