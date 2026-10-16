"""Regression tests for PDF pages containing both a scan image and a text layer."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import fitz
import pytest

from app.pipeline.cleaner import TextCleaner, looks_like_fragmented_alnum_text
from app.pipeline.loader import EmbeddedImage, LoadResult
from app.pipeline.loaders.pdf_loader import PdfLoader
from app.pipeline.ocr.input_prep import PreparedInput, prepare_input
from app.pipeline.ocr.manager import OCRManager
from app.pipeline.ocr.provider import (
    INPUT_IMAGE,
    OCRCapability,
    OCRResult,
    PageOCRResult,
)
from app.pipeline.pipeline import (
    DocumentPipeline,
    _has_low_quality_hybrid_text_layer,
    _merge_page_ocr,
    _pdf_pages_requiring_ocr,
)


NATIVE_TEXT = (
    "Evidence list plaintiff print page count 1 evidence one evidence two evidence three "
    "borrower principal interest penalty and legal fees are listed on this page."
)
OCR_TEXT = (
    "Evidence list plaintiff print page count 1 evidence one evidence two evidence three "
    "borrower principal interest penalty and legal fees are listed on this page"
)
FRAGMENTED_TEXT = (
    "车辆识别代号：L G W E E 4 A 5 2 M K 0 3 0 9 5 2\n"
    "身份证明号码 4 2 0 1 1 6 1 9 9 3 0 1 0 1 4 1 8 9"
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


def _make_multipage_text_pdf(path: Path, page_count: int = 3) -> None:
    doc = fitz.open()
    for page_idx in range(page_count):
        page = doc.new_page(width=600, height=800)
        page.insert_textbox(
            fitz.Rect(72, 72, 528, 300),
            f"page {page_idx + 1} {NATIVE_TEXT}",
            fontsize=10,
        )
    doc.save(path)
    doc.close()


def _pipeline_with_ocr(manager: FakeOCRManager) -> DocumentPipeline:
    pipeline = object.__new__(DocumentPipeline)
    pipeline.ocr_manager = manager
    return pipeline


def test_bbox_filter_uses_actual_page_height() -> None:
    """A tall non-A4 page must not treat all content below 800pt as a footer."""
    blocks = [
        {"bbox": (0, 500, 100, 550), "text": "标题"},
        {"bbox": (0, 900, 100, 950), "text": "车辆数据字段"},
    ]

    filtered = TextCleaner()._filter_by_bbox([blocks], [2483.0])

    assert filtered == [blocks]


def test_bbox_filter_is_skipped_without_page_height() -> None:
    """Missing page dimensions are safer than applying the old A4 assumption."""
    blocks = [
        {"bbox": (0, 900, 100, 950), "text": "车辆数据字段"},
    ]

    filtered = TextCleaner()._filter_by_bbox([blocks], None)

    assert filtered == [blocks]


def test_massive_cleaner_loss_falls_back_to_original() -> None:
    content = "\n".join(f"字段{i}" for i in range(12))
    page_blocks = [
        [
            {"bbox": (0, 805 + index, 100, 812 + index), "text": f"字段{index}"}
            for index in range(12)
        ]
    ]

    cleaned = TextCleaner().clean(
        content=content,
        page_texts=[content],
        page_blocks=page_blocks,
        page_heights=[842.0],
    )

    assert cleaned == content


def test_standard_page_still_removes_header_and_footer() -> None:
    body = "正文内容" * 40
    content = f"页眉\n{body}\n页尾"
    page_blocks = [
        [
            {"bbox": (0, 10, 100, 30), "text": "页眉"},
            {"bbox": (0, 300, 500, 500), "text": body},
            {"bbox": (0, 820, 100, 840), "text": "页尾"},
        ]
    ]

    cleaned = TextCleaner().clean(
        content=content,
        page_texts=[content],
        page_blocks=page_blocks,
        page_heights=[842.0],
    )

    assert cleaned == body


def test_fragmented_alnum_text_detection() -> None:
    assert looks_like_fragmented_alnum_text(FRAGMENTED_TEXT) is True
    assert (
        looks_like_fragmented_alnum_text(
            "Evidence list plaintiff print page count 1 "
            "evidence one evidence two evidence three"
        )
        is False
    )


def test_low_quality_hybrid_text_layer_requires_page_background() -> None:
    background = EmbeddedImage(
        file_path="/tmp/page.png",
        format="png",
        is_page_background=True,
    )
    content_image = EmbeddedImage(
        file_path="/tmp/figure.png",
        format="png",
        is_page_background=False,
    )

    assert _has_low_quality_hybrid_text_layer(
        LoadResult(content=FRAGMENTED_TEXT, images=[background])
    )
    assert not _has_low_quality_hybrid_text_layer(
        LoadResult(content=FRAGMENTED_TEXT, images=[content_image])
    )
    assert not _has_low_quality_hybrid_text_layer(
        LoadResult(content="正常正文内容", images=[background])
    )


def test_pdf_loader_records_usable_text_per_page(tmp_path):
    text_pdf = tmp_path / "text.pdf"
    _make_multipage_text_pdf(text_pdf, page_count=2)
    loaded_text = PdfLoader().load(str(text_pdf))
    assert loaded_text.page_has_usable_text == [True, True]
    assert _pdf_pages_requiring_ocr(loaded_text, "pdf") == []

    native_pdf = tmp_path / "native.pdf"
    _make_pdf(
        native_pdf,
        image_rect=fitz.Rect(0, 0, 600, 800),
        text_layer=NATIVE_TEXT,
    )
    loaded_native = PdfLoader().load(str(native_pdf))
    try:
        assert loaded_native.page_has_usable_text == [True]
    finally:
        DocumentPipeline._cleanup_image_temp_dirs(loaded_native.images)


def test_selective_ocr_targets_sparse_pages_with_images_only():
    result = LoadResult(
        content="tiny\n\nnormal text\n\nshort",
        page_texts=["tiny", "normal text", "short"],
        page_has_usable_text=[False, True, False],
        images=[
            EmbeddedImage(file_path="/tmp/signature.png", format="png", page_or_index=1),
            EmbeddedImage(file_path="/tmp/figure.png", format="png", page_or_index=2),
        ],
    )

    assert _pdf_pages_requiring_ocr(result, "pdf") == [1]
    assert _pdf_pages_requiring_ocr(result, "docx") == []


def test_merge_page_ocr_replaces_only_recognized_pages():
    result = LoadResult(
        content="sparse native\nnormal native",
        page_texts=["sparse native", "normal native"],
        page_has_usable_text=[False, True],
    )
    ocr_result = OCRResult(
        full_text="full page one",
        pages=[PageOCRResult(page_num=1, blocks=[], full_text="full page one")],
        avg_confidence=0.99,
        provider_name="fake",
    )

    merged_pages, recognized = _merge_page_ocr(result, ocr_result, [1])

    assert merged_pages == ["full page one", "normal native"]
    assert recognized == {1}


def test_cleaner_preserves_ocr_page_when_blocks_are_empty():
    content = "full page one\nnormal text"
    cleaned = TextCleaner().clean(
        content=content,
        page_texts=["full page one", "normal text"],
        page_blocks=[[], [{"bbox": (0, 300, 100, 400), "text": "normal text"}]],
        page_heights=[842.0, 842.0],
    )

    assert cleaned == content


def test_input_prep_renders_only_requested_pdf_pages(tmp_path):
    path = tmp_path / "three-pages.pdf"
    _make_multipage_text_pdf(path, page_count=3)
    prepared = prepare_input(
        str(path),
        OCRCapability(
            accepts=frozenset({INPUT_IMAGE}),
            outputs_markdown=False,
            paginated=False,
            recommended_timeout=30.0,
        ),
        page_numbers=[1, 3],
    )
    try:
        assert prepared.kind == "pages"
        assert prepared.page_numbers == [1, 3]
        assert [Path(p).name for p in prepared.paths] == [
            "page0001.png",
            "page0003.png",
        ]
    finally:
        prepared.cleanup()


class FakePageProvider:
    name = "fake-page"
    capability = OCRCapability(
        accepts=frozenset({INPUT_IMAGE}),
        outputs_markdown=False,
        paginated=False,
        recommended_timeout=30.0,
    )

    async def recognize(self, file_path: str) -> OCRResult:
        return OCRResult(
            full_text="recognized",
            pages=[PageOCRResult(page_num=1, blocks=[], full_text="recognized")],
            avg_confidence=0.9,
            provider_name=self.name,
        )


@pytest.mark.asyncio
async def test_ocr_manager_restores_requested_page_numbers(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("JWT_SECRET", "test-secret")
    get_settings.cache_clear()
    manager = object.__new__(OCRManager)
    prepared = PreparedInput(
        kind="pages",
        paths=["/tmp/page-2.png", "/tmp/page-5.png"],
        page_numbers=[2, 5],
    )

    try:
        result = await manager._recognize_pages(FakePageProvider(), prepared)
    finally:
        get_settings.cache_clear()

    assert [page.page_num for page in result.pages] == [2, 5]


@pytest.mark.asyncio
async def test_full_page_scan_with_text_layer_is_not_ocr_duplicated(tmp_path):
    """The hidden text layer wins; the full-page scan image must not be appended again."""
    path = tmp_path / "hybrid-scan.pdf"
    _make_pdf(path, image_rect=fitz.Rect(0, 0, 600, 800), text_layer=NATIVE_TEXT)

    loaded = PdfLoader().load(str(path))
    try:
        assert len(loaded.images) == 1
        assert loaded.images[0].is_page_background is True
        assert loaded.page_heights == [800.0]

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
            page_heights=loaded.page_heights,
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
        assert _pdf_pages_requiring_ocr(loaded, "pdf") == []

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
        assert _pdf_pages_requiring_ocr(loaded, "pdf") == [1]
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
