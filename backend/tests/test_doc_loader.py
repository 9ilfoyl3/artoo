"""旧版 Word (.doc) 加载器测试。

覆盖：
- loader 工厂正确路由 doc → DocLoader
- soffice 探测与 LibreOffice 转换诊断
- 原生转换失败后的 OLE WordDocument 负载识别与提流
- 内嵌 HTML 恢复后复用 DocxLoader，并保留原始 DOC 元数据
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.pipeline.loader import SUPPORTED_TYPES, get_loader
from app.pipeline.loaders import doc_loader
from app.pipeline.loaders.doc_loader import DocLoader
from docx import Document


def _make_docx(path: str, paragraphs: list[str]) -> None:
    """在 path 生成一个真实 docx（供复用链路读取）。"""
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    doc.save(path)


def test_doc_in_supported_types():
    assert "doc" in SUPPORTED_TYPES


def test_get_loader_returns_doc_loader():
    loader = get_loader("doc")
    assert isinstance(loader, DocLoader)


def test_get_loader_doc_case_insensitive():
    assert isinstance(get_loader(".DOC"), DocLoader)


def test_find_soffice_env_var(tmp_path, monkeypatch):
    fake = tmp_path / "soffice"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("LIBREOFFICE_PATH", str(fake))
    assert doc_loader._find_soffice() == str(fake)


def test_find_soffice_from_path(monkeypatch):
    monkeypatch.delenv("LIBREOFFICE_PATH", raising=False)
    monkeypatch.setattr(doc_loader, "_SOFFICE_PATHS", [])
    monkeypatch.setattr(
        doc_loader.shutil,
        "which",
        lambda name: "/usr/local/bin/soffice" if name == "soffice" else None,
    )
    assert doc_loader._find_soffice() == "/usr/local/bin/soffice"


def test_find_soffice_not_found(monkeypatch):
    monkeypatch.delenv("LIBREOFFICE_PATH", raising=False)
    monkeypatch.setattr(doc_loader, "_SOFFICE_PATHS", [])
    monkeypatch.setattr(doc_loader.shutil, "which", lambda name: None)
    assert doc_loader._find_soffice() is None


def test_load_missing_file():
    with pytest.raises(FileNotFoundError):
        DocLoader().load("/nonexistent/path/file.doc")


def test_load_without_libreoffice(tmp_path, monkeypatch):
    doc_path = tmp_path / "sample.doc"
    doc_path.write_bytes(b"\xd0\xcf\x11\xe0")
    monkeypatch.setattr(doc_loader, "_find_soffice", lambda: None)
    with pytest.raises(RuntimeError, match="LibreOffice"):
        DocLoader().load(str(doc_path))


def test_load_conversion_failure_reports_native_error(tmp_path, monkeypatch):
    doc_path = tmp_path / "sample.doc"
    doc_path.write_bytes(b"\xd0\xcf\x11\xe0")
    monkeypatch.setattr(doc_loader, "_find_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(
        doc_loader,
        "_run_office_conversion",
        lambda *args, **kwargs: doc_loader._OfficeConversionResult(
            error="LibreOffice 退出码 1: source file could not be loaded"
        ),
    )
    monkeypatch.setattr(
        doc_loader, "_recover_embedded_doc_payload", lambda src, out: None
    )
    with pytest.raises(ValueError, match="source file could not be loaded"):
        DocLoader().load(str(doc_path))


def test_load_success_reuses_docx_and_fixes_metadata(tmp_path, monkeypatch):
    """转换成功后复用 DocxLoader，内容来自 docx，元数据标记为 doc。"""
    doc_path = tmp_path / "报告.doc"
    doc_path.write_bytes(b"\xd0\xcf\x11\xe0" * 10)

    converted = tmp_path / "converted.docx"
    _make_docx(str(converted), ["第一段内容", "第二段内容"])

    monkeypatch.setattr(doc_loader, "_find_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(
        doc_loader,
        "_run_office_conversion",
        lambda *args, **kwargs: doc_loader._OfficeConversionResult(path=str(converted)),
    )

    result = DocLoader().load(str(doc_path))

    assert "第一段内容" in result.content
    assert "第二段内容" in result.content
    assert result.metadata["file_type"] == "doc"
    assert result.metadata["filename"] == "报告.doc"
    assert result.metadata["file_size"] == os.path.getsize(doc_path)
    assert "doc_recovery" not in result.metadata


def test_load_recovers_embedded_html_and_reuses_docx_loader(tmp_path, monkeypatch):
    doc_path = tmp_path / "法院导出.doc"
    doc_path.write_bytes(b"\xd0\xcf\x11\xe0" * 10)
    recovered_html = tmp_path / "recovered.html"
    recovered_html.write_text("<html><body><p>恢复内容</p></body></html>")
    recovered_docx = tmp_path / "recovered.docx"
    _make_docx(str(recovered_docx), ["恢复内容"])
    calls: list[dict] = []

    def fake_convert(source, out_dir, target_format, **kwargs):
        calls.append(
            {
                "source": source,
                "target": target_format,
                **kwargs,
            }
        )
        if source.endswith(".doc"):
            return doc_loader._OfficeConversionResult(
                error="LibreOffice 退出码 1: source file could not be loaded"
            )
        return doc_loader._OfficeConversionResult(path=str(recovered_docx))

    monkeypatch.setattr(doc_loader, "_find_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(doc_loader, "_run_office_conversion", fake_convert)
    monkeypatch.setattr(
        doc_loader,
        "_recover_embedded_doc_payload",
        lambda src, out: doc_loader._RecoveredDocPayload(
            path=str(recovered_html),
            format_name="html",
            input_filter="HTML (StarWriter)",
        ),
    )

    result = DocLoader().load(str(doc_path))

    assert "恢复内容" in result.content
    assert calls[1]["target"] == "docx"
    assert calls[1]["input_filter"] == "HTML (StarWriter)"
    assert calls[1]["output_filter"] == "Office Open XML Text"
    assert result.metadata["file_type"] == "doc"
    assert result.metadata["filename"] == "法院导出.doc"
    assert result.metadata["doc_recovery"] == "html"
    assert result.metadata["doc_recovery_stream"] == "WordDocument"


def test_load_recovery_failure_keeps_both_diagnostics(tmp_path, monkeypatch):
    doc_path = tmp_path / "sample.doc"
    doc_path.write_bytes(b"\xd0\xcf\x11\xe0")
    recovered = tmp_path / "recovered.html"
    recovered.write_text("<html></html>")

    monkeypatch.setattr(doc_loader, "_find_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(
        doc_loader,
        "_run_office_conversion",
        lambda source, out, target, **kwargs: doc_loader._OfficeConversionResult(
            error="recovery failed"
        ),
    )
    monkeypatch.setattr(
        doc_loader,
        "_recover_embedded_doc_payload",
        lambda src, out: doc_loader._RecoveredDocPayload(
            path=str(recovered),
            format_name="html",
            input_filter="HTML (StarWriter)",
        ),
    )

    with pytest.raises(ValueError, match="recovery failed"):
        DocLoader().load(str(doc_path))


def test_convert_doc_to_docx_no_soffice(monkeypatch, tmp_path):
    monkeypatch.setattr(doc_loader, "_find_soffice", lambda: None)
    assert doc_loader._convert_doc_to_docx("x.doc", str(tmp_path)) is None


def test_convert_doc_to_docx_success(monkeypatch, tmp_path):
    """mock subprocess：模拟 soffice 在 outdir 产出同名 docx。"""
    src = tmp_path / "sample.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    monkeypatch.setattr(doc_loader, "_find_soffice", lambda: "/usr/bin/soffice")

    class _Result:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(cmd, capture_output, timeout, check):
        (out_dir / "sample.docx").write_bytes(b"PK\x03\x04")
        return _Result()

    monkeypatch.setattr(doc_loader.subprocess, "run", fake_run)

    got = doc_loader._convert_doc_to_docx(str(src), str(out_dir))
    assert got == str(out_dir / "sample.docx")


def test_convert_doc_to_docx_retries_then_fails(monkeypatch, tmp_path):
    """subprocess 一直非零返回码，重试耗尽后返回 None。"""
    src = tmp_path / "sample.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    monkeypatch.setattr(doc_loader, "_find_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(doc_loader.time, "sleep", lambda s: None)

    calls = {"n": 0}

    class _Result:
        returncode = 1
        stdout = b""
        stderr = b"boom"

    def fake_run(cmd, capture_output, timeout, check):
        calls["n"] += 1
        return _Result()

    monkeypatch.setattr(doc_loader.subprocess, "run", fake_run)

    got = doc_loader._convert_doc_to_docx(str(src), str(out_dir))
    assert got is None
    assert calls["n"] == doc_loader._MAX_ATTEMPTS


def test_convert_doc_to_docx_does_not_retry_unloadable_source(monkeypatch, tmp_path):
    src = tmp_path / "sample.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    monkeypatch.setattr(doc_loader, "_find_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(doc_loader.time, "sleep", lambda s: None)

    calls = {"n": 0}

    class _Result:
        returncode = 1
        stdout = b""
        stderr = b"Error: source file could not be loaded"

    def fake_run(cmd, capture_output, timeout, check):
        calls["n"] += 1
        return _Result()

    monkeypatch.setattr(doc_loader.subprocess, "run", fake_run)

    got = doc_loader._convert_doc_to_docx(str(src), str(out_dir))
    assert got is None
    assert calls["n"] == 1


def test_detect_embedded_html_allows_bom_and_whitespace():
    payload = b"\xef\xbb\xbf\n  <!DOCTYPE html><html><body>x</body></html>"
    assert (
        doc_loader._detect_embedded_payload_format(payload) is doc_loader._HTML_PAYLOAD
    )


def test_detect_embedded_rtf():
    payload = b"  {\\rtf1\\ansi hello}"
    assert (
        doc_loader._detect_embedded_payload_format(payload) is doc_loader._RTF_PAYLOAD
    )


def test_detect_embedded_payload_rejects_unknown_content():
    assert doc_loader._detect_embedded_payload_format(b"\x00\x01binary") is None


def test_recover_embedded_html_payload(tmp_path, monkeypatch):
    source = tmp_path / "sample.doc"
    source.write_bytes(b"\xd0\xcf\x11\xe0")
    monkeypatch.setattr(
        doc_loader,
        "_read_worddocument_stream",
        lambda path: "<!DOCTYPE html><html><body>\ufeff正文</body></html>".encode(
            "utf-8"
        ),
    )

    recovered = doc_loader._recover_embedded_doc_payload(source, str(tmp_path))

    assert recovered is not None
    assert recovered.format_name == "html"
    assert recovered.input_filter == "HTML (StarWriter)"
    recovered_bytes = Path(recovered.path).read_bytes()
    assert Path(recovered.path).suffix == ".html"
    assert recovered_bytes.startswith(b"<!DOCTYPE html>")
    assert "正文" in recovered_bytes.decode("utf-8")
    assert b"\xef\xbb\xbf" not in recovered_bytes


def test_recover_payload_rejects_valid_word_fib(tmp_path, monkeypatch):
    source = tmp_path / "valid.doc"
    source.write_bytes(b"\xd0\xcf\x11\xe0")
    monkeypatch.setattr(
        doc_loader,
        "_read_worddocument_stream",
        lambda path: b"\xec\xa5" + b"\x00" * 32,
    )

    assert doc_loader._recover_embedded_doc_payload(source, str(tmp_path)) is None


def test_recover_payload_rejects_unknown_stream(tmp_path, monkeypatch):
    source = tmp_path / "sample.doc"
    source.write_bytes(b"\xd0\xcf\x11\xe0")
    monkeypatch.setattr(
        doc_loader,
        "_read_worddocument_stream",
        lambda path: b"\x00\x01not-html",
    )

    assert doc_loader._recover_embedded_doc_payload(source, str(tmp_path)) is None


def test_convert_office_file_uses_filtered_profile_and_blocked_network(
    monkeypatch, tmp_path
):
    src = tmp_path / "recovered.html"
    src.write_text("<html></html>")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    seen: dict = {}

    monkeypatch.setattr(doc_loader, "_find_soffice", lambda: "/usr/bin/soffice")

    class _Result:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(cmd, capture_output, timeout, env, check):
        seen["cmd"] = cmd
        seen["env"] = env
        (out_dir / "recovered.docx").write_bytes(b"PK\x03\x04")
        return _Result()

    monkeypatch.setattr(doc_loader.subprocess, "run", fake_run)

    result = doc_loader._run_office_conversion(
        str(src),
        str(out_dir),
        "docx",
        input_filter="HTML (StarWriter)",
        output_filter="Office Open XML Text",
    )

    assert result.path == str(out_dir / "recovered.docx")
    assert "--infilter=HTML (StarWriter)" in seen["cmd"]
    convert_index = seen["cmd"].index("--convert-to")
    assert seen["cmd"][convert_index + 1] == "docx:Office Open XML Text"
    assert seen["env"]["http_proxy"] == doc_loader._BLOCKED_PROXY
    assert seen["env"]["https_proxy"] == doc_loader._BLOCKED_PROXY
