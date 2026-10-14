"""list_knowledge_documents - enumerate documents available to the Agent.

This is the discovery primitive for knowledge-base collection tasks. Search
tools answer "where is this concept"; this tool answers "what documents exist".
It is intentionally domain-neutral and never interprets document contents.
"""

from __future__ import annotations

import logging
from xml.sax.saxutils import escape as xml_escape

from sqlalchemy import func, literal, select, union_all

from app.agent.state import AgentState
from app.agent.tools.base import BaseTool, ToolResult
from app.schema.db import Document, SessionFile
from app.storage.database import async_session

logger = logging.getLogger(__name__)

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 100


class ListKnowledgeDocumentsTool(BaseTool):
    """Enumerate formal KB documents and optional session files."""

    def __init__(
        self,
        *,
        kb_ids: list[str],
        session_id: str | None = None,
        include_session_source: bool = False,
        state: AgentState | None = None,
    ) -> None:
        self._kb_ids = list(dict.fromkeys(kb_ids))
        self._session_id = session_id
        self._include_session_source = bool(include_session_source and session_id)
        self._state = state

    @property
    def name(self) -> str:
        return "list_knowledge_documents"

    @property
    def description(self) -> str:
        return (
            "List the documents available in the authorized knowledge bases and, "
            "when present, the current conversation's uploaded files. Use this "
            "FIRST for requests to summarize, compare, inventory, or cover all "
            "documents. It returns stable doc_ids and filenames. Never infer a "
            "document's existence from filename numbering; use this list."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "kb_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional subset of authorized knowledge base IDs. "
                        "Session files are included when the runtime enables them."
                    ),
                },
                "page": {
                    "type": "integer",
                    "description": "Page number, starting from 1",
                    "default": 1,
                },
                "page_size": {
                    "type": "integer",
                    "description": "Documents per page (1-100)",
                    "default": _DEFAULT_PAGE_SIZE,
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Optional document status filter. Omit to list every status."
                    ),
                },
            },
        }

    async def execute(self, args: dict) -> ToolResult:
        page = max(1, int(args.get("page") or 1))
        page_size = int(args.get("page_size") or _DEFAULT_PAGE_SIZE)
        page_size = max(1, min(page_size, _MAX_PAGE_SIZE))
        status = str(args.get("status") or "").strip()

        requested = args.get("kb_ids")
        kb_ids = self._kb_ids
        if isinstance(requested, list):
            allowed = set(self._kb_ids)
            selected = {str(item) for item in requested}
            kb_ids = [kb_id for kb_id in self._kb_ids if kb_id in selected & allowed]
            if not kb_ids and self._kb_ids:
                # Match knowledge_search: an invalid subset never widens access,
                # but it falls back to all already-authorized KBs.
                kb_ids = list(self._kb_ids)

        if not kb_ids and not self._include_session_source:
            return ToolResult(
                success=False,
                error="No authorized knowledge base or session source is available.",
            )

        try:
            async with async_session() as session:
                selects = []
                if kb_ids:
                    document_conditions = [Document.kb_id.in_(kb_ids)]
                    if status:
                        document_conditions.append(Document.status == status)
                    selects.append(
                        select(
                            Document.id.label("doc_id"),
                            Document.filename.label("filename"),
                            Document.file_type.label("file_type"),
                            Document.file_size.label("file_size"),
                            Document.status.label("status"),
                            Document.chunk_count.label("chunk_count"),
                            Document.kb_id.label("kb_id"),
                            literal("document").label("source"),
                        ).where(*document_conditions)
                    )
                if self._include_session_source:
                    session_conditions = [SessionFile.session_id == self._session_id]
                    if status:
                        session_conditions.append(SessionFile.status == status)
                    selects.append(
                        select(
                            SessionFile.id.label("doc_id"),
                            SessionFile.filename.label("filename"),
                            SessionFile.file_type.label("file_type"),
                            SessionFile.file_size.label("file_size"),
                            SessionFile.status.label("status"),
                            SessionFile.chunk_count.label("chunk_count"),
                            literal("").label("kb_id"),
                            literal("session-file").label("source"),
                        ).where(*session_conditions)
                    )

                rows_source = selects[0] if len(selects) == 1 else union_all(*selects)
                rows_subquery = rows_source.subquery()
                total = int(
                    (
                        await session.execute(
                            select(func.count()).select_from(rows_subquery)
                        )
                    ).scalar()
                    or 0
                )
                offset = (page - 1) * page_size
                rows = (
                    await session.execute(
                        select(rows_subquery)
                        .order_by(
                            rows_subquery.c.source,
                            rows_subquery.c.filename,
                            rows_subquery.c.doc_id,
                        )
                        .offset(offset)
                        .limit(page_size)
                    )
                ).mappings().all()
        except Exception as exc:
            logger.exception("[ListKnowledgeDocuments] query failed: %s", exc)
            return ToolResult(success=False, error=f"Failed to list documents: {exc}")

        documents = [
            {
                "doc_id": row["doc_id"],
                "filename": row["filename"],
                "file_type": row["file_type"],
                "file_size": row["file_size"],
                "status": row["status"],
                "chunk_count": row["chunk_count"],
                "kb_id": row["kb_id"] or None,
                "source": row["source"],
            }
            for row in rows
        ]
        has_more = offset + len(documents) < total
        next_page = page + 1 if has_more else None

        if self._state is not None:
            for document in documents:
                current = self._state.document_catalog.setdefault(
                    document["doc_id"], {}
                )
                current.update(document)

        lines = [
            f'<documents total="{total}" page="{page}" '
            f'page_size="{page_size}" has_more="{str(has_more).lower()}">'
        ]
        for document in documents:
            attrs = [
                f'doc_id="{xml_escape(document["doc_id"])}"',
                f'source="{xml_escape(document["source"])}"',
                f'filename="{xml_escape(document["filename"])}"',
                f'type="{xml_escape(document["file_type"] or "")}"',
                f'status="{xml_escape(document["status"] or "")}"',
                f'chunk_count="{document["chunk_count"] or 0}"',
            ]
            if document["kb_id"]:
                attrs.append(f'kb_id="{xml_escape(document["kb_id"])}"')
            lines.append(f"<document {' '.join(attrs)} />")
        lines.append("</documents>")
        if next_page is not None:
            lines.append(
                f"[More documents are available. Call list_knowledge_documents "
                f"again with page={next_page}.]"
            )

        return ToolResult(
            success=True,
            output="\n".join(lines),
            data={
                "documents": documents,
                "total": total,
                "page": page,
                "page_size": page_size,
                "has_more": has_more,
                "next_page": next_page,
            },
        )
