"""Empty documents complete successfully with zero chunks."""

from __future__ import annotations

import contextlib
import os
from types import SimpleNamespace

os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret-please-use-32bytes-min")

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.pipeline.ocr.provider import OCRResult
from app.pipeline.pipeline import DocumentPipeline, ProcessedDocument
from app.schema.db import Base, Document, KnowledgeBase, SessionFile
from app.session_upload.limits import UploadLimits
from app.session_upload.queue import SessionUploadTask
from app.session_upload.service import SessionUploadService

pytestmark = pytest.mark.asyncio


class _EmptyOCRManager:
    """OCR service that succeeds but recognizes no text."""

    async def recognize_document(self, file_path: str, **_kwargs) -> OCRResult:
        return OCRResult(
            full_text="",
            pages=[],
            avg_confidence=0.0,
            provider_name="empty-test",
        )


class _FakeMilvus:
    def __init__(self, before_ensure=None) -> None:
        self.ensured: list[tuple[str, int | None, int | None]] = []
        self.inserted: list[dict] = []
        self.deleted_doc_ids: list[str] = []
        self.before_ensure = before_ensure

    async def ensure_collection(self, kb_id: str, **kwargs) -> None:
        if self.before_ensure is not None:
            self.before_ensure()
        self.ensured.append(
            (kb_id, kwargs.get("ef_construction"), kwargs.get("m"))
        )

    async def has_collection(self, _kb_id: str) -> bool:
        return False

    async def insert(self, _kb_id: str, data: list[dict]) -> int:
        self.inserted.extend(data)
        return len(data)

    async def delete_by_doc_id(self, _kb_id: str, doc_id: str) -> None:
        self.deleted_doc_ids.append(doc_id)

    async def ensure_session_files_collection(self) -> None:
        return None


class _FakeEventBus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.events.append(event)


class _FakePipeline:
    def __init__(self, processed: ProcessedDocument) -> None:
        self._processed = processed

    async def process_to_vectors(self, _file_path: str, **_kwargs):
        return self._processed


class _TrackingSessionFactory:
    """Expose the current Session without changing its transaction semantics."""

    def __init__(self, factory):
        self._factory = factory
        self.current = None

    def __call__(self):
        return self._session_context()

    @contextlib.asynccontextmanager
    async def _session_context(self):
        previous = self.current
        async with self._factory() as session:
            self.current = session
            try:
                yield session
            finally:
                self.current = previous


async def _make_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    return engine, factory


def _empty_processed_document() -> ProcessedDocument:
    from app.pipeline.chunker import ChunkResult
    from app.pipeline.embedder import EmbedResult

    return ProcessedDocument(
        chunk_result=ChunkResult(
            parent_chunks=[],
            child_chunks=[],
            parent_child_map={},
            context_headers=[],
        ),
        enriched_children=[],
        metadata_list=[],
        embed_result=EmbedResult(dense_vectors=[], sparse_vectors=[]),
        doc_metadata={"filename": "empty.txt", "empty_content": True},
        child_to_parent={},
    )


@pytest.mark.parametrize("with_ocr", [False, True])
async def test_process_to_vectors_accepts_empty_document(
    monkeypatch, tmp_path, with_ocr
):
    """Neither missing OCR nor an empty OCR result turns an empty file into an error."""
    source = tmp_path / "empty.txt"
    source.write_text("", encoding="utf-8")

    pipeline = DocumentPipeline(
        model_manager=object(),
        milvus_client=object(),
        db_session_factory=object(),
        ocr_manager=_EmptyOCRManager() if with_ocr else None,
    )

    async def _unexpected_chunking(**_kwargs):
        raise AssertionError("empty documents should return before chunking")

    monkeypatch.setattr(
        pipeline, "_select_chunker_for_source", _unexpected_chunking
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

    assert processed.chunk_result.parent_chunks == []
    assert processed.chunk_result.child_chunks == []
    assert processed.enriched_children == []
    assert processed.metadata_list == []
    assert processed.embed_result.dense_vectors == []
    assert processed.embed_result.sparse_vectors == []
    assert processed.doc_metadata["empty_content"] is True


async def test_kb_pipeline_marks_empty_document_completed(monkeypatch):
    """KB ingestion records an empty extracted document as completed, not failed."""
    engine, session_factory = await _make_session_factory()
    async with session_factory() as session:
        session.add(
            KnowledgeBase(
                id="kb-1",
                name="empty-doc-test",
                tenant_id="tenant-1",
                config={"enable_cleaner": True},
            )
        )
        session.add(
            Document(
                id="doc-1",
                kb_id="kb-1",
                tenant_id="tenant-1",
                filename="empty.txt",
                file_type="txt",
                file_size=0,
                status="pending",
                chunk_count=0,
                progress=0,
            )
        )
        await session.commit()

    tracking_factory = _TrackingSessionFactory(session_factory)

    def _assert_no_transaction() -> None:
        assert tracking_factory.current is not None
        assert not tracking_factory.current.in_transaction()

    milvus = _FakeMilvus(before_ensure=_assert_no_transaction)
    pipeline = DocumentPipeline(
        model_manager=object(),
        milvus_client=milvus,
        db_session_factory=tracking_factory,
    )

    async def _return_empty(*_args, **_kwargs):
        # The session used for processing/filename snapshot has already committed.
        # No DB transaction may remain open across the long CPU/network phase.
        assert tracking_factory.current is not None
        assert not tracking_factory.current.in_transaction()
        return _empty_processed_document()

    monkeypatch.setattr(pipeline, "process_to_vectors", _return_empty)

    class _LimitResolver:
        async def resolve(self, _tenant_id):
            return UploadLimits(
                upload_max_file_bytes=1024 * 1024,
                kb_chunk_cap=10_000,
            )

    class _RetrievalStore:
        async def get_effective(self, _tenant_id):
            return SimpleNamespace(
                parent_chunk_size=2500,
                child_chunk_size=450,
                chunk_overlap=70,
                hnsw_ef_construction=200,
                hnsw_m=16,
            )

    monkeypatch.setattr(
        "app.session_upload.limits.get_upload_limit_resolver",
        lambda: _LimitResolver(),
    )
    monkeypatch.setattr(
        "app.retrieval.config.get_retrieval_config_store",
        lambda: _RetrievalStore(),
    )
    monkeypatch.setattr(
        "app.storage.invalidation.get_invalidation_bus", lambda: None
    )

    await pipeline.process("/tmp/empty.txt", doc_id="doc-1", kb_id="kb-1")

    async with session_factory() as session:
        doc = await session.scalar(select(Document).where(Document.id == "doc-1"))

    assert doc is not None
    assert doc.status == "completed"
    assert doc.chunk_count == 0
    assert doc.error_message is None
    assert doc.progress == 100
    assert doc.progress_message == "处理完成"
    assert milvus.inserted == []
    assert milvus.ensured and milvus.ensured[0][0] == "kb-1"

    await engine.dispose()


async def test_session_upload_marks_empty_document_completed(monkeypatch):
    """Session upload completes with zero chunks and publishes chunk_count=0."""
    engine, session_factory = await _make_session_factory()
    async with session_factory() as session:
        session.add(
            SessionFile(
                id="file-1",
                session_id="session-1",
                tenant_id="tenant-1",
                owner_user_id="user-1",
                filename="empty.txt",
                file_type="txt",
                file_size=0,
                chunk_count=0,
                status="queued",
                progress=0,
            )
        )
        await session.commit()

    @contextlib.asynccontextmanager
    async def _materialized(_key: str, suffix: str):
        yield f"/tmp/empty{suffix}"

    class _LimitResolver:
        async def resolve(self, _tenant_id):
            return UploadLimits(
                upload_max_file_bytes=1024 * 1024,
                kb_chunk_cap=10_000,
            )

    async def _pipeline_factory():
        return _FakePipeline(_empty_processed_document())

    monkeypatch.setattr(
        "app.session_upload.service.materialized_file", _materialized
    )
    monkeypatch.setattr(
        "app.session_upload.limits.get_upload_limit_resolver",
        lambda: _LimitResolver(),
    )
    monkeypatch.setattr(
        "app.session_upload.service.get_invalidation_bus", lambda: None
    )

    milvus = _FakeMilvus()
    event_bus = _FakeEventBus()
    service = SessionUploadService(
        milvus_client=milvus,
        db_session_factory=session_factory,
        pipeline_factory=_pipeline_factory,
        event_bus=event_bus,
    )

    await service.process_task(
        SessionUploadTask(
            file_id="file-1",
            session_id="session-1",
            tenant_id="tenant-1",
            owner_user_id="user-1",
            object_key="sessions/session-1/file-1.txt",
            ext="txt",
            filename="empty.txt",
        )
    )

    async with session_factory() as session:
        row = await session.get(SessionFile, "file-1")

    assert row is not None
    assert row.status == "completed"
    assert row.chunk_count == 0
    assert row.progress == 100
    assert row.error_message is None
    assert milvus.inserted == []
    completed = [event for event in event_bus.events if event["type"] == "completed"]
    assert completed and completed[0]["chunk_count"] == 0

    await engine.dispose()
