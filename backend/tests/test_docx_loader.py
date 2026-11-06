"""DOCX loader regression coverage for structured text and page-level image OCR."""

from __future__ import annotations

import asyncio
from pathlib import Path

import fitz
import pytest
from docx import Document
from docx.oxml import OxmlElement, parse_xml

from app.pipeline.loader import EmbeddedImage, LoadResult
from app.pipeline.loaders.doc_loader import (
    DocLoader,
    _convert_office_file,
    _find_soffice,
)
from app.pipeline.loaders.docx_loader import DocxLoader
from app.pipeline.ocr.provider import OCRResult, PageOCRResult
from app.pipeline.pipeline import DocumentPipeline


class FakeOCRManager:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def recognize(self, file_path: str) -> OCRResult:
        self.calls.append(file_path)
        text = "OCR PAGE REPLACEMENT"
        return OCRResult(
            full_text=text,
            pages=[PageOCRResult(page_num=1, blocks=[], full_text=text)],
            avg_confidence=0.99,
            provider_name="fake",
        )


def _make_text_docx(path: Path) -> None:
    doc = Document()
    doc.sections[0].header.paragraphs[0].text = "COURT HEADER"
    doc.sections[0].footer.paragraphs[0].text = "COURT FOOTER"
    doc.add_paragraph("BEFORE TABLE")
    table = doc.add_table(rows=3, cols=3)
    table.cell(0, 0).text = "案号"
    table.cell(0, 1).text = "（2026）粤0118民初4889号"
    table.cell(0, 1).merge(table.cell(0, 2))
    table.cell(1, 0).text = "材料名称"
    table.cell(1, 1).text = "份数"
    table.cell(1, 2).text = "备注"
    table.cell(2, 0).text = "开庭传票"
    table.cell(2, 1).text = "1"
    table.cell(2, 2).text = "A | B"
    doc.add_paragraph("AFTER TABLE")
    doc.save(path)


def test_text_only_docx_extracts_tables_and_body_order(tmp_path, monkeypatch):
    path = tmp_path / "structured.docx"
    _make_text_docx(path)
    monkeypatch.setattr(
        DocxLoader,
        "_convert_to_pdf",
        lambda *_: pytest.fail("text-only DOCX must not invoke LibreOffice"),
    )

    result = DocxLoader().load(str(path))

    assert result.metadata["docx_extraction_mode"] == "structured"
    assert result.images == []
    assert "COURT HEADER" in result.content
    assert "COURT FOOTER" in result.content
    assert "| 案号 | （2026）粤0118民初4889号 |" in result.content
    assert "| 材料名称 | 份数 | 备注 |" in result.content
    assert "| 开庭传票 | 1 | A \\| B |" in result.content
    assert result.content.index("BEFORE TABLE") < result.content.index("| 案号")
    assert result.content.index("| 开庭传票") < result.content.index("AFTER TABLE")


def test_nested_content_control_is_extracted_once(tmp_path):
    path = tmp_path / "controls.docx"
    doc = Document()
    paragraph = doc.add_paragraph()
    content_control = OxmlElement("w:sdt")
    content = OxmlElement("w:sdtContent")
    inner_paragraph = OxmlElement("w:p")
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "CONTENT CONTROL"
    run.append(text)
    inner_paragraph.append(run)
    content.append(inner_paragraph)
    content_control.append(content)
    paragraph._element.addnext(content_control)
    doc.save(path)

    result = DocxLoader().load(str(path))

    assert result.content.count("CONTENT CONTROL") == 1


def test_vml_text_box_is_extracted_once(tmp_path):
    path = tmp_path / "text-box.docx"
    doc = Document()
    text_box = parse_xml(
        '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:v="urn:schemas-microsoft-com:vml"><w:r><w:pict><v:shape><v:textbox>'
        '<w:txbxContent><w:p><w:r><w:t>TEXT BOX VALUE</w:t></w:r></w:p>'
        '</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>'
    )
    doc.element.body.append(text_box)
    doc.save(path)

    result = DocxLoader().load(str(path))

    assert result.content.count("TEXT BOX VALUE") == 1


def test_paragraph_tabs_and_soft_breaks_are_preserved(tmp_path):
    path = tmp_path / "whitespace.docx"
    doc = Document()
    paragraph = doc.add_paragraph()
    paragraph.add_run("字段A")
    paragraph.add_run().add_tab()
    paragraph.add_run("值A")
    paragraph.add_run().add_break()
    paragraph.add_run("字段B")
    paragraph.add_run().add_tab()
    paragraph.add_run("值B")
    doc.save(path)

    result = DocxLoader().load(str(path))

    assert "字段A\t值A\n字段B\t值B" in result.content


def _make_pdf(path: Path, *, image_page: bool) -> None:
    image_doc = fitz.open()
    image_page_doc = image_doc.new_page(width=400, height=300)
    image_page_doc.insert_text((20, 40), "IMAGE", fontsize=24)
    image_bytes = image_page_doc.get_pixmap(alpha=False).tobytes("png")
    image_doc.close()

    pdf = fitz.open()
    first = pdf.new_page(width=600, height=800)
    first.insert_textbox(
        fitz.Rect(50, 50, 550, 300),
        "PAGE ONE NATIVE TEXT",
        fontsize=16,
    )
    second = pdf.new_page(width=600, height=800)
    second.insert_textbox(
        fitz.Rect(50, 50, 550, 150),
        "PAGE TWO FALLBACK TEXT",
        fontsize=16,
    )
    if image_page:
        second.insert_image(fitz.Rect(100, 250, 500, 550), stream=image_bytes)
    pdf.save(path)
    pdf.close()


def test_image_bearing_page_uses_one_whole_page_snapshot(tmp_path):
    pdf_path = tmp_path / "rendered.pdf"
    _make_pdf(pdf_path, image_page=True)

    extraction = DocxLoader._extract_pdf_pages(str(pdf_path))
    assert extraction is not None
    try:
        assert extraction.image_page_numbers == [2]
        assert len(extraction.snapshots) == 1
        assert extraction.snapshots[0].page_or_index == 2
        assert extraction.snapshots[0].is_page_snapshot is True
        assert extraction.page_texts[0].strip() == "PAGE ONE NATIVE TEXT"
        assert "PAGE TWO FALLBACK TEXT" in extraction.page_texts[1]
        snapshot = fitz.open(extraction.snapshots[0].file_path)
        try:
            assert snapshot.page_count == 1
            assert snapshot[0].rect.width > 600
        finally:
            snapshot.close()
    finally:
        extraction.cleanup()


def test_page_snapshot_ocr_replaces_only_image_page_text():
    snapshot = EmbeddedImage(
        file_path="/tmp/not-read-by-fake.png",
        format="png",
        page_or_index=2,
        is_page_snapshot=True,
    )
    load_result = LoadResult(
        content="PAGE ONE NATIVE TEXT\n\nPAGE TWO FALLBACK TEXT",
        images=[snapshot],
        page_texts=["PAGE ONE NATIVE TEXT", "PAGE TWO FALLBACK TEXT"],
        page_has_usable_text=[True, False],
    )
    manager = FakeOCRManager()
    pipeline = object.__new__(DocumentPipeline)
    pipeline.ocr_manager = manager

    merged = asyncio.run(
        pipeline._process_embedded_images(load_result, "docx-page-ocr")
    )

    assert merged == "PAGE ONE NATIVE TEXT\nOCR PAGE REPLACEMENT"
    assert "[图片内容]" not in merged
    assert len(manager.calls) == 1
    assert load_result.metadata["ocr_page_numbers"] == [2]


def test_unpainted_image_resource_does_not_trigger_page_ocr():
    image_doc = fitz.open()
    image_page = image_doc.new_page(width=400, height=300)
    image_page.insert_text((30, 50), "IMAGE", fontsize=30)
    image_bytes = image_page.get_pixmap(alpha=False).tobytes("png")
    image_doc.close()

    doc = fitz.open()
    painted_page = doc.new_page(width=600, height=800)
    painted_page.insert_image(
        fitz.Rect(100, 100, 300, 250),
        stream=image_bytes,
    )
    image_xref = painted_page.get_images(full=True)[0][0]

    resource_only_page = doc.new_page(width=600, height=800)
    _, resources_value = doc.xref_get_key(resource_only_page.xref, "Resources")
    resources_xref = int(resources_value.split()[0])
    doc.xref_set_key(resources_xref, "XObject/Im1", f"{image_xref} 0 R")
    try:
        assert resource_only_page.get_images(full=True)
        assert resource_only_page.get_image_rects(image_xref) == []
        assert DocxLoader._page_has_content_image(resource_only_page) is False
    finally:
        doc.close()


@pytest.mark.skipif(_find_soffice() is None, reason="LibreOffice is required")
def test_docx_with_body_image_uses_full_page_ocr_mode(tmp_path):
    path = tmp_path / "image-and-table.docx"
    image_path = tmp_path / "content.png"
    image_doc = fitz.open()
    image_page = image_doc.new_page(width=600, height=400)
    image_page.insert_textbox(
        fitz.Rect(40, 40, 560, 350),
        "CONTENT IMAGE WITH VISIBLE INFORMATION " * 6,
        fontsize=18,
    )
    image_page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(image_path)
    image_doc.close()

    doc = Document()
    doc.add_paragraph("TEXT BEFORE IMAGE")
    doc.add_picture(str(image_path), width=None)
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "字段"
    table.cell(0, 1).text = "值"
    table.cell(1, 0).text = "案号"
    table.cell(1, 1).text = "（2026）粤0118民初4889号"
    doc.add_paragraph("TEXT AFTER IMAGE")
    doc.save(path)

    result = DocxLoader().load(str(path))
    try:
        assert result.metadata["docx_extraction_mode"] == "page_ocr"
        assert result.images
        assert all(image.is_page_snapshot for image in result.images)
        snapshot_pages = [
            image.page_or_index for image in result.images
        ]
        assert result.metadata["ocr_page_numbers"] == snapshot_pages
        assert snapshot_pages == [
            page_num
            for page_num, usable in enumerate(result.page_has_usable_text, 1)
            if not usable
        ]
        image_pages = set(result.metadata["ocr_page_numbers"])
        assert all(
            not usable
            for page_num, usable in enumerate(result.page_has_usable_text, 1)
            if page_num in image_pages
        )
    finally:
        DocumentPipeline._cleanup_image_temp_dirs(result.images)


@pytest.mark.skipif(_find_soffice() is None, reason="LibreOffice is required")
def test_image_only_docx_uses_page_snapshot_path(tmp_path):
    path = tmp_path / "image-page.docx"
    image_path = tmp_path / "page.png"
    image_doc = fitz.open()
    image_page = image_doc.new_page(width=600, height=800)
    image_page.insert_textbox(
        fitz.Rect(50, 50, 550, 700),
        "IMAGE ONLY PAGE " * 30,
        fontsize=20,
    )
    image_page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(image_path)
    image_doc.close()

    doc = Document()
    doc.add_picture(str(image_path), width=None)
    doc.save(path)

    result = DocxLoader().load(str(path))
    try:
        assert result.metadata["docx_extraction_mode"] == "page_ocr"
        assert result.page_texts == [""]
        assert [image.page_or_index for image in result.images] == [1]
        assert result.images[0].is_page_snapshot is True
        assert result.page_has_usable_text == [False]
    finally:
        DocumentPipeline._cleanup_image_temp_dirs(result.images)


def test_image_only_docx_uses_embedded_image_fallback(monkeypatch, tmp_path):
    path = tmp_path / "image-only.docx"
    doc = Document()
    paragraph = doc.add_paragraph()
    run = paragraph.add_run()
    # Use a real image relationship so package detection and extraction both run.
    image_path = tmp_path / "pixel.png"
    image_doc = fitz.open()
    image_page = image_doc.new_page(width=600, height=800)
    image_page.insert_textbox(
        fitz.Rect(40, 40, 560, 400),
        "IMAGE ONLY DOCUMENT WITH ENOUGH PIXELS AND BYTES " * 8,
        fontsize=18,
    )
    image_page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(image_path)
    image_doc.close()
    run.add_picture(str(image_path))
    doc.save(path)
    monkeypatch.setattr(DocxLoader, "_convert_to_pdf", lambda *_: None)

    result = DocxLoader().load(str(path))

    assert result.content.strip() == ""
    assert result.metadata["docx_extraction_mode"] == "structured"
    assert result.metadata["docx_page_ocr_fallback"] == "conversion_failed"
    assert len(result.images) == 1
    try:
        assert result.images[0].is_page_snapshot is False
        assert Path(result.images[0].file_path).is_file()
    finally:
        DocumentPipeline._cleanup_image_temp_dirs(result.images)


@pytest.mark.skipif(_find_soffice() is None, reason="LibreOffice is required")
def test_doc_round_trip_preserves_table_text(tmp_path):
    source = tmp_path / "source.docx"
    _make_text_docx(source)
    converted_dir = tmp_path / "legacy"
    converted_dir.mkdir()
    doc_path = _convert_office_file(str(source), str(converted_dir), "doc")
    assert doc_path is not None

    result = DocLoader().load(doc_path)

    assert result.metadata["file_type"] == "doc"
    assert "| 材料名称 | 份数 | 备注 |" in result.content
    assert "（2026）粤0118民初4889号" in result.content
