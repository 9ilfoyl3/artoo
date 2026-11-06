# Agent Note: DOCX structured extraction and page-level OCR

Status: implemented

## Problem

The DOCX loader read only `Document.paragraphs`, which excludes table cells and other
WordprocessingML stories. A court service receipt containing a 15-row table was therefore
reduced to its title paragraphs. Simply extracting tables was insufficient for documents
with images: image OCR was appended after existing text, producing multiple competing text
sources and no stable page boundary.

## Decision

`DocxLoader` uses two source-selection paths.

Documents without images in the main document story use native DOCX structure directly.
The loader extracts paragraphs, tables, headers, footers, content controls, and text boxes
in body order. Tables become Markdown rows so field/value relationships stay visible to
retrieval. Tabs and soft line breaks are preserved.

Documents with a main-story image are rendered to PDF through LibreOffice to recover page
boundaries. A page with a meaningful body image becomes one full-page PNG snapshot and is
marked `is_page_snapshot`. `DocumentPipeline._process_page_snapshots` sends those snapshots
to OCR and replaces the corresponding page text. Pages without body images keep their PDF
text layer. The pipeline does not append local image OCR text, does not run whole-document
OCR after page snapshots succeed, and falls back to the text layer only when a page snapshot
returns no text.

An image resource retained in a page's resource dictionary without a painted rectangle is
ignored. LibreOffice can reuse an image XObject on later text-only pages; treating that
resource as visible content would incorrectly OCR those pages.

Header/footer artwork, thumbnails, and images referenced only outside the main document
story do not select the page-OCR path. If LibreOffice is unavailable or rendering fails,
text-bearing DOCX files retain the native structured path. Image-only DOCX files fall back
to embedded-image extraction.

## Alternatives considered

**Always convert DOCX to PDF and use PDF extraction.** Rejected because it adds a rendering
round trip to every text document and loses native table/content-control semantics.

**Append OCR text from embedded images to structured text.** Rejected because a page with
both a text layer and an image would contain two competing sources, with no reliable way to
deduplicate table ordering or punctuation.

**OCR every page of every image-bearing DOCX.** Rejected because a single logo or signature
image would make all text pages pay OCR cost and replace accurate native text.

**Use `Document.paragraphs` plus `Document.tables` separately.** Rejected because it loses
their relative order and does not cover content controls or text boxes.

## Consequences

Service forms and other table-heavy Word documents now retain their field-level content.
Text-only DOCX files avoid LibreOffice and OCR entirely. Image-bearing documents pay
LibreOffice plus OCR only for body-image pages, and each such page has one authoritative
result.

The heuristic can treat a body decoration crossing the header/footer band as a content
image; the affected page then uses whole-page OCR. Documents whose images live only in
headers/footers stay on the structured path. Markdown tables flatten repeated merged-cell
values rather than reproducing Word's visual cell spans.

`EmbeddedImage.is_page_snapshot` is internal pipeline state. It does not change the Open
API, persisted chunk schema, or cleanup contract.

## Testing

`backend/tests/test_docx_loader.py` covers table/body ordering, merged cells, tabs, soft
breaks, content controls, body-image page selection, whole-page replacement, image-only
page snapshots and fallback, unpainted image resources, and the absence of LibreOffice
conversion for text-only DOCX files. A LibreOffice `.docx → .doc → DocLoader` round trip
also verifies merged-cell table preservation. The real `11-送达回证_第三人.doc` sample is
checked through LibreOffice conversion and yields the complete table rather than only its
title paragraphs.
