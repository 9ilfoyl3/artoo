"""Deterministic post-processing for the unified knowledge retrieval tool."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select

from app.retrieval.base import RetrievalResult
from app.retrieval.textutil import jaccard, tokenize
from app.schema.db import Chunk, SessionChunk
from app.storage.database import async_session

_NEAR_DUPLICATE_THRESHOLD = 0.85
_NEIGHBOR_EXPANSION_LIMIT = 8
_MERGE_MAX_CHARS = 6_000
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class RetrievalAudit:
    """Bounded audit record attached to every retrieval tool result."""

    raw_count: int = 0
    after_id_dedup: int = 0
    after_signature_dedup: int = 0
    after_near_duplicate_dedup: int = 0
    expanded_count: int = 0
    merged_count: int = 0
    returned_count: int = 0
    limit: int = 0
    cutoff_applied: bool = False
    exact_duplicates_removed: int = 0
    signature_duplicates_removed: int = 0
    near_duplicates_removed: int = 0
    neighbor_chunks_added: int = 0
    sequential_groups_merged: int = 0

    def as_dict(self) -> dict:
        return {
            "raw_count": self.raw_count,
            "after_id_dedup": self.after_id_dedup,
            "after_signature_dedup": self.after_signature_dedup,
            "after_near_duplicate_dedup": self.after_near_duplicate_dedup,
            "expanded_count": self.expanded_count,
            "merged_count": self.merged_count,
            "returned_count": self.returned_count,
            "limit": self.limit,
            "cutoff_applied": self.cutoff_applied,
            "exact_duplicates_removed": self.exact_duplicates_removed,
            "signature_duplicates_removed": self.signature_duplicates_removed,
            "near_duplicates_removed": self.near_duplicates_removed,
            "neighbor_chunks_added": self.neighbor_chunks_added,
            "sequential_groups_merged": self.sequential_groups_merged,
        }


@dataclass
class RetrievalPostprocessResult:
    results: list[RetrievalResult] = field(default_factory=list)
    audit: RetrievalAudit = field(default_factory=RetrievalAudit)


def content_signature(content: str) -> str:
    """Stable exact-content signature after whitespace normalization."""
    normalized = _WHITESPACE_RE.sub(" ", (content or "").strip()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _copy_result(
    result: RetrievalResult,
    *,
    chunk_id: str | None = None,
    content: str | None = None,
    score: float | None = None,
    metadata: dict | None = None,
    child_content: str | None = None,
) -> RetrievalResult:
    return RetrievalResult(
        chunk_id=chunk_id if chunk_id is not None else result.chunk_id,
        content=content if content is not None else result.content,
        score=score if score is not None else result.score,
        doc_id=result.doc_id,
        metadata=dict(metadata if metadata is not None else result.metadata),
        child_content=(
            child_content if child_content is not None else result.child_content
        ),
    )


def _deduplicate_by_id(
    results: list[RetrievalResult],
) -> tuple[list[RetrievalResult], int]:
    best: dict[str, RetrievalResult] = {}
    order: list[str] = []
    for result in results:
        key = result.chunk_id or f"{result.doc_id}:{content_signature(result.content)}"
        if key not in best:
            best[key] = result
            order.append(key)
        elif result.score > best[key].score:
            best[key] = result
    return [best[key] for key in order], len(results) - len(best)


def _deduplicate_by_signature(
    results: list[RetrievalResult],
) -> tuple[list[RetrievalResult], int]:
    seen: dict[str, RetrievalResult] = {}
    order: list[str] = []
    for result in results:
        signature = content_signature(result.content)
        if signature not in seen:
            seen[signature] = result
            order.append(signature)
        elif result.score > seen[signature].score:
            seen[signature] = result
        result.metadata["content_signature"] = signature
    return [seen[signature] for signature in order], len(results) - len(seen)


def _deduplicate_near_duplicates(
    results: list[RetrievalResult],
    threshold: float = _NEAR_DUPLICATE_THRESHOLD,
) -> tuple[list[RetrievalResult], int]:
    kept: list[RetrievalResult] = []
    kept_tokens: list[frozenset[str]] = []
    removed = 0
    for result in sorted(results, key=lambda item: item.score, reverse=True):
        tokens = tokenize(result.content)
        duplicate = any(
            jaccard(tokens, existing) >= threshold for existing in kept_tokens
        )
        if duplicate:
            removed += 1
            continue
        kept.append(result)
        kept_tokens.append(tokens)
    return kept, removed


async def _load_parent_rows(
    results: list[RetrievalResult],
) -> dict[str, list[tuple[str, str, int]]]:
    doc_ids = list({result.doc_id for result in results if result.doc_id})
    if not doc_ids:
        return {}

    rows_by_doc: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    async with async_session() as session:
        formal_rows = (
            await session.execute(
                select(Chunk.id, Chunk.doc_id, Chunk.content, Chunk.chunk_index)
                .where(Chunk.doc_id.in_(doc_ids), Chunk.parent_id.is_(None))
                .order_by(Chunk.doc_id, Chunk.chunk_index, Chunk.id)
            )
        ).all()
        for row in formal_rows:
            rows_by_doc[row.doc_id].append(
                (row.id, row.content or "", int(row.chunk_index or 0))
            )

        missing = [doc_id for doc_id in doc_ids if doc_id not in rows_by_doc]
        if missing:
            session_rows = (
                await session.execute(
                    select(
                        SessionChunk.id,
                        SessionChunk.file_id,
                        SessionChunk.content,
                        SessionChunk.chunk_index,
                    )
                    .where(
                        SessionChunk.file_id.in_(missing),
                        SessionChunk.parent_id.is_(None),
                    )
                    .order_by(SessionChunk.file_id, SessionChunk.chunk_index, SessionChunk.id)
                )
            ).all()
            for row in session_rows:
                rows_by_doc[row.file_id].append(
                    (row.id, row.content or "", int(row.chunk_index or 0))
                )
    return rows_by_doc


async def _expand_neighbors(
    results: list[RetrievalResult],
    *,
    max_seed_results: int = _NEIGHBOR_EXPANSION_LIMIT,
) -> tuple[list[RetrievalResult], int]:
    if not results:
        return [], 0
    rows_by_doc = await _load_parent_rows(results)
    expanded = list(results)
    existing_ids = {result.chunk_id for result in results if result.chunk_id}
    added = 0

    for result in sorted(results, key=lambda item: item.score, reverse=True)[:max_seed_results]:
        rows = rows_by_doc.get(result.doc_id) or []
        if not rows:
            continue
        parent_id = str(result.metadata.get("parent_id") or result.chunk_id or "")
        index = next((idx for idx, row in enumerate(rows) if row[0] == parent_id), -1)
        if index < 0:
            # The hybrid retriever already returns parent content; use stable
            # chunk index metadata as a fallback when parent_id is absent.
            try:
                position = int(result.metadata.get("chunk_index") or 0)
            except (TypeError, ValueError):
                position = 0
            index = min(position, len(rows) - 1)

        for neighbor_idx in range(max(0, index - 1), min(len(rows), index + 2)):
            chunk_id, content, position = rows[neighbor_idx]
            if chunk_id in existing_ids or not content:
                continue
            metadata = dict(result.metadata)
            metadata.update(
                {
                    "parent_id": chunk_id,
                    "parent_chunk_id": chunk_id,
                    "chunk_index": position,
                    "expansion": "neighbor",
                    "seed_chunk_id": result.chunk_id,
                }
            )
            expanded.append(
                RetrievalResult(
                    chunk_id=chunk_id,
                    content=content,
                    score=max(0.0, result.score - 0.02),
                    doc_id=result.doc_id,
                    metadata=metadata,
                    child_content="",
                )
            )
            existing_ids.add(chunk_id)
            added += 1
    return expanded, added


def _parent_position(result: RetrievalResult) -> int:
    try:
        return int(result.metadata.get("chunk_index") or 0)
    except (TypeError, ValueError):
        return 0


def _merge_sequential(
    results: list[RetrievalResult],
) -> tuple[list[RetrievalResult], int]:
    """Merge adjacent parent chunks from the same source document.

    Merge is intentionally bounded by characters. If a run is too large, the
    original results are preserved so a single citation cannot silently cover
    an unbounded amount of content.
    """
    grouped: dict[str, list[RetrievalResult]] = defaultdict(list)
    passthrough: list[RetrievalResult] = []
    for result in results:
        if result.metadata.get("parent_id") or result.metadata.get("chunk_index") is not None:
            grouped[result.doc_id].append(result)
        else:
            passthrough.append(result)

    merged: list[RetrievalResult] = []
    merged_groups = 0
    for doc_id, items in grouped.items():
        ordered = sorted(items, key=_parent_position)
        current: list[RetrievalResult] = []
        for item in ordered:
            if not current:
                current = [item]
                continue
            previous = current[-1]
            adjacent = _parent_position(item) <= _parent_position(previous) + 1
            projected = sum(len(part.content) for part in current) + len(item.content)
            if adjacent and projected <= _MERGE_MAX_CHARS:
                current.append(item)
                continue
            if len(current) > 1:
                merged_groups += 1
                merged.append(_merge_group(current))
            else:
                merged.append(current[0])
            current = [item]
        if current:
            if len(current) > 1:
                merged_groups += 1
                merged.append(_merge_group(current))
            else:
                merged.append(current[0])

    return merged + passthrough, merged_groups


def _merge_group(group: list[RetrievalResult]) -> RetrievalResult:
    first = group[0]
    metadata = dict(first.metadata)
    metadata["merged_parent_chunk_ids"] = [item.chunk_id for item in group]
    metadata["merged_positions"] = [_parent_position(item) for item in group]
    metadata["merged_range"] = [
        min(_parent_position(item) for item in group),
        max(_parent_position(item) for item in group),
    ]
    return RetrievalResult(
        chunk_id=first.chunk_id,
        content="\n\n".join(item.content for item in group),
        score=max(item.score for item in group),
        doc_id=first.doc_id,
        metadata=metadata,
        child_content="\n\n".join(
            item.child_content for item in group if item.child_content
        ),
    )


async def postprocess_retrieval(
    results: list[RetrievalResult],
    *,
    limit: int,
    expand_neighbors: bool = True,
) -> RetrievalPostprocessResult:
    """Apply exact dedupe, near-dedupe, expansion, merge, and stable cutoff."""
    audit = RetrievalAudit(raw_count=len(results), limit=limit)
    deduped, audit.exact_duplicates_removed = _deduplicate_by_id(results)
    audit.after_id_dedup = len(deduped)

    deduped, audit.signature_duplicates_removed = _deduplicate_by_signature(deduped)
    audit.after_signature_dedup = len(deduped)

    deduped, audit.near_duplicates_removed = _deduplicate_near_duplicates(deduped)
    audit.after_near_duplicate_dedup = len(deduped)

    if expand_neighbors:
        deduped, audit.neighbor_chunks_added = await _expand_neighbors(deduped)
    audit.expanded_count = len(deduped)

    deduped, audit.sequential_groups_merged = _merge_sequential(deduped)
    audit.merged_count = len(deduped)

    stable = sorted(
        deduped,
        key=lambda item: (
            -item.score,
            str(item.metadata.get("source") or ""),
            item.doc_id,
            _parent_position(item),
            item.chunk_id,
        ),
    )
    returned = stable[: max(1, int(limit))]
    audit.returned_count = len(returned)
    audit.cutoff_applied = len(stable) > len(returned)
    return RetrievalPostprocessResult(results=returned, audit=audit)


__all__ = [
    "RetrievalAudit",
    "RetrievalPostprocessResult",
    "content_signature",
    "postprocess_retrieval",
]
