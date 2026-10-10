"""Regression tests for PDF pages containing both a scan image and a text layer."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import fitz
import pytest

from app.pipeline.loader import LoadResult
from app.pipeline.loaders.pdf_loader import PdfLoader
from app.pipeline.ocr.provider import OCRResult, PageOCRResult
from app.pipeline.pipeline import DocumentPipeline


NATIVE_TEXT = (
    "Evidence list plaintiff print page count 1 evidence one evidence two evidence three "
    "borrower principal interest penalty and legal fees are listed on this page."
)
OCR_TEXT = (
    "Evidence list plaintiff print page count 1 evidence one evidence two evidence three "
    "borrower principal interest penalty and legal fees are listed on this page"
)


class FakeOCRManager:
    """Minimal OCR manager that records calls and returns deterministic test text."""

    def __init__(self) -> None:
        self.calls = 0

    async def recognize(self, file_path: str) -> OCRResult:
        self.calls += 1
        return OCRResult(
            full_text=OCR_TEXT,
            pages=[PageOCRResult(page_num=1, blocks=[], full_text=OCR_TEXT)],
            avg_confidence=0.98,
            provider_name="fake",
        )


def _image_png() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_textbox(
        fitz.Rect(40, 60, 560, 300),
        NATIVE_TEXT,
        fontsize=18,
    )
    image = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")
    doc.close()
    return image


def _make_pdf(
    path: Path,
    *,
    image_rect: fitz.Rect,
    text_layer: str = "",
) -> None:
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_image(image_rect, stream=_image_png())
    if text_layer:
        page.insert_textbox(
            fitz.Rect(40, 60, 560, 500),
            text_layer,
            fontsize=12,
            render_mode=3,
        )
    doc.save(path)
    doc.close()


def _pipeline_with_ocr(manager: FakeOCRManager) -> DocumentPipeline:
    pipeline = object.__new__(DocumentPipeline)
    pipeline.ocr_manager = manager
    return pipeline


@pytest.mark.asyncio
async def test_full_page_scan_with_text_layer_is_not_ocr_duplicated(tmp_path):
    """The hidden text layer wins; the full-page scan image must not be appended again."""
    path = tmp_path / "hybrid-scan.pdf"
    _make_pdf(path, image_rect=fitz.Rect(0, 0, 600, 800), text_layer=NATIVE_TEXT)

    loaded = PdfLoader().load(str(path))
    try:
        assert len(loaded.images) == 1
        assert loaded.images[0].is_page_background is True

        current_ocr = FakeOCRManager()
        current_merged = await _pipeline_with_ocr(current_ocr)._process_embedded_images(
            loaded, "current"
        )
        assert current_merged.strip() == loaded.content.strip()
        assert "[图片内容]" not in current_merged
        assert current_ocr.calls == 0

        # Old behavior control: forcing the page background to a content image reproduces
        # the duplicate output that this regression fixes.
        legacy_image = replace(loaded.images[0], is_page_background=False)
        legacy_result = LoadResult(
            content=loaded.content,
            metadata=loaded.metadata,
            images=[legacy_image],
            page_texts=loaded.page_texts,
            page_blocks=loaded.page_blocks,
        )
        legacy_ocr = FakeOCRManager()
        legacy_merged = await _pipeline_with_ocr(legacy_ocr)._process_embedded_images(
            legacy_result, "legacy"
        )
        assert legacy_merged.count("[图片内容]") == 1
        assert legacy_merged.count(OCR_TEXT) == 1
        assert legacy_ocr.calls == 1
    finally:
        DocumentPipeline._cleanup_image_temp_dirs(loaded.images)


@pytest.mark.asyncio
async def test_small_embedded_image_still_uses_ocr(tmp_path):
    """A normal figure on a text page keeps the existing OCR-and-append behavior."""
    path = tmp_path / "text-with-figure.pdf"
    _make_pdf(path, image_rect=fitz.Rect(100, 200, 300, 400), text_layer=NATIVE_TEXT)

    loaded = PdfLoader().load(str(path))
    try:
        assert len(loaded.images) == 1
        assert loaded.images[0].is_page_background is False

        ocr = FakeOCRManager()
        merged = await _pipeline_with_ocr(ocr)._process_embedded_images(
            loaded, "small-image"
        )
        assert "[图片内容]" in merged
        assert OCR_TEXT in merged
        assert ocr.calls == 1
    finally:
        DocumentPipeline._cleanup_image_temp_dirs(loaded.images)


def test_pure_scan_image_is_not_marked_as_text_backed(tmp_path):
    """A scan without a text layer remains eligible for the full-document OCR path."""
    path = tmp_path / "pure-scan.pdf"
    _make_pdf(path, image_rect=fitz.Rect(0, 0, 600, 800))

    loaded = PdfLoader().load(str(path))
    try:
        assert loaded.content.strip() == ""
        assert len(loaded.images) == 1
        assert loaded.images[0].is_page_background is False
    finally:
        DocumentPipeline._cleanup_image_temp_dirs(loaded.images)


@pytest.mark.asyncio
async def test_full_page_figure_with_single_line_caption_still_uses_ocr(tmp_path):
    """A dense page figure with only a caption remains a content image."""
    caption = "Illustration " * 7
    path = tmp_path / "full-page-figure.pdf"
    _make_pdf(path, image_rect=fitz.Rect(0, 0, 600, 800), text_layer=caption)

    loaded = PdfLoader().load(str(path))
    try:
        assert len(loaded.images) == 1
        assert loaded.images[0].is_page_background is False

        ocr = FakeOCRManager()
        merged = await _pipeline_with_ocr(ocr)._process_embedded_images(
            loaded, "full-page-figure"
        )
        assert "[图片内容]" in merged
        assert ocr.calls == 1
    finally:
        DocumentPipeline._cleanup_image_temp_dirs(loaded.images)
