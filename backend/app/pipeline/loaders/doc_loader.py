"""旧版 Word (.doc) 文档加载器。

.doc 通常是以 OLE2 Compound File 包装的 Word 二进制文档，python-docx 无法
直接解析。本加载器优先用 LibreOffice 转为 .docx，再复用 DocxLoader 的文本、
表格、图片和页级 OCR 链路。

部分办公系统（常见于政务/IM 导出）会把 HTML 或 RTF 负载写进 OLE 的
``WordDocument`` 流，同时保留 ``.doc`` 扩展名。这类文件没有有效 Word FIB，
LibreOffice 自动探测会拒绝加载。原生转换失败后，本加载器会检查该流并按真实
负载重新选择导入过滤器；恢复结果仍然进入 DocxLoader，不在 loader 内直接调用
OCR。
"""

from __future__ import annotations

import logging
import os
import shutil
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from app.pipeline.loader import BaseLoader, LoadResult
from app.pipeline.loaders.docx_loader import DocxLoader

logger = logging.getLogger(__name__)

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
# OLE Compound File 魔数
_OLE_COMPOUND_FILE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# 正常 Word 97-2003 WordDocument 流的 FIB 魔数（小端 0xA5EC）
_WORD_FIB_MAGIC = b"\xec\xa5"
# 恢复负载上限。正常 DOC 不读取整个流，只有原生转换失败后才进入这里。
_MAX_RECOVERY_PAYLOAD_BYTES = 64 * 1024 * 1024
# 检查格式特征时读取的最大前缀
_RECOVERY_PROBE_BYTES = 4096
# 强制 HTML 导入时阻断子进程可能使用的外部代理出口
_BLOCKED_PROXY = "http://127.0.0.1:9"
# 写入异常信息的最长转换器输出
_MAX_CONVERSION_DIAGNOSTIC_CHARS = 1200


@dataclass(frozen=True)
class _OfficeConversionResult:
    """一次 LibreOffice 转换的结果及可诊断的失败原因。"""

    path: str | None = None
    error: str = ""


@dataclass(frozen=True)
class _EmbeddedPayloadFormat:
    """OLE WordDocument 流中可恢复的文本负载格式。"""

    name: str
    suffix: str
    input_filter: str | None = None


@dataclass(frozen=True)
class _RecoveredDocPayload:
    """已提取到临时文件的 DOC 内嵌负载。"""

    path: str
    format_name: str
    input_filter: str | None


_HTML_PAYLOAD = _EmbeddedPayloadFormat(
    name="html",
    suffix=".html",
    input_filter="HTML (StarWriter)",
)
_RTF_PAYLOAD = _EmbeddedPayloadFormat(
    name="rtf",
    suffix=".rtf",
)


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


def _blocked_network_env() -> dict[str, str]:
    """返回禁止外部 HTTP(S) 出站的子进程环境。

    LibreOffice 的 Writer/HTML 导入通常不会下载远程图片，但强制导入的是
    不可信文档内容，因此在恢复路径上再加一层代理隔离。
    """
    env = os.environ.copy()
    for key in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        env[key] = _BLOCKED_PROXY
    env["NO_PROXY"] = ""
    env["no_proxy"] = ""
    return env


def _decode_conversion_output(data: bytes) -> str:
    """解码并截断 LibreOffice 输出，避免错误信息无限膨胀。"""
    text = data.decode("utf-8", errors="replace").strip()
    if len(text) <= _MAX_CONVERSION_DIAGNOSTIC_CHARS:
        return text
    return text[:_MAX_CONVERSION_DIAGNOSTIC_CHARS] + "...(已截断)"


def _conversion_target(target_format: str, output_filter: str | None) -> str:
    """拼出 LibreOffice ``--convert-to`` 参数。"""
    normalized = target_format.strip().lstrip(".")
    if not normalized:
        raise ValueError("target_format 不能为空")
    if not output_filter:
        return normalized
    return f"{normalized}:{output_filter}"


def _find_converted_file(
    source_path: str,
    out_dir: str,
    expected_suffix: str,
) -> str | None:
    """查找 LibreOffice 输出文件，优先同名，再扫描目标后缀。"""
    stem = Path(source_path).stem
    candidate = os.path.join(out_dir, f"{stem}{expected_suffix}")
    if os.path.isfile(candidate):
        return candidate
    for name in sorted(os.listdir(out_dir)):
        if name.lower().endswith(expected_suffix):
            return os.path.join(out_dir, name)
    return None


def _run_office_conversion(
    source_path: str,
    out_dir: str,
    target_format: str,
    *,
    input_filter: str | None = None,
    output_filter: str | None = None,
) -> _OfficeConversionResult:
    """用 LibreOffice 转换 Office 文件并保留失败诊断。

    每次尝试使用独立的 UserInstallation profile，避免并发时争抢同一 profile
    锁。可确定是源文件无法加载时不再进行无意义重试。
    """
    soffice = _find_soffice()
    if not soffice:
        return _OfficeConversionResult(error="LibreOffice (soffice) 未安装或不可用")

    target = _conversion_target(target_format, output_filter)
    expected_suffix = f".{target_format.strip().lstrip('.').lower()}"
    last_error = "LibreOffice 转换失败，未返回错误信息"

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        with tempfile.TemporaryDirectory(prefix="soffice_profile_") as profile_dir:
            user_installation = Path(profile_dir).as_uri()
            cmd = [
                soffice,
                "--headless",
                f"-env:UserInstallation={user_installation}",
            ]
            if input_filter:
                cmd.append(f"--infilter={input_filter}")
            cmd.extend(
                [
                    "--convert-to",
                    target,
                    "--outdir",
                    out_dir,
                    source_path,
                ]
            )
            run_kwargs: dict[str, object] = {
                "capture_output": True,
                "timeout": _CONVERT_TIMEOUT,
            }
            if input_filter:
                run_kwargs["env"] = _blocked_network_env()
            try:
                result = subprocess.run(cmd, check=False, **run_kwargs)
            except subprocess.TimeoutExpired:
                last_error = f"LibreOffice 转换超时（{_CONVERT_TIMEOUT}s）"
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(0.5 * attempt)
                    continue
                return _OfficeConversionResult(error=last_error)
            except OSError as exc:
                last_error = f"无法启动 LibreOffice: {exc}"
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(0.5 * attempt)
                    continue
                return _OfficeConversionResult(error=last_error)

            stderr = _decode_conversion_output(result.stderr)
            stdout = _decode_conversion_output(result.stdout)
            if result.returncode == 0:
                converted_path = _find_converted_file(
                    source_path, out_dir, expected_suffix
                )
                if converted_path:
                    return _OfficeConversionResult(path=converted_path)
                last_error = f"LibreOffice 返回成功但未生成 {expected_suffix} 文件"
            else:
                detail = stderr or stdout or f"退出码 {result.returncode}"
                last_error = f"LibreOffice 退出码 {result.returncode}: {detail}"
                if "source file could not be loaded" in detail.lower():
                    break

            if attempt < _MAX_ATTEMPTS:
                time.sleep(0.5 * attempt)

    return _OfficeConversionResult(error=last_error)


def _convert_office_file(
    source_path: str,
    out_dir: str,
    target_format: str,
) -> str | None:
    """兼容既有调用方：返回转换路径，失败返回 None。"""
    return _run_office_conversion(
        source_path,
        out_dir,
        target_format,
    ).path


def _convert_doc_to_docx(doc_path: str, out_dir: str) -> str | None:
    """用 LibreOffice 将 .doc 转换为 .docx。"""
    return _convert_office_file(doc_path, out_dir, "docx")


def _read_worddocument_stream(doc_path: str) -> bytes | None:
    """读取 OLE 文件根存储中的 WordDocument 流。"""
    try:
        with open(doc_path, "rb") as source:
            if source.read(len(_OLE_COMPOUND_FILE_MAGIC)) != _OLE_COMPOUND_FILE_MAGIC:
                return None
    except OSError:
        return None

    try:
        import olefile
    except ImportError:
        logger.warning("olefile 未安装，无法检查异常 DOC 的内嵌负载")
        return None

    try:
        with olefile.OleFileIO(doc_path) as ole:
            stream_names = [
                "/".join(entry)
                for entry in ole.listdir(streams=True, storages=False)
                if len(entry) == 1 and entry[0].lower() == "worddocument"
            ]
            if not stream_names:
                return None
            stream_name = stream_names[0]
            size = ole.get_size(stream_name)
            if size <= 0 or size > _MAX_RECOVERY_PAYLOAD_BYTES:
                logger.warning(
                    "DOC WordDocument 流大小不可恢复: %s bytes（上限 %s）",
                    size,
                    _MAX_RECOVERY_PAYLOAD_BYTES,
                )
                return None
            return ole.openstream(stream_name).read()
    except (OSError, ValueError, KeyError, IndexError, TypeError, struct.error) as exc:
        # 恢复是原生转换失败后的 best-effort 路径，不能让损坏的 OLE
        # 容器异常覆盖原始转换错误。
        logger.warning("读取 DOC WordDocument 流失败: %s", exc)
        return None


def _detect_embedded_payload_format(payload: bytes) -> _EmbeddedPayloadFormat | None:
    """按内容特征识别 WordDocument 流中的真实文本格式。"""
    probe = payload[:_RECOVERY_PROBE_BYTES].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if probe.startswith((b"<!doctype html", b"<html")):
        return _HTML_PAYLOAD
    if probe.startswith(b"<?xml") and b"<html" in probe:
        return _HTML_PAYLOAD
    if probe.startswith(b"{\\rtf"):
        return _RTF_PAYLOAD
    return None


def _recover_embedded_doc_payload(
    doc_path: str,
    out_dir: str,
) -> _RecoveredDocPayload | None:
    """从无有效 FIB 的 DOC 中提取 HTML/RTF 负载到临时文件。"""
    payload = _read_worddocument_stream(doc_path)
    if not payload:
        return None

    if len(payload) >= 2 and payload[:2] == _WORD_FIB_MAGIC:
        return None

    payload_format = _detect_embedded_payload_format(payload)
    if payload_format is None:
        return None

    # BOM 可能出现在流头部或 HTML body 开头；它不是正文，统一去除，
    # 避免 LibreOffice 将其物化为可见字符。
    if payload_format is _HTML_PAYLOAD:
        payload = payload.replace(b"\xef\xbb\xbf", b"")
    payload = payload.lstrip(b" \t\r\n")
    recovered_path = os.path.join(out_dir, f"recovered{payload_format.suffix}")
    with open(recovered_path, "wb") as recovered:
        recovered.write(payload)
    return _RecoveredDocPayload(
        path=recovered_path,
        format_name=payload_format.name,
        input_filter=payload_format.input_filter,
    )


class DocLoader(BaseLoader):
    """处理旧版 .doc 文件的加载器（原生或内嵌负载恢复后复用 DocxLoader）。"""

    def load(self, file_path: str) -> LoadResult:
        """加载 .doc 文件：原生转换失败时按真实内嵌负载恢复。

        Args:
            file_path: .doc 文件路径

        Returns:
            LoadResult: 包含文件内容、元数据和嵌入图片列表

        Raises:
            FileNotFoundError: 文件不存在
            RuntimeError: 未安装 LibreOffice
            ValueError: 原生转换与内容恢复均失败
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
            native_dir = os.path.join(out_dir, "native")
            os.makedirs(native_dir)
            native = _run_office_conversion(file_path, native_dir, "docx")
            docx_path = native.path
            recovered: _RecoveredDocPayload | None = None

            if not docx_path:
                recovery_dir = os.path.join(out_dir, "recovery")
                os.makedirs(recovery_dir)
                recovered = _recover_embedded_doc_payload(file_path, recovery_dir)
                if recovered is None:
                    raise ValueError(
                        f"无法将 .doc 转换为 .docx: {file_path}；{native.error}"
                    )

                recovered_conversion = _run_office_conversion(
                    recovered.path,
                    recovery_dir,
                    "docx",
                    input_filter=recovered.input_filter,
                    output_filter="Office Open XML Text",
                )
                if not recovered_conversion.path:
                    raise ValueError(
                        "无法将 .doc 转换为 .docx: "
                        f"{file_path}；原生转换失败: {native.error}；"
                        f"检测到内嵌 {recovered.format_name.upper()} 负载，"
                        f"但恢复转换失败: {recovered_conversion.error}"
                    )
                docx_path = recovered_conversion.path

            # 复用 docx 加载链路（文本、表格、图片与页级 OCR）。
            result = DocxLoader().load(docx_path)

        # 修正元数据，保留原始 .doc 身份并记录恢复来源。
        result.metadata["filename"] = os.path.basename(file_path)
        result.metadata["file_type"] = "doc"
        result.metadata["file_size"] = file_size
        if recovered is not None:
            result.metadata["doc_recovery"] = recovered.format_name
            result.metadata["doc_recovery_stream"] = "WordDocument"
        return result
