"""文档文本去噪模块。

在 chunk 切分之前去除页眉页脚、页码等噪音文本，
提升后续元数据提取和检索的准确性。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# ─── 内嵌 data URI 图片（base64）剥离 ───
# 输出 Markdown 的 OCR provider（MinerU 的 md_content、VL 多模态模型的 content）
# 在未配置图片外链存储时，会把图片以 data URI 直接内嵌进正文，单张图可达数十万
# 字符。这类内容对检索零价值，却会一路污染下游：参与 dense embedding 计算扭曲
# 向量语义、进入 BM25 分词、被召回后原样喂给 LLM 吃光上下文预算。同理适用于
# 用户直接上传的含内嵌图片的 .md / .html 文件。
# 故在 chunk 之前统一剥离，替换为占位符——保留「此处有一张图」的版面信息，
# 同时不破坏所在 HTML 表格 / Markdown 段落的结构。
IMAGE_PLACEHOLDER = "[图片]"

# 1) HTML img 标签内含 data URI。MinerU 的 md_content 里 HTML 属性引号常被转义
# 为 \"，故不去匹配引号，改用 [^>]*? 宽松吃掉整个标签。base64 字符集
# （A-Za-z0-9+/= 及 URL-safe 的 -_）不含 '>'，不会越过标签边界。
_HTML_IMG_DATA_URI_RE = re.compile(r'<img\b[^>]*?data:[^>]*?>', re.IGNORECASE)

# 2) Markdown 图片语法内含 data URI：![alt](data:image/png;base64,...)
# base64 字符集不含 ')'，不会越界。
_MD_IMG_DATA_URI_RE = re.compile(r'!\[[^\]]*\]\(\s*data:[^)]*\)', re.IGNORECASE)

# 3) 兜底：未被上述两种语法完整包裹的裸 data URI（标签被上游截断、CSS url(...)
# 内嵌等）。刻意不允许空白字符出现在 base64 段中——否则字符类会跨越换行，
# 连带吞掉后续正常英文正文。要求 payload ≥ 64 字符，避免误伤正文里偶然出现的
# "data:...;base64," 字样（如技术文档在讲解 data URI 本身）。
_BARE_DATA_URI_RE = re.compile(
    r'data:[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=_-]{64,}',
    re.IGNORECASE,
)


def strip_data_uris(text: str) -> str:
    """剥离文本中内嵌的 data URI 图片（base64），替换为 ``[图片]`` 占位符。

    独立于 :class:`TextCleaner` 的页眉页脚去噪：后者是可由知识库配置
    ``enable_cleaner`` 关闭的**质量偏好**，而 base64 剥离是无条件必须的
    **数据卫生**处理——留着它只会污染向量、分词和 LLM 上下文，没有任何场景
    需要保留。因此由 pipeline 在 chunk 之前无条件调用一次。

    Args:
        text: 待处理文本（OCR 输出或 loader 提取的正文）。

    Returns:
        剥离内嵌图片数据后的文本。无内嵌图片时原样返回。
    """
    if not text or "data:" not in text:
        return text

    result = _HTML_IMG_DATA_URI_RE.sub(IMAGE_PLACEHOLDER, text)
    result = _MD_IMG_DATA_URI_RE.sub(IMAGE_PLACEHOLDER, result)
    result = _BARE_DATA_URI_RE.sub(IMAGE_PLACEHOLDER, result)
    return result


# 页码正则模式
_PAGE_NUMBER_PATTERNS = [
    r'^\s*-\s*\d+\s*-\s*$',                # - 3 -
    r'^\s*第\s*\d+\s*页\s*$',               # 第 3 页
    r'^\s*Page\s+\d+\s*(of\s+\d+)?\s*$',   # Page 3 of 10
    r'^\s*\d+\s*/\s*\d+\s*$',              # 3/10
    r'^\s*\d{1,4}\s*$',                     # 纯数字（1-4位）
]
_PAGE_NUM_RE = re.compile(
    '|'.join(f'(?:{p})' for p in _PAGE_NUMBER_PATTERNS),
    re.MULTILINE | re.IGNORECASE,
)

# 清洗后至少保留原文的有效字符比例；低于该值时视为误删并回退原文。
_MIN_RETAINED_TEXT_RATIO = 0.30
# 隐藏 OCR 文本层常把 ASCII 字母/数字逐字符拆开（如 "A 2 8 8"）。
_FRAGMENTED_ALNUM_MIN_TOKENS = 8
_FRAGMENTED_ALNUM_MIN_TOTAL_TOKENS = 16
_FRAGMENTED_ALNUM_MIN_RATIO = 0.25


def _meaningful_char_count(text: str) -> int:
    """统计非空白字符数，用于识别清洗过程中的异常文本损失。"""
    return sum(1 for char in text if not char.isspace())


def looks_like_fragmented_alnum_text(text: str) -> bool:
    """判断文本是否像逐字符拆开的隐藏 OCR 文本层。

    PDF 扫描件中的隐藏文本层经常把编号和车牌等 ASCII 字符拆成单字符 token，
    例如 ``A 2 8 8 W 8``。这种文本虽然非空，但版式和字段质量通常明显不如
    对整页图片重新执行 OCR。
    """
    tokens = text.split()
    if len(tokens) < _FRAGMENTED_ALNUM_MIN_TOTAL_TOKENS:
        return False

    isolated_alnum = sum(
        1
        for token in tokens
        if len(token) == 1 and token.isascii() and token.isalnum()
    )
    return (
        isolated_alnum >= _FRAGMENTED_ALNUM_MIN_TOKENS
        and isolated_alnum / len(tokens) >= _FRAGMENTED_ALNUM_MIN_RATIO
    )


class TextCleaner:
    """文档文本去噪器。

    通过 bbox 位置过滤、跨页重复检测和正则页码清理三种策略，
    去除文档中的页眉、页脚和页码等噪音文本。
    """

    # 页面顶部/底部区域比例阈值
    HEADER_FOOTER_RATIO: float = 0.05
    # 跨页重复判定频率阈值
    REPEAT_FREQUENCY_THRESHOLD: float = 0.5

    def clean(
        self,
        content: str,
        page_texts: list[str] | None = None,
        page_blocks: list[list[dict]] | None = None,
        page_heights: list[float] | None = None,
    ) -> str:
        """执行去噪流程: bbox过滤 → 重复检测 → 正则清理。

        Args:
            content: 原始文档文本内容。
            page_texts: 按页分割的文本列表，用于跨页重复检测。
            page_blocks: 每页的文本块列表（含 bbox 信息），用于位置过滤。
            page_heights: 每页真实高度（PDF 点），与 ``page_blocks`` 按页对应。

        Returns:
            去噪后的文本内容。
        """
        result = content

        # Step 1: bbox 过滤 - 如果有 page_blocks，过滤边缘短文本后重建文本
        if page_blocks:
            filtered_blocks = self._filter_by_bbox(page_blocks, page_heights)
            # 从过滤后的 blocks 重建文本；没有 block 的页（例如已由整页 OCR
            # 替换的页面）保留原始页文本，避免被重建为空。
            page_texts_rebuilt = []
            for page_idx, page in enumerate(filtered_blocks):
                if page:
                    page_text = "\n".join(block["text"] for block in page)
                elif page_texts and page_idx < len(page_texts):
                    page_text = page_texts[page_idx]
                else:
                    page_text = ""
                page_texts_rebuilt.append(page_text)
            result = "\n".join(page_texts_rebuilt)
            # 使用重建后的 page_texts 进行后续处理
            page_texts = page_texts_rebuilt

        # Step 2: 重复检测 - 检测并去除跨页重复的页眉页脚
        if page_texts and len(page_texts) >= 2:
            repeated = self._detect_repeated_headers(page_texts)
            if repeated:
                lines = result.split('\n')
                result = '\n'.join(
                    line for line in lines if line.strip() not in repeated
                )

        # Step 3: 正则清理 - 去除页码行
        result = self._remove_page_numbers(result)

        original_chars = _meaningful_char_count(content)
        cleaned_chars = _meaningful_char_count(result)
        if (
            original_chars
            and cleaned_chars < original_chars * _MIN_RETAINED_TEXT_RATIO
        ):
            logger.warning(
                "TextCleaner 清理后仅保留 %d/%d 个有效字符，疑似误删，回退原文",
                cleaned_chars,
                original_chars,
            )
            return content

        return result

    def _filter_by_bbox(
        self,
        page_blocks: list[list[dict]],
        page_heights: list[float] | None,
    ) -> list[list[dict]]:
        """过滤页面顶部/底部5%区域的短文本块。

        Args:
            page_blocks: 每页的文本块列表，每个块包含 bbox (x0, y0, x1, y1) 和 text。
            page_heights: 每页真实高度（用于计算边缘区域阈值）。缺失时跳过 bbox 过滤。

        Returns:
            过滤后的每页文本块列表。
        """
        if not page_heights:
            return page_blocks

        result = []
        for page_idx, page in enumerate(page_blocks):
            if page_idx >= len(page_heights):
                result.append(page)
                continue

            try:
                page_height = float(page_heights[page_idx])
            except (TypeError, ValueError):
                result.append(page)
                continue
            if page_height <= 0:
                result.append(page)
                continue

            header_threshold = page_height * self.HEADER_FOOTER_RATIO
            footer_threshold = page_height * (1 - self.HEADER_FOOTER_RATIO)
            filtered = []
            for block in page:
                y0 = block["bbox"][1]
                y1 = block["bbox"][3]
                text = block.get("text", "")

                # 顶部/底部5%区域的短文本过滤
                is_edge = y0 < header_threshold or y1 > footer_threshold
                is_short = len(text.strip()) < 100

                if is_edge and is_short:
                    continue  # 跳过边缘短文本（疑似页眉页脚）
                filtered.append(block)
            result.append(filtered)

        return result

    def _detect_repeated_headers(
        self, page_texts: list[str]
    ) -> set[str]:
        """检测跨页重复短文本（出现频率>50%判定为页眉页脚）。

        取每页首3行和尾3行中的短文本（长度在 2~50 之间），
        统计其跨页出现频率，超过 50% 的判定为页眉页脚。

        Args:
            page_texts: 按页分割的文本列表。

        Returns:
            被判定为页眉页脚的重复文本集合。
        """
        if len(page_texts) < 2:
            return set()

        total_pages = len(page_texts)
        candidates: dict[str, int] = {}

        for page_text in page_texts:
            lines = page_text.strip().split('\n')
            # 取首3行和尾3行
            check_lines = lines[:3] + lines[-3:]
            for line in check_lines:
                text = line.strip()
                if 2 < len(text) < 50:  # 短文本才可能是页眉页脚
                    candidates[text] = candidates.get(text, 0) + 1

        # 出现频率 > 50% 判定为页眉页脚
        threshold = total_pages * self.REPEAT_FREQUENCY_THRESHOLD
        return {text for text, count in candidates.items() if count > threshold}

    def _remove_page_numbers(self, text: str) -> str:
        """正则去除纯页码行。

        匹配以下页码模式并移除整行：
        - "- 3 -" 格式
        - "第 3 页" 格式
        - "Page 3 of 10" 格式
        - "3/10" 格式
        - 纯数字（1-4位）

        Args:
            text: 输入文本。

        Returns:
            去除页码行后的文本。
        """
        lines = text.split('\n')
        cleaned = [line for line in lines if not _PAGE_NUM_RE.match(line)]
        return '\n'.join(cleaned)
