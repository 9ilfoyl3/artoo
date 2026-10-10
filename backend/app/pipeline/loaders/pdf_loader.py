"""PDF 文档加载器（基于 pymupdf）

支持提取文本和嵌入图片。图片写入临时目录（避免内存压力），
由 pipeline 的 OCR 流程处理后清理。
"""

import hashlib
import os
import tempfile

import fitz  # pymupdf

from app.pipeline.loader import BaseLoader, EmbeddedImage, LoadResult


# 最小图片尺寸阈值（像素），过小的图片（如装饰图标）跳过
_MIN_IMAGE_SIZE = 50
# 最小图片数据大小（字节），过小的图片数据跳过
_MIN_IMAGE_BYTES = 1024
# 单文档最大提取图片数量
_MAX_IMAGES_PER_DOC = 50
# 判定文本层可信所需的最小字符数：过短文本多为标题、页眉或装饰文字
_MIN_TEXT_LAYER_CHARS = 80
# 判定文本层可信所需的最小文本面积占比：避免把整页照片的短说明误判为扫描文本层
_MIN_TEXT_LAYER_AREA_RATIO = 0.01
# 判定文本层可信所需的最小纵向跨度：单行说明不视为整页扫描文本层
_MIN_TEXT_LAYER_HEIGHT_RATIO = 0.03
# 扫描底图占页面面积比例阈值
_PAGE_BACKGROUND_IMAGE_AREA_RATIO = 0.70
# 扫描底图中的文本块按字符计需占页面文本层的比例
_PAGE_BACKGROUND_TEXT_OVERLAP_RATIO = 0.60


class PdfLoader(BaseLoader):
    """处理 .pdf 文件的加载器，同时提取文本和嵌入图片"""

    def load(self, file_path: str) -> LoadResult:
        """加载 PDF 文件，提取全部页面文本和嵌入图片

        图片写入临时目录，通过 content_hash 去重避免重复 OCR（如水印、logo）。

        Args:
            file_path: 文件路径

        Returns:
            LoadResult: 包含文件内容、按页文本、元数据和嵌入图片列表

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 无效的 PDF 文件
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        file_size = os.path.getsize(file_path)

        try:
            doc = fitz.open(file_path)
        except Exception as e:
            raise ValueError(f"无法解析 PDF 文件: {file_path}，错误: {e}")

        # 创建临时目录存放提取的图片
        tmp_dir = tempfile.mkdtemp(prefix="pdf_images_")

        pages_text: list[str] = []
        page_blocks: list[list[dict]] = []
        images: list[EmbeddedImage] = []
        seen_hashes: set[str] = set()  # 用于去重
        total_images_extracted = 0

        for page_idx, page in enumerate(doc):
            # 提取文本
            text = page.get_text()
            pages_text.append(text)

            # 使用 get_text("dict") 获取带 bbox 的文本块，供 TextCleaner 去噪使用
            blocks = self._extract_page_blocks(page)
            page_blocks.append(blocks)
            has_usable_text_layer = self._has_usable_text_layer(page, blocks)

            # 达到图片上限后不再提取
            if total_images_extracted >= _MAX_IMAGES_PER_DOC:
                continue

            # 提取该页嵌入的图片
            page_images = self._extract_page_images(
                doc,
                page,
                page_idx + 1,
                tmp_dir,
                seen_hashes,
                page_blocks=blocks,
                has_usable_text_layer=has_usable_text_layer,
            )
            total_images_extracted += len(page_images)
            images.extend(page_images)

        page_count = len(pages_text)
        content = "\n".join(pages_text)

        doc.close()

        metadata = {
            "filename": os.path.basename(file_path),
            "file_type": "pdf",
            "file_size": file_size,
            "page_count": page_count,
            "embedded_image_count": len(images),
        }

        return LoadResult(
            content=content,
            metadata=metadata,
            images=images,
            page_texts=pages_text,
            page_blocks=page_blocks,
        )

    @staticmethod
    def _extract_page_blocks(page: fitz.Page) -> list[dict]:
        """从页面中提取带 bbox 的文本块

        使用 get_text("dict") 获取结构化文本信息，
        仅保留文本块（type==0），提取 bbox 和文本内容。

        Args:
            page: fitz 页面对象

        Returns:
            简化后的文本块列表，每个块包含 bbox 和 text
        """
        page_dict = page.get_text("dict")
        blocks = page_dict.get("blocks", [])
        result: list[dict] = []

        for block in blocks:
            # 仅处理文本块（type==0），跳过图片块（type==1）
            if block.get("type", 0) != 0:
                continue

            # 从 lines -> spans -> text 中提取文本内容
            text_parts: list[str] = []
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    span_text = span.get("text", "")
                    if span_text:
                        text_parts.append(span_text)

            text = "".join(text_parts)
            if not text.strip():
                continue

            result.append({
                "bbox": tuple(block["bbox"]),  # (x0, y0, x1, y1)
                "text": text,
            })

        return result

    @staticmethod
    def _has_usable_text_layer(page: fitz.Page, blocks: list[dict]) -> bool:
        """判断页面文本层是否足以作为整页扫描图的权威文本来源。

        扫描件常见“整页图像 + 隐藏 OCR 文本层”结构。只要文本层达到最小字符数
        且覆盖一定页面面积，就将其视为该页的文本来源，避免后面再把整页图像 OCR
        一次并追加成重复内容。
        """
        text_chars = sum(len(block.get("text", "").strip()) for block in blocks)
        if text_chars < _MIN_TEXT_LAYER_CHARS:
            return False

        page_area = page.rect.width * page.rect.height
        if page_area <= 0:
            return False

        text_area = sum(
            max(0.0, block["bbox"][2] - block["bbox"][0])
            * max(0.0, block["bbox"][3] - block["bbox"][1])
            for block in blocks
        )
        if text_area / page_area < _MIN_TEXT_LAYER_AREA_RATIO:
            return False

        text_top = min(block["bbox"][1] for block in blocks)
        text_bottom = max(block["bbox"][3] for block in blocks)
        text_height_ratio = (text_bottom - text_top) / page.rect.height
        return text_height_ratio >= _MIN_TEXT_LAYER_HEIGHT_RATIO

    @staticmethod
    def _is_page_background_image(
        page: fitz.Page,
        blocks: list[dict],
        xref: int,
    ) -> bool:
        """判断图片是否是该页整页扫描底图。

        需要同时满足：
        - 图片显示区域覆盖页面大部分面积；
        - 页面文本层的大部分字符位于该图片区域内。

        普通插图、图表和局部截图不会命中，因此仍会进入后续 OCR。
        """
        try:
            image_rects = page.get_image_rects(xref)
        except Exception:
            return False
        if not image_rects:
            return False

        page_area = page.rect.width * page.rect.height
        if page_area <= 0:
            return False

        total_chars = sum(len(block.get("text", "").strip()) for block in blocks)
        if total_chars <= 0:
            return False

        for image_rect in image_rects:
            visible = image_rect & page.rect
            visible_area = visible.width * visible.height
            if visible_area / page_area < _PAGE_BACKGROUND_IMAGE_AREA_RATIO:
                continue

            inside_chars = 0
            for block in blocks:
                text = block.get("text", "").strip()
                if not text:
                    continue
                block_rect = fitz.Rect(block["bbox"])
                overlap = block_rect & image_rect
                block_area = block_rect.width * block_rect.height
                if (
                    block_area > 0
                    and overlap.width * overlap.height / block_area >= 0.5
                ):
                    inside_chars += len(text)

            if inside_chars / total_chars >= _PAGE_BACKGROUND_TEXT_OVERLAP_RATIO:
                return True

        return False

    @staticmethod
    def _extract_page_images(
        doc: fitz.Document,
        page: fitz.Page,
        page_num: int,
        tmp_dir: str,
        seen_hashes: set[str],
        *,
        page_blocks: list[dict],
        has_usable_text_layer: bool,
    ) -> list[EmbeddedImage]:
        """提取单页中的嵌入图片，写入临时目录

        通过 content_hash 去重，过滤装饰性小图。

        Args:
            doc: fitz 文档对象
            page: 当前页面对象
            page_num: 页码（从1开始）
            tmp_dir: 临时目录路径
            seen_hashes: 已见图片 hash 集合（用于去重，会被修改）
            page_blocks: 当前页文本块（含 bbox）
            has_usable_text_layer: 当前页是否存在可信文本层

        Returns:
            该页提取到的 EmbeddedImage 列表
        """
        images: list[EmbeddedImage] = []

        for img_info in page.get_images(full=True):
            xref = img_info[0]

            try:
                base_image = doc.extract_image(xref)
            except Exception:
                continue

            if not base_image:
                continue

            image_bytes = base_image.get("image")
            if not image_bytes or len(image_bytes) < _MIN_IMAGE_BYTES:
                continue

            # 检查图片尺寸，过滤装饰性小图
            width = base_image.get("width", 0)
            height = base_image.get("height", 0)
            if width < _MIN_IMAGE_SIZE or height < _MIN_IMAGE_SIZE:
                continue

            # 计算 hash 去重（水印、logo 等重复图片只处理一次）
            img_hash = hashlib.md5(image_bytes).hexdigest()
            if img_hash in seen_hashes:
                continue
            seen_hashes.add(img_hash)

            # 写入临时文件
            img_ext = base_image.get("ext", "png")
            img_filename = f"page{page_num}_img{len(images)+1}_{img_hash[:8]}.{img_ext}"
            img_path = os.path.join(tmp_dir, img_filename)

            with open(img_path, "wb") as f:
                f.write(image_bytes)

            is_page_background = (
                has_usable_text_layer
                and PdfLoader._is_page_background_image(page, page_blocks, xref)
            )
            images.append(
                EmbeddedImage(
                    file_path=img_path,
                    format=img_ext,
                    page_or_index=page_num,
                    content_hash=img_hash,
                    description=f"pdf_page{page_num}_img{len(images)+1}",
                    is_page_background=is_page_background,
                )
            )

        return images
