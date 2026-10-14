"""list_knowledge_chunks 工具 - 按文档 ID 分页读取 chunk 内容

在 knowledge_search 找到相关文档后，使用此工具深度阅读该文档的完整内容。
按 chunk_index（位置）排序，支持分页浏览。
"""

import logging
import math

from sqlalchemy import func, select
from xml.sax.saxutils import escape as xml_escape

from app.agent.state import AgentState
from app.agent.tools.base import BaseTool, ToolResult
from app.schema.db import Chunk, SessionChunk
from app.storage.database import async_session

logger = logging.getLogger(__name__)


class ListKnowledgeChunksTool(BaseTool):
    """按文档 ID 分页读取 chunk 内容

    在 knowledge_search 找到相关文档后，使用此工具深度阅读该文档的完整内容。
    按 chunk 位置顺序返回，支持分页浏览大文档。
    """

    def __init__(self, state: AgentState | None = None):
        self._state = state

    @property
    def name(self) -> str:
        return "list_knowledge_chunks"

    @property
    def description(self) -> str:
        return "按文档 ID 分页读取 chunk 内容。在 knowledge_search 找到相关文档后，使用此工具深度阅读该文档的完整内容。"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "文档 ID，从 knowledge_search 结果的 doc_id 属性获取",
                },
                "page": {
                    "type": "integer",
                    "description": "页码，从 1 开始",
                    "default": 1,
                },
                "page_size": {
                    "type": "integer",
                    "description": "每页返回的 chunk 数量",
                    "default": 20,
                },
            },
            "required": ["doc_id"],
        }

    async def execute(self, args: dict) -> ToolResult:
        """按 doc_id 分页查询 Chunk 记录，按 chunk_index 排序"""
        doc_id: str = args.get("doc_id", "")
        page: int = args.get("page", 1)
        page_size: int = args.get("page_size", 20)

        if not doc_id:
            return ToolResult(success=False, error="doc_id parameter is required")

        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 20
        page_size = min(page_size, 100)

        logger.info(
            "[ListKnowledgeChunks] doc_id=%s, page=%d, page_size=%d",
            doc_id,
            page,
            page_size,
        )

        try:
            async with async_session() as session:
                # 仅读取父块，避免父子块同时返回造成正文重复。
                count_stmt = select(func.count()).select_from(Chunk).where(
                    Chunk.doc_id == doc_id,
                    Chunk.parent_id.is_(None),
                )
                total_chunks_result = await session.execute(count_stmt)
                total_chunks: int = total_chunks_result.scalar() or 0

                # 会话文件回退：正式库查不到时，doc_id 可能是会话文件 file_id，
                # 其 chunk 存于独立的 session_chunks 表。会话文件的 doc_id(=file_id) 对应
                # 该文件的全部父块（parent_id IS NULL），按父块即可拼出完整文档内容，
                # 避免父/子块同时返回导致内容重复。
                is_session_file = False
                if total_chunks == 0:
                    session_count_stmt = (
                        select(func.count())
                        .select_from(SessionChunk)
                        .where(
                            SessionChunk.file_id == doc_id,
                            SessionChunk.parent_id.is_(None),
                        )
                    )
                    session_count_result = await session.execute(session_count_stmt)
                    total_chunks = session_count_result.scalar() or 0
                    is_session_file = total_chunks > 0

                if total_chunks == 0:
                    return ToolResult(
                        success=True,
                        output=f'<document_chunks doc_id="{xml_escape(doc_id)}" page="1" total_pages="0" total_chunks="0">\n'
                        f"No chunks found for this document.\n"
                        f"</document_chunks>",
                        data={
                            "doc_id": doc_id,
                            "page": 1,
                            "page_size": page_size,
                            "total_pages": 0,
                            "total_chunks": 0,
                            "has_more": False,
                            "next_page": None,
                            "read_chunks": 0,
                            "fully_read": False,
                        },
                    )

                # 计算分页
                total_pages = math.ceil(total_chunks / page_size)
                if page > total_pages:
                    page = total_pages

                offset = (page - 1) * page_size

                # 按 chunk_index 排序分页查询（会话文件走 session_chunks 父块）
                if is_session_file:
                    query_stmt = (
                        select(SessionChunk)
                        .where(
                            SessionChunk.file_id == doc_id,
                            SessionChunk.parent_id.is_(None),
                        )
                        .order_by(SessionChunk.chunk_index)
                        .offset(offset)
                        .limit(page_size)
                    )
                else:
                    query_stmt = (
                        select(Chunk)
                        .where(
                            Chunk.doc_id == doc_id,
                            Chunk.parent_id.is_(None),
                        )
                        .order_by(Chunk.chunk_index)
                        .offset(offset)
                        .limit(page_size)
                    )
                result = await session.execute(query_stmt)
                chunks = result.scalars().all()

                read_chunks = len(chunks)
                fully_read = page >= total_pages
                if self._state is not None:
                    read_ranges = self._state.document_read_ranges.setdefault(
                        doc_id, set()
                    )
                    read_ranges.update(range(offset, offset + len(chunks)))
                    read_chunks = len(read_ranges)
                    fully_read = read_chunks >= total_chunks
                    catalog_entry = self._state.document_catalog.setdefault(doc_id, {})
                    catalog_entry.update({
                        "read_chunks": read_chunks,
                        "total_readable_chunks": total_chunks,
                        "fully_read": fully_read,
                    })

                output = self._format_xml_output(
                    doc_id=doc_id,
                    chunks=chunks,
                    page=page,
                    total_pages=total_pages,
                    total_chunks=total_chunks,
                    read_chunks=read_chunks,
                    fully_read=fully_read,
                )
                return ToolResult(
                    success=True,
                    output=output,
                    data={
                        "doc_id": doc_id,
                        "page": page,
                        "page_size": page_size,
                        "total_pages": total_pages,
                        "total_chunks": total_chunks,
                        "has_more": page < total_pages,
                        "next_page": page + 1 if page < total_pages else None,
                        "read_chunks": read_chunks,
                        "fully_read": fully_read,
                    },
                )

        except Exception as e:
            logger.error("[ListKnowledgeChunks] Database query failed: %s", e)
            return ToolResult(success=False, error=f"Database query failed: {e}")

    def _format_xml_output(
        self,
        doc_id: str,
        chunks: list,
        page: int,
        total_pages: int,
        total_chunks: int,
        read_chunks: int,
        fully_read: bool,
    ) -> str:
        """生成 XML 格式化输出

        格式：
        <document_chunks doc_id="..." page="1" total_pages="5" total_chunks="100">
          <chunk position="0" chunk_id="...">
            <content>...</content>
          </chunk>
          ...
        </document_chunks>
        """
        lines: list[str] = []
        lines.append(
            f'<document_chunks doc_id="{xml_escape(doc_id)}" '
            f'page="{page}" total_pages="{total_pages}" '
            f'total_chunks="{total_chunks}" read_chunks="{read_chunks}" '
            f'fully_read="{str(fully_read).lower()}">'
        )

        for chunk in chunks:
            position = chunk.chunk_index if chunk.chunk_index is not None else 0
            lines.append(
                f'<chunk position="{position}" chunk_id="{xml_escape(chunk.id)}">'
            )
            lines.append(f"<content>{xml_escape(chunk.content)}</content>")
            lines.append("</chunk>")

        lines.append("</document_chunks>")
        if fully_read:
            lines.append("[Document fully read.]")
        else:
            lines.append(
                f"[Document not fully read. Call list_knowledge_chunks again "
                f"with page={page + 1}.]"
            )
        return "\n".join(lines)
