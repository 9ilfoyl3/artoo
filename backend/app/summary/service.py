"""Durable document-summary scheduling and materialization helpers."""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.schema.db import Chunk, Document, DocumentSummary, SessionChunk, SessionFile
from app.storage.database import async_session
from app.summary.queue import (
    DocumentSummaryQueue,
    DocumentSummaryTask,
    get_document_summary_queue,
    set_document_summary_queue,
)

logger = logging.getLogger(__name__)

_SUPPORTED_SOURCES = {"document", "session-file"}


@dataclass(frozen=True)
class SummarySourceSnapshot:
    """Authoritative source metadata used by the summary worker."""

    source: str
    doc_id: str
    kb_id: str | None
    session_id: str | None
    tenant_id: str | None
    filename: str
    content_version: str
    chunk_contents: list[str]


async def initialize_document_summary_queue(redis_url: str) -> DocumentSummaryQueue | None:
    """Create and inject the summary queue singleton when Redis is available."""
    queue = await DocumentSummaryQueue.create(redis_url)
    set_document_summary_queue(queue)
    return queue


async def close_document_summary_queue() -> None:
    """Close and clear the summary queue singleton."""
    queue = get_document_summary_queue()
    set_document_summary_queue(None)
    redis_client = getattr(queue, "_redis", None)
    if redis_client is not None:
        try:
            await redis_client.aclose()
        except Exception:  # noqa: BLE001 - shutdown is best-effort
            pass


def _version_for_rows(rows: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for chunk_id, content in rows:
        digest.update(str(chunk_id).encode("utf-8"))
        digest.update(b"\0")
        digest.update((content or "").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


async def load_summary_source(
    source: str,
    doc_id: str,
    *,
    kb_id: str | None = None,
    session_id: str | None = None,
) -> SummarySourceSnapshot | None:
    """Load the current source snapshot and its content version."""
    if source not in _SUPPORTED_SOURCES:
        return None

    async with async_session() as session:
        if source == "document":
            document = await session.get(Document, doc_id)
            if document is None or document.status != "completed":
                return None
            if kb_id and document.kb_id != kb_id:
                return None
            rows = (
                await session.execute(
                    select(Chunk.id, Chunk.content)
                    .where(Chunk.doc_id == doc_id, Chunk.parent_id.is_(None))
                    .order_by(Chunk.chunk_index, Chunk.id)
                )
            ).all()
            return SummarySourceSnapshot(
                source=source,
                doc_id=doc_id,
                kb_id=document.kb_id,
                session_id=None,
                tenant_id=document.tenant_id,
                filename=document.filename,
                content_version=_version_for_rows([(row.id, row.content or "") for row in rows]),
                chunk_contents=[row.content or "" for row in rows if row.content],
            )

        file_row = await session.get(SessionFile, doc_id)
        if file_row is None or file_row.status != "completed":
            return None
        if session_id and file_row.session_id != session_id:
            return None
        conditions = [
            SessionChunk.file_id == doc_id,
            SessionChunk.parent_id.is_(None),
        ]
        if session_id:
            conditions.append(SessionChunk.session_id == session_id)
        rows = (
            await session.execute(
                select(SessionChunk.id, SessionChunk.content)
                .where(*conditions)
                .order_by(SessionChunk.chunk_index, SessionChunk.id)
            )
        ).all()
        return SummarySourceSnapshot(
            source=source,
            doc_id=doc_id,
            kb_id=None,
            session_id=file_row.session_id,
            tenant_id=file_row.tenant_id,
            filename=file_row.filename,
            content_version=_version_for_rows([(row.id, row.content or "") for row in rows]),
            chunk_contents=[row.content or "" for row in rows if row.content],
        )


async def schedule_document_summary(
    *,
    source: str,
    doc_id: str,
    kb_id: str | None = None,
    session_id: str | None = None,
    tenant_id: str | None = None,
    force: bool = False,
) -> bool:
    """Persist a pending summary artifact and enqueue generation.

    The document ingestion path stays authoritative. A missing Redis queue or a
    summary-generation failure must never fail ingestion; callers therefore use
    this helper in a best-effort ``create_task`` wrapper.
    """
    snapshot = await load_summary_source(
        source,
        doc_id,
        kb_id=kb_id,
        session_id=session_id,
    )
    if snapshot is None:
        return False

    queue = get_document_summary_queue()
    if queue is None:
        logger.warning(
            "Summary queue unavailable; skipping artifact schedule source=%s doc_id=%s",
            source,
            doc_id,
        )
        return False

    async with async_session() as session:
        existing = await session.scalar(
            select(DocumentSummary).where(
                DocumentSummary.source == source,
                DocumentSummary.doc_id == doc_id,
            )
        )
        if (
            existing is not None
            and existing.content_version == snapshot.content_version
            and existing.status in {"pending", "processing", "completed"}
            and not force
        ):
            return False

        if existing is None:
            existing = DocumentSummary(
                id=str(uuid.uuid4()),
                source=source,
                doc_id=doc_id,
                kb_id=snapshot.kb_id,
                session_id=snapshot.session_id,
                tenant_id=snapshot.tenant_id,
                filename=snapshot.filename,
                content_version=snapshot.content_version,
                status="pending",
                chunk_count=len(snapshot.chunk_contents),
            )
            session.add(existing)
        else:
            existing.kb_id = snapshot.kb_id
            existing.session_id = snapshot.session_id
            existing.tenant_id = snapshot.tenant_id
            existing.filename = snapshot.filename
            existing.content_version = snapshot.content_version
            existing.status = "pending"
            existing.summary = None
            existing.gist = None
            existing.topics = None
            existing.doc_type = None
            existing.typical_question = None
            existing.chunk_count = len(snapshot.chunk_contents)
            existing.error_message = None
            existing.generated_at = None
        await session.commit()

    try:
        await queue.enqueue(
            DocumentSummaryTask(
                source=source,
                doc_id=doc_id,
                kb_id=snapshot.kb_id,
                session_id=snapshot.session_id,
                tenant_id=snapshot.tenant_id or tenant_id,
                filename=snapshot.filename,
            )
        )
    except Exception as exc:  # noqa: BLE001 - summary is an optimization
        logger.warning("Failed to enqueue summary source=%s doc_id=%s: %s", source, doc_id, exc)
        return False
    return True


async def mark_summary_failed(
    source: str,
    doc_id: str,
    error: str,
    *,
    content_version: str | None = None,
) -> None:
    """Record a terminal summary failure without affecting the source document."""
    async with async_session() as session:
        row = await session.scalar(
            select(DocumentSummary).where(
                DocumentSummary.source == source,
                DocumentSummary.doc_id == doc_id,
            )
        )
        if row is None:
            return
        if content_version and row.content_version != content_version:
            return
        row.status = "failed"
        row.error_message = error[:2000]
        row.generated_at = datetime.now(timezone.utc)
        await session.commit()


async def delete_document_summary(source: str, doc_id: str) -> None:
    """Delete a summary artifact after its source is removed."""
    from sqlalchemy import delete

    async with async_session() as session:
        await session.execute(
            delete(DocumentSummary).where(
                DocumentSummary.source == source,
                DocumentSummary.doc_id == doc_id,
            )
        )
        await session.commit()


async def delete_session_summaries(session_id: str) -> None:
    """Delete all summary artifacts owned by a deleted chat session."""
    from sqlalchemy import delete

    async with async_session() as session:
        await session.execute(
            delete(DocumentSummary).where(DocumentSummary.session_id == session_id)
        )
        await session.commit()


__all__ = [
    "SummarySourceSnapshot",
    "initialize_document_summary_queue",
    "close_document_summary_queue",
    "load_summary_source",
    "schedule_document_summary",
    "mark_summary_failed",
    "delete_document_summary",
    "delete_session_summaries",
]
