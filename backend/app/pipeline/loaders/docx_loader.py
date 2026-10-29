"""Word (docx) document loader.

Text-only documents use native DOCX structure so tables, headers, footers, content
controls, and text boxes are available without a rendering round trip. Documents that
contain content images are rendered to PDF to recover page boundaries; each image-bearing
page is represented by one full-page snapshot for OCR, while text-only pages keep their
native PDF text layer.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field

import fitz  # pymupdf
from docx import Document
from docx.oxml.ns import qn

from app.pipeline.loader import BaseLoader, EmbeddedImage, LoadResult
from app.pipeline.loaders.pdf_loader import PdfLoader

logger = logging.getLogger(__name__)

# Minimum image data size (bytes); tiny decorative images are ignored.
_MIN_IMAGE_BYTES = 1024
# Maximum legacy fallback images extracted per document.
_MAX_IMAGES_PER_DOC = 50
# Minimum intrinsic image dimensions considered meaningful.
_MIN_IMAGE_SIZE = 50
# Header/footer band used to ignore repeated logos and border artwork.
_FURNITURE_BAND_RATIO = 0.08
# Full-page snapshot DPI. Matches the OCR input preparation default.
_SNAPSHOT_DPI = 200
# Upper bound for rendered OCR pages per document.
_MAX_SNAPSHOT_PAGES = 200

# Transparent containers that may wrap paragraphs/tables in WordprocessingML.
_CONTAINER_TAGS = {
    qn("w:sdt"),
    qn("w:sdtContent"),
    qn("w:customXml"),
    qn("w:ins"),
    qn("w:moveTo"),
}


@dataclass
class _PdfPageExtraction:
    """Page-aware extraction from the LibreOffice-rendered PDF."""

    page_texts: list[str] = field(default_factory=list)
    page_blocks: list[list[dict]] = field(default_factory=list)
    page_heights: list[float] = field(default_factory=list)
    page_has_usable_text: list[bool] = field(default_factory=list)
    snapshots: list[EmbeddedImage] = field(default_factory=list)
    image_page_numbers: list[int] = field(default_factory=list)
    snapshot_dir: str | None = None

    def cleanup(self) -> None:
        if self.snapshot_dir:
            shutil.rmtree(self.snapshot_dir, ignore_errors=True)


class DocxLoader(BaseLoader):
    """Load DOCX text structurally and OCR image-bearing pages as whole pages."""

    def load(self, file_path: str) -> LoadResult:
        """Load a DOCX file without mixing text-layer and image OCR results.

        Text-only documents stay on the native DOCX path. If the main document story
        contains images, LibreOffice is used to recover page boundaries. Pages with body
        images become one full-page OCR snapshot per page; pages without images keep their
        text layer.

        Args:
            file_path: DOCX file path.

        Returns:
            LoadResult containing either structured text or page-aware text and snapshots.

        Raises:
            FileNotFoundError: File does not exist.
            ValueError: Invalid DOCX file.
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        file_size = os.path.getsize(file_path)
        try:
            doc = Document(file_path)
        except Exception as e:
            raise ValueError(f"无法解析 docx 文件: {file_path}，错误: {e}")

        structured_content = self._extract_structured_content(doc)
        source_has_images = self._has_body_images(doc)
        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": "docx",
            "file_size": file_size,
            "docx_extraction_mode": "structured",
        }

        if source_has_images:
            with tempfile.TemporaryDirectory(prefix="docx_pdf_") as pdf_dir:
                pdf_path = self._convert_to_pdf(file_path, pdf_dir)
                if pdf_path:
                    extraction = self._extract_pdf_pages(pdf_path)
                    if extraction and extraction.image_page_numbers:
                        metadata.update(
                            {
                                "docx_extraction_mode": "page_ocr",
                                "page_count": len(extraction.page_texts),
                                "embedded_image_count": len(extraction.snapshots),
                                "ocr_page_numbers": extraction.image_page_numbers,
                            }
                        )
                        return LoadResult(
                            content="\n".join(extraction.page_texts),
                            metadata=metadata,
                            images=extraction.snapshots,
                            page_texts=extraction.page_texts,
                            page_blocks=extraction.page_blocks,
                            page_heights=extraction.page_heights,
                            page_has_usable_text=extraction.page_has_usable_text,
                        )
                    if extraction:
                        extraction.cleanup()
                        metadata["docx_page_ocr_fallback"] = "no_content_images"

                    # A logo or other header/footer artwork should not force the whole
                    # document onto the PDF path. Native structure is richer.
                    logger.info(
                        "DOCX %s 仅含页眉页脚/装饰图片，保留原生结构提取",
                        os.path.basename(file_path),
                    )
                else:
                    metadata["docx_page_ocr_fallback"] = "conversion_failed"
                    logger.warning(
                        "DOCX %s 含图片但 LibreOffice 分页失败，回退原生结构提取",
                        os.path.basename(file_path),
                    )

        fallback_images: list[EmbeddedImage] = []
        if source_has_images and not structured_content.strip():
            # Image-only fallback: with no text to preserve, OCR the embedded images.
            fallback_images = self._extract_images(doc)

        metadata["embedded_image_count"] = len(fallback_images)

        return LoadResult(
            content=structured_content,
            metadata=metadata,
            images=fallback_images,
        )

    def _extract_structured_content(self, doc) -> str:
        """Extract visible Word text in reading order, including tables and furniture."""
        header_parts: list[str] = []
        footer_parts: list[str] = []
        seen_furniture: set[str] = set()

        for section in doc.sections:
            for attr in ("header", "first_page_header", "even_page_header"):
                container = getattr(section, attr, None)
                element = getattr(container, "_element", None)
                if element is None:
                    continue
                parts = self._extract_container(element)
                text = "\n\n".join(parts).strip()
                if text and text not in seen_furniture:
                    seen_furniture.add(text)
                    header_parts.append(text)

        body_parts = self._extract_container(doc.element.body)

        for section in doc.sections:
            for attr in ("footer", "first_page_footer", "even_page_footer"):
                container = getattr(section, attr, None)
                element = getattr(container, "_element", None)
                if element is None:
                    continue
                parts = self._extract_container(element)
                text = "\n\n".join(parts).strip()
                if text and text not in seen_furniture:
                    seen_furniture.add(text)
                    footer_parts.append(text)

        return "\n\n".join(header_parts + body_parts + footer_parts).strip()

    def _extract_container(self, element) -> list[str]:
        """Extract paragraph/table blocks from a WordprocessingML container."""
        parts: list[str] = []
        for child in element.iterchildren():
            if child.tag == qn("w:p"):
                text = self._element_text(child).strip()
                if text:
                    parts.append(text)
            elif child.tag == qn("w:tbl"):
                text = self._table_markdown(child)
                if text:
                    parts.append(text)
            elif child.tag in _CONTAINER_TAGS:
                parts.extend(self._extract_container(child))
        return parts

    @classmethod
    def _element_text(cls, element) -> str:
        """Return visible text from paragraphs, preserving tabs and line breaks.

        Text nodes are assigned to their nearest paragraph so a text box is not counted
        twice by its outer anchor paragraph. ``w:tab`` and ``w:br`` become their visible
        characters rather than silently concatenating adjacent text.
        """
        paragraphs: list[str] = []
        for paragraph in element.iter(qn("w:p")):
            text_parts: list[str] = []
            for node in paragraph.iter():
                owner = cls._nearest_paragraph(node)
                if owner is not paragraph:
                    continue
                if node.tag == qn("w:t") and node.text:
                    text_parts.append(node.text)
                elif node.tag == qn("w:tab"):
                    text_parts.append("\t")
                elif node.tag in {qn("w:br"), qn("w:cr")}:
                    text_parts.append("\n")
            text = "".join(text_parts).strip()
            if text:
                paragraphs.append(text)
        return "\n".join(paragraphs)

    @staticmethod
    def _nearest_paragraph(element):
        parent = element.getparent()
        while parent is not None and parent.tag != qn("w:p"):
            parent = parent.getparent()
        return parent

    @classmethod
    def _table_markdown(cls, table_element) -> str:
        """Serialize a Word table as Markdown while preserving merged-cell semantics."""
        rows: list[list[str]] = []
        for row in table_element.findall(qn("w:tr")):
            cells: list[str] = []
            for cell in row.findall(qn("w:tc")):
                text = cls._element_text(cell)
                compact = " ".join(
                    part.strip() for part in text.splitlines() if part.strip()
                )
                cells.append(compact.replace("|", "\\|"))
            if any(cell.strip() for cell in cells):
                rows.append(cells)

        if not rows:
            return ""
        column_count = max(len(row) for row in rows)
        if column_count == 0:
            return ""

        def render_row(row: list[str]) -> str:
            padded = row + [""] * (column_count - len(row))
            return "| " + " | ".join(padded) + " |"

        lines = [render_row(rows[0])]
        lines.append("| " + " | ".join("---" for _ in range(column_count)) + " |")
        lines.extend(render_row(row) for row in rows[1:])
        return "\n".join(lines)

    @staticmethod
    def _has_body_images(doc) -> bool:
        """Return whether the main document story contains a visible image.

        Package-level image parts include thumbnails and header/footer artwork, which do
        not require whole-page OCR. Inspect drawing relationships referenced by the main
        document part instead.
        """
        try:
            for rel in doc.part.rels.values():
                if "image" in rel.reltype:
                    return True
            return any(
                element.tag in {qn("w:drawing"), qn("w:pict")}
                for element in doc.element.body.iter()
            )
        except Exception:
            return bool(doc.inline_shapes)

    @staticmethod
    def _convert_to_pdf(file_path: str, out_dir: str) -> str | None:
        """Render DOCX to PDF through the shared LibreOffice conversion helper."""
        from app.pipeline.loaders.doc_loader import _convert_office_file

        return _convert_office_file(file_path, out_dir, "pdf")

    @classmethod
    def _extract_pdf_pages(cls, pdf_path: str) -> _PdfPageExtraction | None:
        """Extract page text and full-page snapshots for image-bearing pages."""
        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            logger.warning("无法打开 DOCX 生成的 PDF: %s", e)
            return None

        extraction = _PdfPageExtraction()
        try:
            for page_idx, page in enumerate(doc):
                page_num = page_idx + 1
                text = page.get_text()
                blocks = PdfLoader._extract_page_blocks(page)
                has_content_image = cls._page_has_content_image(page)

                extraction.page_texts.append(text)
                extraction.page_blocks.append(blocks)
                extraction.page_heights.append(float(page.rect.height))

                if has_content_image and len(extraction.snapshots) < _MAX_SNAPSHOT_PAGES:
                    if extraction.snapshot_dir is None:
                        extraction.snapshot_dir = tempfile.mkdtemp(
                            prefix="docx_page_snapshots_"
                        )
                    snapshot_path = os.path.join(
                        extraction.snapshot_dir, f"page{page_num}.png"
                    )
                    matrix = fitz.Matrix(_SNAPSHOT_DPI / 72, _SNAPSHOT_DPI / 72)
                    page.get_pixmap(matrix=matrix, alpha=False).save(snapshot_path)
                    extraction.snapshots.append(
                        EmbeddedImage(
                            file_path=snapshot_path,
                            format="png",
                            page_or_index=page_num,
                            description=f"docx_page{page_num}_snapshot",
                            is_page_snapshot=True,
                        )
                    )
                    extraction.image_page_numbers.append(page_num)
                    extraction.page_has_usable_text.append(False)
                else:
                    extraction.page_has_usable_text.append(bool(text.strip()))
        except Exception as e:
            logger.warning("DOCX 分页提取失败: %s", e)
            extraction.cleanup()
            return None
        finally:
            doc.close()

        return extraction

    @staticmethod
    def _page_has_content_image(page: fitz.Page) -> bool:
        """Detect body images while ignoring tiny/header/footer decoration.

        A page is treated as an image page when at least one meaningful image intersects
        the body area. Repeated logos confined to the top/bottom furniture bands remain on
        the native text path.
        """
        page_rect = page.rect
        if page_rect.height <= 0:
            return False
        body_top = page_rect.y0 + page_rect.height * _FURNITURE_BAND_RATIO
        body_bottom = page_rect.y1 - page_rect.height * _FURNITURE_BAND_RATIO

        for image_info in page.get_images(full=True):
            xref = image_info[0]
            width = image_info[2] if len(image_info) > 2 else 0
            height = image_info[3] if len(image_info) > 3 else 0
            if width < _MIN_IMAGE_SIZE or height < _MIN_IMAGE_SIZE:
                continue

            try:
                rects = page.get_image_rects(xref)
            except Exception:
                return True
            if not rects:
                # PDFs may retain an image resource in a page's resource dictionary
                # without painting it. That is not a visible content image.
                continue

            for rect in rects:
                visible = rect & page_rect
                if visible.width <= 0 or visible.height <= 0:
                    continue
                if visible.y1 <= body_top or visible.y0 >= body_bottom:
                    continue
                return True
        return False

    @staticmethod
    def _extract_images(doc) -> list[EmbeddedImage]:
        """Extract embedded images for the image-only fallback path."""
        tmp_dir = tempfile.mkdtemp(prefix="docx_images_")
        images: list[EmbeddedImage] = []
        seen_hashes: set[str] = set()
        img_index = 0

        for rel in doc.part.rels.values():
            if "image" not in rel.reltype:
                continue

            if len(images) >= _MAX_IMAGES_PER_DOC:
                break

            try:
                image_part = rel.target_part
                image_bytes = image_part.blob
            except Exception:
                continue

            if not image_bytes or len(image_bytes) < _MIN_IMAGE_BYTES:
                continue

            img_hash = hashlib.md5(image_bytes).hexdigest()
            if img_hash in seen_hashes:
                continue
            seen_hashes.add(img_hash)

            content_type = getattr(image_part, "content_type", "")
            img_format = "png"
            if "jpeg" in content_type or "jpg" in content_type:
                img_format = "jpeg"
            elif "png" in content_type:
                img_format = "png"
            elif "gif" in content_type:
                img_format = "gif"
            elif "bmp" in content_type:
                img_format = "bmp"

            img_index += 1
            img_filename = f"docx_img{img_index}_{img_hash[:8]}.{img_format}"
            img_path = os.path.join(tmp_dir, img_filename)
            with open(img_path, "wb") as f:
                f.write(image_bytes)

            images.append(
                EmbeddedImage(
                    file_path=img_path,
                    format=img_format,
                    page_or_index=img_index,
                    content_hash=img_hash,
                    description=f"docx_img{img_index}",
                )
            )

        return images
