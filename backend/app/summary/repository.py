"""Retrieval over durable document-summary artifacts."""

from __future__ import annotations

import logging

from sqlalchemy import or_, select

from app.retrieval.textutil import tokenize
from app.schema.db import DocumentSummary
from app.storage.database import async_session

logger = logging.getLogger(__name__)


def _profile_text(row: DocumentSummary) -> str:
    topics = " ".join(str(topic) for topic in (row.topics or []))
    return " ".join(
        part
        for part in (
            row.filename or "",
            row.gist or "",
            row.summary or "",
            row.doc_type or "",
            row.typical_question or "",
            topics,
        )
        if part
    )


def _score_profile(query: str, row: DocumentSummary) -> tuple[float, int]:
    query_tokens = tokenize(query)
    if not query_tokens:
        return 1.0, 0
    profile_tokens = tokenize(_profile_text(row))
    overlap = query_tokens & profile_tokens
    coverage = len(overlap) / max(1, len(query_tokens))
    precision = len(overlap) / max(1, len(profile_tokens))
    filename = (row.filename or "").casefold()
    substring_bonus = 0.15 if query.casefold() in filename else 0.0
    return coverage + precision * 0.35 + substring_bonus, len(overlap)


async def search_summary_artifacts(
    *,
    query: str,
    kb_ids: list[str],
    session_id: str | None,
    limit: int,
) -> list[dict]:
    """Return completed summary profiles ranked for one user query.

    Profiles are deliberately searched in PostgreSQL rather than copied into
    Milvus. They are small, versioned metadata records; keeping the authoritative
    copy in the same database as the source avoids a second consistency domain.
    """
    conditions = [DocumentSummary.status == "completed"]
    source_conditions = []
    if kb_ids:
        source_conditions.append(DocumentSummary.kb_id.in_(kb_ids))
    if session_id:
        source_conditions.append(
            (DocumentSummary.source == "session-file")
            & (DocumentSummary.session_id == session_id)
        )
    if source_conditions:
        conditions.append(or_(*source_conditions))
    else:
        return []

    # A generous bounded candidate set keeps ranking deterministic without
    # allowing an unbounded knowledge base to turn one tool call into a full scan
    # of an arbitrarily large table.
    candidate_limit = max(limit * 8, 64)
    async with async_session() as session:
        rows = (
            await session.execute(
                select(DocumentSummary)
                .where(*conditions)
                .order_by(DocumentSummary.updated_at.desc())
                .limit(candidate_limit)
            )
        ).scalars().all()

    ranked: list[tuple[float, int, DocumentSummary]] = []
    for row in rows:
        score, overlap = _score_profile(query, row)
        if query.strip() and overlap == 0 and not (
            query.casefold() in _profile_text(row).casefold()
        ):
            # For summary inventory, a non-empty query should still not hide
            # profiles when no lexical match exists: return low-scored recent
            # profiles so the model can explicitly ask for a narrower subset.
            score = 0.01
        ranked.append((score, overlap, row))
    ranked.sort(
        key=lambda item: (
            -item[0],
            -item[1],
            item[2].filename or "",
            item[2].doc_id,
        )
    )

    return [
        {
            "doc_id": row.doc_id,
            "kb_id": row.kb_id,
            "session_id": row.session_id,
            "source": row.source,
            "filename": row.filename,
            "content_version": row.content_version,
            "summary": row.summary or "",
            "gist": row.gist or "",
            "topics": list(row.topics or []),
            "doc_type": row.doc_type,
            "typical_question": row.typical_question,
            "chunk_count": row.chunk_count,
            "score": round(score, 6),
        }
        for score, _overlap, row in ranked[:limit]
    ]


__all__ = ["search_summary_artifacts"]
