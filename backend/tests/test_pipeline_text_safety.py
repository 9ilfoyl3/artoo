"""Pipeline storage-safety coverage for extracted text."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret-please-use-32bytes-min")

import pytest

from app.pipeline.cleaner import sanitize_text_for_storage
from app.pipeline.chunker import ChunkResult
from app.pipeline.embedder import EmbedResult
from app.pipeline.pipeline import DocumentPipeline
from app.session_upload.limits import UploadLimits


def test_sanitize_text_for_storage_removes_only_nul() -> None:
    raw = "第一行\x00内容\n第二行\t末尾"

    cleaned = sanitize_text_for_storage(raw)

    assert cleaned == "第一行内容\n第二行\t末尾"
    assert "\x00" not in cleaned


@pytest.mark.asyncio
async def test_nul_only_document_is_treated_as_empty(tmp_path) -> None:
    source = tmp_path / "nul-only.txt"
    source.write_bytes(b"\x00\x00")

    pipeline = DocumentPipeline(
        model_manager=object(),
        milvus_client=object(),
        db_session_factory=object(),
    )

    processed = await pipeline.process_to_vectors(
        str(source),
        source_kind="session",
        source_id="session-1",
        tenant_id=None,
        limits=UploadLimits(
            upload_max_file_bytes=1024 * 1024,
            kb_chunk_cap=10_000,
        ),
    )

    assert processed.doc_metadata["empty_content"] is True
    assert processed.chunk_result.parent_chunks == []
    assert processed.chunk_result.child_chunks == []


@pytest.mark.asyncio
async def test_process_to_vectors_sanitizes_chunks_before_metadata_and_embedding(
    monkeypatch, tmp_path
) -> None:
    """NUL introduced by any chunker/enricher must not reach storage or embedding."""
    source = tmp_path / "nul.txt"
    source.write_text("source text", encoding="utf-8")

    pipeline = DocumentPipeline(
        model_manager=object(),
        milvus_client=object(),
        db_session_factory=object(),
    )

    class _Chunker:
        def chunk(self, _text: str, _metadata: dict) -> ChunkResult:
            return ChunkResult(
                parent_chunks=["parent\x00text"],
                child_chunks=["child\x00text"],
                parent_child_map={0: [0]},
                context_headers=["header\x00text"],
            )

    async def _select_chunker(**_kwargs):
        return _Chunker()

    async def _session_used_chunks(_session_id: str) -> int:
        return 0

    captured_embed_texts: list[str] = []

    async def _embed_with_progress(texts, **_kwargs) -> EmbedResult:
        captured_embed_texts.extend(texts)
        return EmbedResult(dense_vectors=[[1.0]], sparse_vectors=[{0: 1.0}])

    class _RetrievalStore:
        async def get_effective(self, _tenant_id):
            return SimpleNamespace(
                parent_chunk_size=2500,
                child_chunk_size=450,
                chunk_overlap=70,
            )

    monkeypatch.setattr(pipeline, "_select_chunker_for_source", _select_chunker)
    monkeypatch.setattr(pipeline, "_session_used_chunks", _session_used_chunks)
    monkeypatch.setattr(pipeline, "_embed_with_progress", _embed_with_progress)
    monkeypatch.setattr(
        "app.retrieval.config.get_retrieval_config_store",
        lambda: _RetrievalStore(),
    )

    processed = await pipeline.process_to_vectors(
        str(source),
        source_kind="session",
        source_id="session-1",
        tenant_id=None,
        limits=UploadLimits(
            upload_max_file_bytes=1024 * 1024,
            kb_chunk_cap=10_000,
        ),
    )

    assert processed.chunk_result.parent_chunks == ["parenttext"]
    assert processed.chunk_result.child_chunks == ["childtext"]
    assert processed.chunk_result.context_headers == ["headertext"]
    assert processed.enriched_children == ["childtext"]
    assert all("\x00" not in repr(metadata) for metadata in processed.metadata_list)
    assert captured_embed_texts
    assert all("\x00" not in text for text in captured_embed_texts)

