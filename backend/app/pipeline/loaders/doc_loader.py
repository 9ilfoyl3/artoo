"""旧版 Word (.doc) 文档加载器

.doc 是微软早期的二进制复合文档格式（OLE2），python-docx 无法解析。
本加载器先用 LibreOffice（soffice）将 .doc 转换为 .docx，再复用 DocxLoader
的文本 + 嵌入图片提取链路。

依赖：运行环境需安装 LibreOffice（提供 soffice 命令）。未安装时抛出
清晰的错误提示，引导用户安装或改用 .docx。
"""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from app.pipeline.loader import BaseLoader, LoadResult
from app.pipeline.loaders.docx_loader import DocxLoader

# soffice 常见安装路径（跨平台）
_SOFFICE_PATHS = [
    "/usr/bin/soffice",
    "/usr/lib/libreoffice/program/soffice",
    "/opt/libreoffice/program/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "C:\\Program Files\\LibreOffice\\program\\soffice.exe",
    "C:\\Program Files (x86)\\LibreOffice\\program\\soffice.exe",
]

# 转换超时（秒）
_CONVERT_TIMEOUT = 120
# 转换重试次数（并发时 LibreOffice profile 锁可能导致失败）
_MAX_ATTEMPTS = 3


def _find_soffice() -> str | None:
    """查找 soffice 可执行文件路径。

    优先级：LIBREOFFICE_PATH 环境变量 > 常见安装路径 > PATH。

    Returns:
        soffice 路径，未找到返回 None
    """
    env_path = os.environ.get("LIBREOFFICE_PATH", "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path

    for path in _SOFFICE_PATHS:
        if os.path.isfile(path):
            return path

    return shutil.which("soffice") or shutil.which("libreoffice")


def _convert_office_file(
    source_path: str,
    out_dir: str,
    target_format: str,
) -> str | None:
    """用 LibreOffice 将 Office 文件转换为目标格式。

    每次尝试使用独立的 UserInstallation profile 目录，避免并发时争抢
    同一 profile 锁导致静默失败。

    Args:
        source_path: 源 Office 文件路径
        out_dir: 输出目录
        target_format: LibreOffice 输出格式（如 ``docx`` / ``pdf``）

    Returns:
        转换后的文件路径，失败返回 None
    """
    soffice = _find_soffice()
    if not soffice:
        return None

    expected_suffix = f".{target_format.lstrip('.').lower()}"
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        with tempfile.TemporaryDirectory(prefix="soffice_profile_") as profile_dir:
            user_installation = Path(profile_dir).as_uri()
            cmd = [
                soffice,
                "--headless",
                f"-env:UserInstallation={user_installation}",
                "--convert-to",
                target_format,
                "--outdir",
                out_dir,
                source_path,
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=_CONVERT_TIMEOUT
                )
            except (OSError, subprocess.TimeoutExpired):
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(0.5 * attempt)
                    continue
                return None

            if result.returncode == 0:
                # LibreOffice 以源文件同名（改扩展名）输出
                stem = Path(source_path).stem
                candidate = os.path.join(out_dir, f"{stem}{expected_suffix}")
                if os.path.isfile(candidate):
                    return candidate
                # 兜底：扫描输出目录中的目标格式文件
                for name in os.listdir(out_dir):
                    if name.lower().endswith(expected_suffix):
                        return os.path.join(out_dir, name)

            if attempt < _MAX_ATTEMPTS:
                time.sleep(0.5 * attempt)

    return None


def _convert_doc_to_docx(doc_path: str, out_dir: str) -> str | None:
    """用 LibreOffice 将 .doc 转换为 .docx。"""
    return _convert_office_file(doc_path, out_dir, "docx")


class DocLoader(BaseLoader):
    """处理旧版 .doc 文件的加载器（转 docx 后复用 DocxLoader）"""

    def load(self, file_path: str) -> LoadResult:
        """加载 .doc 文件：转换为 docx 后提取文本与嵌入图片。

        Args:
            file_path: .doc 文件路径

        Returns:
            LoadResult: 包含文件内容、元数据和嵌入图片列表

        Raises:
            FileNotFoundError: 文件不存在
            RuntimeError: 未安装 LibreOffice
            ValueError: 转换失败
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        if _find_soffice() is None:
            raise RuntimeError(
                "解析 .doc 文件需要 LibreOffice（soffice 命令），当前环境未安装。"
                "请安装 LibreOffice，或将文件另存为 .docx 后再上传。"
            )

        file_size = os.path.getsize(file_path)

        with tempfile.TemporaryDirectory(prefix="doc_convert_") as out_dir:
            docx_path = _convert_doc_to_docx(file_path, out_dir)
            if not docx_path:
                raise ValueError(f"无法将 .doc 转换为 .docx: {file_path}")

            # 复用 docx 加载链路（文本 + 嵌入图片）
            result = DocxLoader().load(docx_path)

        # 修正元数据，保留原始 .doc 身份
        result.metadata["filename"] = os.path.basename(file_path)
        result.metadata["file_type"] = "doc"
        result.metadata["file_size"] = file_size
        return result
