"""Unified knowledge retrieval tool.

``search_knowledge`` is the only model-facing recall primitive. It exposes
semantic, keyword, hybrid, and durable-summary modes while keeping source
authorization, caching, evidence registration, and retrieval post-processing in
one place.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from xml.sax.saxutils import escape as xml_escape

from app.agent.evidence import evidence_id_for, register_chunk_evidence, register_retrieval_evidence
from app.agent.harness import normalize_query
from app.agent.retrieval_postprocess import content_signature, postprocess_retrieval
from app.agent.state import AgentState
from app.agent.tools.base import BaseTool, ToolResult
from app.agent.tools.doc_files import build_tool_files
from app.retrieval.base import RetrievalResult
from app.retrieval.cache import RetrievalCache
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.log_safety import sanitize_for_log
from app.summary.repository import search_summary_artifacts

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 50
_SUMMARY_MAX_LIMIT = 100
_FAQ_ANSWER_RE = re.compile(
    r"(?:^|\n)\s*(?:A|答)\s*[：:]\s*(.+?)(?=\n\s*(?:Q|问)\s*[：:]|\Z)",
    re.DOTALL,
)
_ROUTES = {
    "hybrid": {"dense", "sparse", "bm25", "graph"},
    "semantic": {"dense", "sparse"},
    "keyword": {"bm25"},
}


@dataclass(frozen=True)
class SearchTarget:
    """Authorized source: a KB or the current session's uploaded files."""

    kb_id: str
    expr: str | None = None


class SearchKnowledgeTool(BaseTool):
    """Retrieve evidence from all authorized knowledge sources."""

    def __init__(
        self,
        retriever: HybridRetriever,
        *,
        search_targets: list[SearchTarget],
        state: AgentState | None = None,
        tenant_id: str | None = None,
        cache: RetrievalCache | None = None,
        session_id: str | None = None,
    ) -> None:
        self._retriever = retriever
        self._search_targets = list(search_targets)
        self._state = state
        self._tenant_id = tenant_id
        self._cache = cache
        self._session_id = session_id
        self._authorized_kb_ids = {
            target.kb_id for target in self._search_targets if target.expr is None
        }

    @property
    def name(self) -> str:
        return "search_knowledge"

    @property
    def description(self) -> str:
        return (
            "The single retrieval tool for authorized knowledge bases and files "
            "uploaded in the current conversation. Use `mode=hybrid` for normal "
            "questions, `semantic` for concepts and paraphrases, `keyword` for "
            "exact identifiers or phrases, and `summary` for fast document-level "
            "inventory or whole-knowledge-base summaries. Keep the query specific "
            "to one information need; call the tool again for a different need."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "One specific retrieval query. For summary mode it may be "
                        "empty to list all completed document profiles."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["hybrid", "semantic", "keyword", "summary"],
                    "default": "hybrid",
                    "description": (
                        "hybrid=fused semantic+keyword, semantic=meaning only, "
                        "keyword=exact lexical match, summary=durable document profiles."
                    ),
                },
                "knowledge_base_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional subset of authorized knowledge base IDs. IDs "
                        "outside the authorized set are ignored."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _SUMMARY_MAX_LIMIT,
                    "default": _DEFAULT_LIMIT,
                    "description": "Maximum number of results to return.",
                },
            },
            "required": ["query"],
        }

    async def execute(self, args: dict) -> ToolResult:
        query = normalize_query(args.get("query", ""))
        mode = str(args.get("mode") or "hybrid").strip().lower()
        if mode not in {"hybrid", "semantic", "keyword", "summary"}:
            return ToolResult(
                success=False,
                error="mode must be one of: hybrid, semantic, keyword, summary",
            )
        try:
            requested_limit = int(args.get("limit") or _DEFAULT_LIMIT)
        except (TypeError, ValueError):
            requested_limit = _DEFAULT_LIMIT
        max_limit = _SUMMARY_MAX_LIMIT if mode == "summary" else _MAX_LIMIT
        limit = max(1, min(requested_limit, max_limit))
        if not query and mode != "summary":
            return ToolResult(success=False, error="query must not be empty")

        targets = self._select_targets(args.get("knowledge_base_ids"))
        if mode == "summary":
            return await self._execute_summary(
                query=query,
                targets=targets,
                limit=limit,
            )
        if not targets:
            return ToolResult(success=False, error="no authorized search source")
        return await self._execute_retrieval(
            query=query,
            mode=mode,
            targets=targets,
            limit=limit,
        )

    def _select_targets(self, requested: object) -> list[SearchTarget]:
        if not isinstance(requested, list):
            return list(self._search_targets)
        selected = {str(item) for item in requested}
        authorized_subset = selected & self._authorized_kb_ids
        if not authorized_subset and self._authorized_kb_ids:
            return list(self._search_targets)
        return [
            target
            for target in self._search_targets
            if target.expr is not None
            or target.kb_id in authorized_subset
        ]

    async def _execute_retrieval(
        self,
        *,
        query: str,
        mode: str,
        targets: list[SearchTarget],
        limit: int,
    ) -> ToolResult:
        flat_targets = [target for target in targets for _ in range(1)]
        try:
            gathered = await asyncio.gather(
                *[
                    self._search_with_cache(
                        target,
                        query,
                        max(limit, 12),
                        mode=mode,
                    )
                    for target in flat_targets
                ],
                return_exceptions=True,
            )
        except Exception as exc:  # noqa: BLE001 - tool must return a bounded error
            logger.error("[search_knowledge] retrieval failed: %s", sanitize_for_log(exc))
            if self._state is not None:
                self._state.degraded = True
            return ToolResult(success=False, error=f"Search failed: {exc}")

        merged: list[RetrievalResult] = []
        cache_hits = 0
        for index, item in enumerate(gathered):
            target = flat_targets[index]
            if isinstance(item, Exception):
                if self._state is not None:
                    self._state.degraded = True
                    self._state.failed_source_ids.add(target.kb_id)
                logger.warning(
                    "[search_knowledge] source=%s failed: %s",
                    target.kb_id,
                    sanitize_for_log(item),
                )
                continue
            results, route_degraded, cache_hit = item
            cache_hits += int(cache_hit)
            if route_degraded and self._state is not None:
                self._state.degraded = True
                self._state.failed_source_ids.add(target.kb_id)
            for result in results:
                metadata = dict(result.metadata)
                metadata["source"] = (
                    "session-file" if target.expr is not None else "document"
                )
                metadata["knowledge_base_id"] = target.kb_id
                merged.append(
                    RetrievalResult(
                        chunk_id=result.chunk_id,
                        content=result.content,
                        score=result.score,
                        doc_id=result.doc_id,
                        metadata=metadata,
                        child_content=result.child_content,
                    )
                )

        processed = await postprocess_retrieval(
            merged,
            limit=limit,
            expand_neighbors=True,
        )
        results_with_status = self._mark_seen_chunks(processed.results)

        if not results_with_status:
            return ToolResult(
                success=True,
                output=(
                    '<search_results count="0">\n</search_results>\n'
                    "No relevant content found in the authorized knowledge sources."
                ),
                data={
                    "count": 0,
                    "new_count": 0,
                    "files": [],
                    "suggest_rewrite": True,
                    "cache_hits": cache_hits,
                    "retrieval_audit": processed.audit.as_dict(),
                },
            )

        if self._state is not None:
            for result, already_seen in results_with_status:
                if not already_seen:
                    self._state.knowledge_refs.append(result)
                register_retrieval_evidence(
                    self._state,
                    result,
                    source="search_knowledge",
                )

        files = await build_tool_files(
            [
                result.doc_id
                for result, already_seen in results_with_status
                if not already_seen
            ]
        )
        output = self._format_xml_output(results_with_status, processed.audit.as_dict())
        new_count = sum(1 for _result, seen in results_with_status if not seen)
        return ToolResult(
            success=True,
            output=output,
            data={
                "count": len(results_with_status),
                "new_count": new_count,
                "files": files,
                "suggest_rewrite": new_count == 0,
                "cache_hits": cache_hits,
                "retrieval_audit": processed.audit.as_dict(),
                "evidence_count": len(self._state.evidence) if self._state else 0,
            },
        )

    async def _execute_summary(
        self,
        *,
        query: str,
        targets: list[SearchTarget],
        limit: int,
    ) -> ToolResult:
        kb_ids = [
            target.kb_id
            for target in targets
            if target.expr is None and target.kb_id in self._authorized_kb_ids
        ]
        include_session = any(target.expr is not None for target in targets)
        profiles = await search_summary_artifacts(
            query=query,
            kb_ids=kb_ids,
            session_id=self._session_id if include_session else None,
            limit=limit,
        )

        if self._state is not None:
            for profile in profiles:
                chunk_id = f"summary:{profile['content_version']}"
                register_chunk_evidence(
                    self._state,
                    doc_id=profile["doc_id"],
                    chunk_id=chunk_id,
                    content=profile["summary"],
                    source="search_knowledge:summary",
                    metadata={
                        "document_summary": True,
                        "content_version": profile["content_version"],
                    },
                )

        lines = [
            f'<knowledge_summaries count="{len(profiles)}" '
            f'query="{xml_escape(query)}" '
            f'cutoff_applied="{str(len(profiles) >= limit).lower()}">'
        ]
        for rank, profile in enumerate(profiles, start=1):
            evidence_id = evidence_id_for(
                profile["doc_id"],
                f"summary:{profile['content_version']}",
            )
            lines.append(
                f'<document rank="{rank}" doc_id="{xml_escape(profile["doc_id"])}" '
                f'source="{xml_escape(profile["source"])}" '
                f'filename="{xml_escape(profile["filename"] or "")}" '
                f'score="{profile["score"]:.4f}" '
                f'chunk_count="{profile["chunk_count"]}" '
                f'evidence_id="{xml_escape(evidence_id)}">'
            )
            if profile.get("doc_type"):
                lines.append(
                    f'<doc_type>{xml_escape(str(profile["doc_type"]))}</doc_type>'
                )
            if profile.get("typical_question"):
                lines.append(
                    "<typical_question>"
                    f"{xml_escape(str(profile['typical_question']))}"
                    "</typical_question>"
                )
            if profile.get("topics"):
                lines.append(
                    f'<topics>{" | ".join(xml_escape(str(topic)) for topic in profile["topics"])}</topics>'
                )
            lines.append(f'<gist>{xml_escape(profile["gist"])}</gist>')
            lines.append(f'<summary>{xml_escape(profile["summary"])}</summary>')
            lines.append("</document>")
        lines.append("</knowledge_summaries>")
        if len(profiles) >= limit:
            lines.append(
                "[The summary result reached its bounded cutoff. Do not treat it "
                "as proof that no additional documents exist.]"
            )

        return ToolResult(
            success=True,
            output="\n".join(lines),
            data={
                "count": len(profiles),
                "new_count": len(profiles),
                "files": [
                    {
                        "id": profile["doc_id"],
                        "filename": profile["filename"],
                        "source": profile["source"],
                    }
                    for profile in profiles
                ],
                "suggest_rewrite": not profiles,
                "cache_hits": 0,
                "retrieval_audit": {
                    "raw_count": len(profiles),
                    "returned_count": len(profiles),
                    "limit": limit,
                    "cutoff_applied": len(profiles) >= limit,
                    "mode": "summary",
                },
            },
        )

    async def _search_with_cache(
        self,
        target: SearchTarget,
        query: str,
        top_k: int,
        *,
        mode: str,
    ) -> tuple[list[RetrievalResult], bool, bool]:
        cache_enabled = self._cache is not None and target.expr is None
        if cache_enabled:
            assert self._cache is not None
            cached = await self._cache.get(
                target.kb_id,
                query,
                mode,
                scope=target.expr or "",
                top_k=top_k,
                tenant_id=self._tenant_id,
            )
            if cached is not None:
                return cached, False, True

        results, route_degraded = await self._retriever.search_with_degraded(
            query=query,
            kb_id=target.kb_id,
            top_k=top_k,
            expr=target.expr,
            tenant_id=self._tenant_id,
            routes=_ROUTES[mode],
        )
        if cache_enabled and not route_degraded and results:
            assert self._cache is not None
            await self._cache.set(
                target.kb_id,
                query,
                mode,
                results,
                scope=target.expr or "",
                top_k=top_k,
                tenant_id=self._tenant_id,
            )
        return results, route_degraded, False

    def _mark_seen_chunks(
        self,
        results: list[RetrievalResult],
    ) -> list[tuple[RetrievalResult, bool]]:
        marked: list[tuple[RetrievalResult, bool]] = []
        for result in results:
            if not result.chunk_id or self._state is None:
                marked.append((result, False))
                continue
            already_seen = result.chunk_id in self._state.seen_chunk_ids
            self._state.seen_chunk_ids.add(result.chunk_id)
            marked.append((result, already_seen))
        return marked

    def _format_xml_output(
        self,
        results_with_status: list[tuple[RetrievalResult, bool]],
        audit: dict,
    ) -> str:
        lines = [
            f'<search_results count="{len(results_with_status)}" '
            f'raw_count="{audit.get("raw_count", len(results_with_status))}" '
            f'cutoff_applied="{str(bool(audit.get("cutoff_applied"))).lower()}">'
        ]
        for rank, (result, already_seen) in enumerate(results_with_status, start=1):
            metadata = result.metadata
            evidence_id = (
                self._state.evidence_by_chunk.get(result.chunk_id)
                if self._state is not None
                else evidence_id_for(result.doc_id, result.chunk_id)
            )
            source = str(metadata.get("source") or "document")
            parent_chunk_id = str(
                metadata.get("parent_chunk_id")
                or metadata.get("parent_id")
                or result.chunk_id
            )
            chunk_index = metadata.get("chunk_index", 0)
            attributes = [
                f'rank="{rank}"',
                f'chunk_id="{xml_escape(result.chunk_id)}"',
                f'doc_id="{xml_escape(result.doc_id)}"',
                f'source="{xml_escape(source)}"',
                f'score="{result.score:.4f}"',
                f'parent_chunk_id="{xml_escape(parent_chunk_id)}"',
                f'chunk_index="{xml_escape(str(chunk_index))}"',
                f'content_signature="{content_signature(result.content)}"',
            ]
            if evidence_id:
                attributes.append(f'evidence_id="{xml_escape(evidence_id)}"')
            if already_seen:
                attributes.append('status="already_retrieved"')
            if metadata.get("merged_parent_chunk_ids"):
                merged_ids = ",".join(
                    str(item) for item in metadata["merged_parent_chunk_ids"]
                )
                attributes.append(f'merged_parent_chunk_ids="{xml_escape(merged_ids)}"')
            lines.append(f"<chunk {' '.join(attributes)}>")
            if already_seen:
                lines.append("<content>(content omitted, already returned)</content>")
            else:
                lines.append(f"<content>{xml_escape(result.content)}</content>")
                faq_match = _FAQ_ANSWER_RE.search(result.content)
                if faq_match:
                    lines.append(
                        f"<faq_answer>{xml_escape(faq_match.group(1).strip())}</faq_answer>"
                    )
            lines.append("</chunk>")
        lines.append("</search_results>")
        if audit.get("cutoff_applied"):
            lines.append(
                "[Retrieval cutoff applied. Additional matching material may "
                "exist; refine the query if needed.]"
            )
        return "\n".join(lines)


__all__ = ["SearchKnowledgeTool", "SearchTarget"]
