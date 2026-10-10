"""文档加载器 - 基类与工厂方法"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class EmbeddedImage:
    """文档中嵌入的图片（存储为临时文件路径，避免内存压力）"""
    file_path: str               # 图片临时文件路径
    format: str                  # 图片格式（png/jpeg 等）
    page_or_index: int = 0       # 所在页码或位置索引（从1开始）
    content_hash: str = ""       # 图片内容 hash（用于去重）
    description: str = ""        # 可选描述
    is_page_background: bool = False  # PDF 整页扫描底图；页面已有可信文本层时跳过其 OCR


@dataclass
class LoadResult:
    """文档加载结果"""
    content: str          # 文档文本内容
    metadata: dict = field(default_factory=dict)  # 元数据（文件名、页码等）
    images: list[EmbeddedImage] = field(default_factory=list)  # 文档中嵌入的图片
    page_texts: list[str] = field(default_factory=list)  # 按页文本（用于图片文本按页插入）
    pre_chunked: list[str] = field(default_factory=list)  # loader 预切分的 chunk（跳过 chunker）
    page_blocks: list[list[dict]] | None = None  # 按页文本块（含 bbox），用于 TextCleaner 去噪


class BaseLoader(ABC):
    """文档加载器基类"""

    @abstractmethod
    def load(self, file_path: str) -> LoadResult:
        """加载文件并返回文本内容"""
        ...


# 支持的音频文件类型（走 ASR 语音转写链路）
AUDIO_TYPES = {"mp3", "wav", "m4a", "flac", "ogg"}

# 支持的文件类型
SUPPORTED_TYPES = {
    "md", "txt", "pdf", "doc", "docx", "xlsx", "pptx", "csv", "jpg", "jpeg", "png",
} | AUDIO_TYPES


def get_loader(file_type: str) -> BaseLoader:
    """根据文件类型返回对应的 Loader 实例

    Args:
        file_type: 文件扩展名（不含点号），如 "pdf"、"docx"

    Returns:
        对应文件类型的 Loader 实例

    Raises:
        ValueError: 不支持的文件类型
    """
    # 统一转小写
    file_type = file_type.lower().strip(".")

    if file_type not in SUPPORTED_TYPES:
        raise ValueError(
            f"不支持的文件类型: {file_type}，"
            f"支持的格式: {', '.join(sorted(SUPPORTED_TYPES))}"
        )

    if file_type in ("md", "txt"):
        from app.pipeline.loaders.text_loader import TextLoader
        return TextLoader()
    elif file_type == "pdf":
        from app.pipeline.loaders.pdf_loader import PdfLoader
        return PdfLoader()
    elif file_type == "docx":
        from app.pipeline.loaders.docx_loader import DocxLoader
        return DocxLoader()
    elif file_type == "doc":
        from app.pipeline.loaders.doc_loader import DocLoader
        return DocLoader()
    elif file_type == "csv":
        from app.pipeline.loaders.csv_loader import CsvLoader
        return CsvLoader()
    elif file_type == "xlsx":
        from app.pipeline.loaders.xlsx_loader import XlsxLoader
        return XlsxLoader()
    elif file_type == "pptx":
        from app.pipeline.loaders.pptx_loader import PptxLoader
        return PptxLoader()
    elif file_type in ("jpg", "jpeg", "png"):
        from app.pipeline.loaders.image_loader import ImageLoader
        return ImageLoader()
    elif file_type in AUDIO_TYPES:
        from app.pipeline.loaders.audio_loader import AudioLoader
        return AudioLoader()

    # 不应到达此处
    raise ValueError(f"未实现的加载器: {file_type}")
