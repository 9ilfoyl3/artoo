"""Authorized, bounded document reader.

``read_document`` reads parent chunks by stable ``doc_id`` or ``chunk_id``.
It supports deterministic offset pagination, lexical/regex lookup, and coverage
tracking. Authorization is enforced against the same KB/session scope used by
the retrieval tool.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from xml.sax.saxutils import escape as xml_escape

from sqlalchemy import select

from app.agent.evidence import evidence_id_for, register_chunk_evidence
from app.agent.state import AgentState
from app.agent.tools.base import BaseTool, ToolResult
from app.schema.db import Chunk, Document, SessionChunk, SessionFile
from app.storage.database import async_session

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 12_000
_MAX_LIMIT = 30_000
_DEFAULT_CONTEXT = 300
_MAX_CONTEXT = 2_000
_MAX_MATCHES = 20
_CHUNK_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class _ParentChunk:
    chunk_id: str
    content: str
    index: int


@dataclass(frozen=True)
class _DocumentScope:
    doc_id: str
    filename: str
    source: str
    chunks: list[_ParentChunk]

    @property
    def content(self) -> str:
        return _CHUNK_SEPARATOR.join(chunk.content for chunk in self.chunks)


class ReadDocumentTool(BaseTool):
    """Read one authorized document with bounded output."""

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
        return "read_document"

    @property
    def description(self) -> str:
        return (
            "Read one authorized document by doc_id or chunk_id. Use offset/limit "
            "for sequential reading, or query/regex with context to locate exact "
            "material. Follow next_offset until fully_read=true for full coverage."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "Stable document ID returned by retrieval or list_documents.",
                },
                "chunk_id": {
                    "type": "string",
                    "description": "Optional chunk ID; the containing document is resolved automatically.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Character offset for sequential reading.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_LIMIT,
                    "default": _DEFAULT_LIMIT,
                    "description": "Maximum characters to return.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional case-insensitive literal text to locate.",
                },
                "regex": {
                    "type": "string",
                    "description": "Optional regular expression to locate.",
                },
                "context": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": _MAX_CONTEXT,
                    "default": _DEFAULT_CONTEXT,
                    "description": "Characters of context around each match.",
                },
            },
            "anyOf": [
                {"required": ["doc_id"]},
                {"required": ["chunk_id"]},
            ],
        }

    async def execute(self, args: dict) -> ToolResult:
        doc_id = str(args.get("doc_id") or "").strip()
        chunk_id = str(args.get("chunk_id") or "").strip()
        if not doc_id and not chunk_id:
            return ToolResult(success=False, error="doc_id or chunk_id is required")
        if chunk_id and not doc_id:
            doc_id = await self._resolve_doc_id_from_chunk(chunk_id)
            if not doc_id:
                return ToolResult(
                    success=False,
                    error="chunk_id is not visible in the authorized document scope",
                )

        scope = await self._load_document_scope(doc_id)
        if scope is None:
            return ToolResult(
                success=False,
                error="document is not visible in the authorized document scope",
            )
        if not scope.chunks:
            return ToolResult(
                success=True,
                output=(
                    f'<document doc_id="{xml_escape(doc_id)}" '
                    'total_chars="0" fully_read="true" />'
                ),
                data={
                    "doc_id": doc_id,
                    "filename": scope.filename,
                    "source": scope.source,
                    "total_chars": 0,
                    "next_offset": None,
                    "fully_read": True,
                    "matches": 0,
                },
            )

        try:
            limit = int(args.get("limit") or _DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = _DEFAULT_LIMIT
        try:
            context = int(args.get("context") or _DEFAULT_CONTEXT)
        except (TypeError, ValueError):
            context = _DEFAULT_CONTEXT
        limit = max(1, min(limit, _MAX_LIMIT))
        context = max(0, min(context, _MAX_CONTEXT))

        query = str(args.get("query") or "").strip()
        regex = str(args.get("regex") or "").strip()
        if query and regex:
            return ToolResult(success=False, error="provide query or regex, not both")
        if regex:
            if len(regex) > 200:
                return ToolResult(success=False, error="regex is too long")
            try:
                compiled = re.compile(regex, re.IGNORECASE)
            except re.error as exc:
                return ToolResult(success=False, error=f"invalid regex: {exc}")
            return self._execute_match_mode(
                scope=scope,
                pattern=compiled,
                context=context,
                limit=limit,
            )
        if query:
            return self._execute_match_mode(
                scope=scope,
                pattern=re.compile(re.escape(query), re.IGNORECASE),
                context=context,
                limit=limit,
            )

        try:
            offset = int(args.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        offset = max(0, offset)
        if chunk_id and offset == 0:
            offset = self._offset_for_chunk(scope, chunk_id)
        return self._execute_sequential(
            scope=scope,
            offset=offset,
            limit=limit,
        )

    async def _resolve_doc_id_from_chunk(self, chunk_id: str) -> str | None:
        async with async_session() as session:
            if self._kb_ids:
                row = (
                    await session.execute(
                        select(Chunk.doc_id)
                        .join(Document, Document.id == Chunk.doc_id)
                        .where(
                            Chunk.id == chunk_id,
                            Document.kb_id.in_(self._kb_ids),
                        )
                    )
                ).scalar_one_or_none()
                if row:
                    return str(row)
            if self._include_session_source:
                session_row = (
                    await session.execute(
                        select(SessionChunk.file_id)
                        .join(SessionFile, SessionFile.id == SessionChunk.file_id)
                        .where(
                            SessionChunk.id == chunk_id,
                            SessionChunk.session_id == self._session_id,
                            SessionFile.session_id == self._session_id,
                        )
                    )
                ).scalar_one_or_none()
                if session_row:
                    return str(session_row)
        return None

    async def _load_document_scope(self, doc_id: str) -> _DocumentScope | None:
        async with async_session() as session:
            if self._kb_ids:
                document = (
                    await session.execute(
                        select(Document).where(
                            Document.id == doc_id,
                            Document.kb_id.in_(self._kb_ids),
                        )
                    )
                ).scalar_one_or_none()
                if document is not None:
                    rows = (
                        await session.execute(
                            select(Chunk.id, Chunk.content, Chunk.chunk_index)
                            .where(
                                Chunk.doc_id == doc_id,
                                Chunk.parent_id.is_(None),
                            )
                            .order_by(Chunk.chunk_index, Chunk.id)
                        )
                    ).all()
                    return _DocumentScope(
                        doc_id=doc_id,
                        filename=document.filename,
                        source="document",
                        chunks=[
                            _ParentChunk(
                                chunk_id=row.id,
                                content=row.content or "",
                                index=int(row.chunk_index or position),
                            )
                            for position, row in enumerate(rows)
                        ],
                    )

            if self._include_session_source:
                file_row = (
                    await session.execute(
                        select(SessionFile).where(
                            SessionFile.id == doc_id,
                            SessionFile.session_id == self._session_id,
                        )
                    )
                ).scalar_one_or_none()
                if file_row is not None:
                    rows = (
                        await session.execute(
                            select(
                                SessionChunk.id,
                                SessionChunk.content,
                                SessionChunk.chunk_index,
                            )
                            .where(
                                SessionChunk.file_id == doc_id,
                                SessionChunk.session_id == self._session_id,
                                SessionChunk.parent_id.is_(None),
                            )
                            .order_by(SessionChunk.chunk_index, SessionChunk.id)
                        )
                    ).all()
                    return _DocumentScope(
                        doc_id=doc_id,
                        filename=file_row.filename,
                        source="session-file",
                        chunks=[
                            _ParentChunk(
                                chunk_id=row.id,
                                content=row.content or "",
                                index=int(row.chunk_index or position),
                            )
                            for position, row in enumerate(rows)
                        ],
                    )
        return None

    @staticmethod
    def _offset_for_chunk(scope: _DocumentScope, chunk_id: str) -> int:
        offset = 0
        for chunk in scope.chunks:
            if chunk.chunk_id == chunk_id:
                return offset
            offset += len(chunk.content) + len(_CHUNK_SEPARATOR)
        return 0

    def _execute_sequential(
        self,
        *,
        scope: _DocumentScope,
        offset: int,
        limit: int,
    ) -> ToolResult:
        content = scope.content
        total_chars = len(content)
        end = min(total_chars, offset + limit)
        excerpt = content[offset:end]
        returned_chunks = self._chunks_in_range(
            scope,
            start=offset,
            end=end,
        )
        self._register_reads(scope, returned_chunks)
        fully_read = end >= total_chars
        next_offset = None if fully_read else end
        output = self._format_output(
            scope=scope,
            body=excerpt,
            mode="read",
            offset=offset,
            end=end,
            total_chars=total_chars,
            fully_read=fully_read,
            next_offset=next_offset,
        )
        return ToolResult(
            success=True,
            output=output,
            data={
                "doc_id": scope.doc_id,
                "filename": scope.filename,
                "source": scope.source,
                "offset": offset,
                "end_offset": end,
                "total_chars": total_chars,
                "next_offset": next_offset,
                "fully_read": fully_read,
                "read_chunk_ids": [chunk.chunk_id for chunk in returned_chunks],
                "matches": 0,
            },
        )

    def _execute_match_mode(
        self,
        *,
        scope: _DocumentScope,
        pattern: re.Pattern[str],
        context: int,
        limit: int,
    ) -> ToolResult:
        remaining = limit
        windows: list[tuple[_ParentChunk, int, int, str]] = []
        for chunk in scope.chunks:
            for match in pattern.finditer(chunk.content):
                start = max(0, match.start() - context)
                end = min(len(chunk.content), match.end() + context)
                window = chunk.content[start:end]
                if len(window) > remaining:
                    window = window[:remaining]
                    end = start + len(window)
                windows.append((chunk, start, end, window))
                remaining -= len(window)
                if len(windows) >= _MAX_MATCHES or remaining <= 0:
                    break
            if len(windows) >= _MAX_MATCHES or remaining <= 0:
                break

        self._register_reads(scope, [window[0] for window in windows])
        body = "\n\n".join(
            (
                f'<match chunk_id="{xml_escape(chunk.chunk_id)}" '
                f'chunk_index="{chunk.index}" start="{start}" end="{end}">\n'
                f"{xml_escape(window)}\n</match>"
            )
            for chunk, start, end, window in windows
        )
        output = self._format_output(
            scope=scope,
            body=body,
            mode="match",
            offset=0,
            end=len(body),
            total_chars=len(scope.content),
            fully_read=False,
            next_offset=None,
            match_count=len(windows),
        )
        return ToolResult(
            success=True,
            output=output,
            data={
                "doc_id": scope.doc_id,
                "filename": scope.filename,
                "source": scope.source,
                "matches": len(windows),
                "match_windows": [
                    {
                        "chunk_id": chunk.chunk_id,
                        "chunk_index": chunk.index,
                        "start": start,
                        "end": end,
                    }
                    for chunk, start, end, _window in windows
                ],
                "fully_read": False,
                "next_offset": None,
                "total_chars": len(scope.content),
            },
        )

    @staticmethod
    def _chunks_in_range(
        scope: _DocumentScope,
        *,
        start: int,
        end: int,
    ) -> list[_ParentChunk]:
        cursor = 0
        selected: list[_ParentChunk] = []
        for chunk in scope.chunks:
            chunk_start = cursor
            chunk_end = cursor + len(chunk.content)
            if chunk_end > start and chunk_start < end:
                selected.append(chunk)
            cursor = chunk_end + len(_CHUNK_SEPARATOR)
        return selected

    def _register_reads(
        self,
        scope: _DocumentScope,
        chunks: list[_ParentChunk],
    ) -> None:
        if self._state is None:
            return
        for chunk in chunks:
            register_chunk_evidence(
                self._state,
                doc_id=scope.doc_id,
                chunk_id=chunk.chunk_id,
                content=chunk.content,
                source=scope.source,
                metadata={"chunk_index": chunk.index},
            )
            self._state.document_read_ranges.setdefault(scope.doc_id, set()).add(
                chunk.index
            )
        read_ranges = self._state.document_read_ranges.get(scope.doc_id, set())
        fully_read = len(read_ranges) >= len(scope.chunks)
        self._state.document_catalog.setdefault(scope.doc_id, {}).update(
            {
                "filename": scope.filename,
                "source": scope.source,
                "read_chunks": len(read_ranges),
                "total_readable_chunks": len(scope.chunks),
                "fully_read": fully_read,
            }
        )

    def _format_output(
        self,
        *,
        scope: _DocumentScope,
        body: str,
        mode: str,
        offset: int,
        end: int,
        total_chars: int,
        fully_read: bool,
        next_offset: int | None,
        match_count: int = 0,
    ) -> str:
        lines = [
            f'<document doc_id="{xml_escape(scope.doc_id)}" '
            f'filename="{xml_escape(scope.filename)}" '
            f'source="{xml_escape(scope.source)}" '
            f'mode="{mode}" offset="{offset}" end_offset="{end}" '
            f'total_chars="{total_chars}" matches="{match_count}" '
            f'fully_read="{str(fully_read).lower()}">',
            body,
            "</document>",
        ]
        if next_offset is not None:
            lines.append(
                f"[More content is available. Call read_document with "
                f"doc_id=\"{scope.doc_id}\" offset={next_offset}.]"
            )
        elif mode == "read":
            lines.append("[Document fully read.]")
        return "\n".join(lines)


__all__ = ["ReadDocumentTool"]
