"""Generic evidence identity shared by retrieval and read tools."""

from __future__ import annotations

import hashlib
from typing import Any

from app.agent.state import AgentState
from app.retrieval.base import RetrievalResult

_EXCERPT_CHARS = 320


def evidence_id_for(doc_id: str, chunk_id: str) -> str:
    raw = f"{doc_id}\0{chunk_id}".encode("utf-8")
    return f"ev_{hashlib.sha256(raw).hexdigest()[:20]}"


def register_retrieval_evidence(
    state: AgentState | None,
    result: RetrievalResult,
    *,
    source: str,
) -> tuple[str, bool]:
    """Register one retrieval result and return ``(evidence_id, is_new)``."""
    evidence_id = evidence_id_for(result.doc_id, result.chunk_id)
    if state is None:
        return evidence_id, True
    existing = state.evidence_by_chunk.get(result.chunk_id)
    if existing is not None:
        return existing, False

    state.evidence_by_chunk[result.chunk_id] = evidence_id
    state.evidence[evidence_id] = {
        "evidence_id": evidence_id,
        "doc_id": result.doc_id,
        "chunk_id": result.chunk_id,
        "source": source,
        "score": result.score,
        "excerpt": (result.child_content or result.content or "")[:_EXCERPT_CHARS],
        "metadata": dict(result.metadata or {}),
    }
    return evidence_id, True


def register_chunk_evidence(
    state: AgentState | None,
    *,
    doc_id: str,
    chunk_id: str,
    content: str,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """Register a chunk read through a document reader."""
    evidence_id = evidence_id_for(doc_id, chunk_id)
    if state is None:
        return evidence_id, True
    if chunk_id in state.evidence_by_chunk:
        return state.evidence_by_chunk[chunk_id], False
    state.evidence_by_chunk[chunk_id] = evidence_id
    state.evidence[evidence_id] = {
        "evidence_id": evidence_id,
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "source": source,
        "excerpt": content[:_EXCERPT_CHARS],
        "metadata": dict(metadata or {}),
    }
    return evidence_id, True
