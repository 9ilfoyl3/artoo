"""knowledge_search 工具 - 语义检索

通过 HybridRetriever（Dense + Sparse + BM25 + RRF + Rerank）执行语义检索，
支持多 query 并发检索、chunk_id 去重、跨调用 seen_chunks 去重，
输出 XML 格式供 LLM 解析。

检索源由 ``SearchTarget`` 列表描述（agent-session-source-unification）：每个源 = kb_id +
可选 Milvus 标量过滤 expr。正式知识库 expr=None；会话文件源 kb_id=SESSION_FILES_KB_ID
且 expr='session_id == "{sid}"'（会话级隔离）。这样会话文件与正式知识库能作为平等的
检索源由 agent 多轮检索，不再靠"换检索路径"接入。
"""

import asyncio
import logging
from dataclasses import dataclass
from xml.sax.saxutils import escape as xml_escape

from app.agent.harness import dedupe_queries
from app.agent.state import AgentState
from app.agent.tools.base import BaseTool, ToolResult
from app.agent.tools.doc_files import build_tool_files
from app.retrieval.base import RetrievalResult
from app.retrieval.cache import RetrievalCache
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.log_safety import sanitize_for_log

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchTarget:
    """一个 agent 检索源：kb_id + 可选 Milvus 标量过滤 expr。

    - 正式知识库：``expr=None``（无 session 概念）。
    - 会话文件源：``kb_id=SESSION_FILES_KB_ID``、``expr='session_id == "{sid}"'``
      （会话级隔离，跨会话不泄露）。

    expr 透传给 ``HybridRetriever.search_with_degraded(expr=...)`` 做 Milvus pre-filter。
    """

    kb_id: str
    expr: str | None = None



class KnowledgeSearchTool(BaseTool):
    """语义检索工具 - 基于 HybridRetriever 的多 query 语义搜索

    使用向量嵌入理解查询意图，从知识库中检索语义相关的内容块。
    内部调用 HybridRetriever（Dense + BM25 + 可选 Sparse + RRF 融合 + Rerank）。
    支持多知识库并发检索。
    """

    def __init__(
        self,
        retriever: HybridRetriever,
        kb_id: str | None = None,
        state: AgentState | None = None,
        knowledge_base_ids: list[str] | None = None,
        tenant_id: str | None = None,
        search_targets: list[SearchTarget] | None = None,
        dedupe_queries_enabled: bool = True,
        max_queries_per_call: int = 3,
        cache: RetrievalCache | None = None,
    ):
        self._retriever = retriever
        self._state = state
        # 显式租户 ID（H5）：透传给底层 hybrid.search，避免 agent 模式在流式响应中
        # contextvar 已 reset 时丢失租户检索配置；None 时底层回退 contextvar。
        self._tenant_id = tenant_id
        self._dedupe_queries_enabled = dedupe_queries_enabled
        self._max_queries_per_call = max(1, min(int(max_queries_per_call), 3))
        self._cache = cache

        # 检索源装配（agent-session-source-unification）：
        # 优先用显式 search_targets（新装配层传入，可含会话源 + 多库）；
        # 否则由旧入参 kb_id / knowledge_base_ids 派生（向后兼容既有调用点与测试）。
        if search_targets:
            self._search_targets: list[SearchTarget] = list(search_targets)
        else:
            kb_ids = knowledge_base_ids or ([kb_id] if kb_id else [])
            self._search_targets = [SearchTarget(kb_id=k, expr=None) for k in kb_ids]

        # 已授权的正式 KB 源集合（expr=None 的源）：用于约束 LLM 经 knowledge_base_ids
        # 入参只能在该集合内取子集，不能引入新 kb，也不能指定/伪造会话源（Property 5）。
        self._authorized_kb_ids: set[str] = {
            t.kb_id for t in self._search_targets if t.expr is None
        }

    @classmethod
    def from_kb_ids(
        cls,
        retriever: HybridRetriever,
        kb_ids: list[str],
        state: AgentState,
        tenant_id: str | None = None,
    ) -> "KnowledgeSearchTool":
        """便捷工厂：从知识库 id 列表构造（全部为正式 KB 源，expr=None）。"""
        targets = [SearchTarget(kb_id=k, expr=None) for k in kb_ids]
        return cls(retriever=retriever, state=state, tenant_id=tenant_id, search_targets=targets)


    @property
    def name(self) -> str:
        return "knowledge_search"

    @property
    def description(self) -> str:
        return (
            "Semantic search tool for retrieving knowledge by meaning, intent, and conceptual relevance. "
            "Uses embeddings to find semantically similar content across the authorized knowledge bases "
            "and, if present, the files uploaded in the current conversation. "
            "Designed for conceptual explanations, topic overviews, reasoning-based information needs, "
            "and intent-driven retrieval. Searches by MEANING rather than exact text. "
            "Each query should be a short, well-formed semantic question or conceptual statement. "
            "Avoid keyword lists or raw text from user messages."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "description": "1-3 semantic questions or conceptual statements for retrieval",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 3,
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of top results to return per query",
                    "default": 10,
                },
                "knowledge_base_ids": {
                    "type": "array",
                    "description": (
                        "Optional subset of the authorized knowledge base IDs to search across. "
                        "If omitted, searches all authorized sources (including this conversation's "
                        "uploaded files when present). IDs outside the authorized set are ignored."
                    ),
                    "items": {"type": "string"},
                },
            },
            "required": ["queries"],
        }

    async def execute(self, args: dict) -> ToolResult:
        """执行语义检索

        流程：多源 × 多 query 并发检索 → 合并 → chunk_id 去重 → seen_chunks 跨调用去重 → XML 格式化
        每个源（SearchTarget）携带其专属 expr（会话源带 session_id 隔离，KB 源不带）。
        若 LLM 经 ``knowledge_base_ids`` 指定子集，仅在已授权正式 KB 源内取交集
        （不引入新 kb、不影响会话源是否参与——后者由装配层决定，Property 5）。
        """
        raw_queries = args.get("queries", [])
        if self._dedupe_queries_enabled:
            queries = dedupe_queries(raw_queries, limit=self._max_queries_per_call)
        else:
            queries = [
                query.strip()
                for query in raw_queries
                if isinstance(query, str) and query.strip()
            ][:self._max_queries_per_call]
        top_k: int = args.get("top_k", 10)
        kb_ids_param: list[str] | None = args.get("knowledge_base_ids")

        if not queries:
            return ToolResult(
                success=False,
                error="queries parameter must contain at least one non-empty query",
            )

        # 确定本次检索的源列表：
        # - LLM 指定了 knowledge_base_ids → 在已授权正式 KB 源内取交集 + 始终保留会话源
        #   （会话源是否存在由装配层 search_targets 决定，LLM 不能指定/移除）。
        # - 未指定 → 用全部已配置 search_targets。
        if kb_ids_param:
            requested = set(kb_ids_param) & self._authorized_kb_ids
            if not requested and self._authorized_kb_ids:
                # LLM 指定了 KB 但全部不在授权范围（幻觉/伪造）→ 忽略其 KB 选择，
                # 回退到全部已授权 KB 源 + 会话源，避免被误窄化到仅会话源（Property 5）。
                targets = list(self._search_targets)
            else:
                targets = [
                    t for t in self._search_targets
                    if (t.expr is None and t.kb_id in requested) or t.expr is not None
                ]
        else:
            targets = list(self._search_targets)

        if not targets:
            return ToolResult(success=False, error="no authorized search source")

        logger.info(
            "[KnowledgeSearch] Executing with %d queries, top_k=%d, targets=%s (session_source=%s)",
            len(queries),
            top_k,
            [t.kb_id for t in targets],
            any(t.expr is not None for t in targets),
        )

        # 对每个 target × query 组合并发检索，每个源透传其 expr（会话源带 session_id 隔离）。
        # H3：用 search_with_degraded 取本次各路是否路级降级（经返回结构承载，并发安全）。
        flat_targets = [t for t in targets for _ in queries]
        try:
            search_tasks = [
                self._search_with_cache(t, q, top_k)
                for t in targets
                for q in queries
            ]
            all_results_nested: list = await asyncio.gather(
                *search_tasks, return_exceptions=True
            )
        except Exception as e:
            logger.error("[KnowledgeSearch] Search failed: %s", sanitize_for_log(e))
            if self._state is not None:
                self._state.degraded = True
            return ToolResult(success=False, error=f"Search failed: {e}")

        # 合并所有结果，过滤异常
        # 任一子检索抛异常（某源/query 失败）或返回 degraded=True（三路路级降级）→ 标记降级。
        merged: list[RetrievalResult] = []
        cache_hits = 0
        for i, item in enumerate(all_results_nested):
            failed_kb = flat_targets[i].kb_id if i < len(flat_targets) else None
            if isinstance(item, Exception):
                logger.warning(
                    "[KnowledgeSearch] Source '%s' query '%s' failed: %s",
                    failed_kb or "?",
                    sanitize_for_log(queries[i % len(queries)] if queries else ""),
                    sanitize_for_log(item),
                )
                if self._state is not None:
                    self._state.degraded = True
                    if failed_kb:
                        self._state.failed_source_ids.add(failed_kb)
                continue
            result, route_degraded, cache_hit = item
            if cache_hit:
                cache_hits += 1
            if route_degraded and self._state is not None:
                self._state.degraded = True
                if failed_kb:
                    self._state.failed_source_ids.add(failed_kb)
            merged.extend(result)

        logger.info("[KnowledgeSearch] Merged %d raw results from %d queries × %d sources", len(merged), len(queries), len(targets))

        # chunk_id 去重：保留最高分
        deduped = self._deduplicate_by_chunk_id(merged)

        # 按 score 降序排序
        deduped.sort(key=lambda r: r.score, reverse=True)

        # 截取 top_k
        deduped = deduped[:top_k]

        logger.info("[KnowledgeSearch] After dedup and top_k: %d results", len(deduped))

        # 跨调用 seen_chunks 去重标记
        results_with_status = self._mark_seen_chunks(deduped)

        # 无结果处理
        if not results_with_status:
            output = (
                "<search_results count=\"0\">\n"
                "</search_results>\n"
                "No relevant content found in knowledge base.\n"
                "DO NOT use training data or general knowledge to answer. "
                "State that no relevant information was found."
            )
            return ToolResult(
                success=True,
                output=output,
                data={
                    "count": 0,
                    "new_count": 0,
                    "files": [],
                    "suggest_rewrite": True,
                    "cache_hits": cache_hits,
                },
            )

        # XML 格式化输出
        output = self._format_xml_output(results_with_status)

        # 收集引用到 AgentState（state 可能为 None：仅在无状态调用/测试场景）
        if self._state is not None:
            for result, _seen in results_with_status:
                if not _seen:
                    self._state.knowledge_refs.append(result)

        # 解析本次读到的文件（doc_id → 文件名/来源），供前端在工具步骤行内联展示可点击预览。
        # 仅纳入本次新命中（非 already_retrieved）的结果，避免重复罗列此前已展示过的文件。
        files = await build_tool_files(
            [result.doc_id for result, seen in results_with_status if not seen]
        )
        new_count = sum(1 for _, seen in results_with_status if not seen)

        return ToolResult(
            success=True,
            output=output,
            data={
                "count": len(results_with_status),
                "new_count": new_count,
                "files": files,
                "suggest_rewrite": new_count == 0,
                "cache_hits": cache_hits,
            },
        )

    async def _search_with_cache(
        self,
        target: SearchTarget,
        query: str,
        top_k: int,
    ) -> tuple[list[RetrievalResult], bool, bool]:
        """Search one target/query pair, using an exact-scope cache when available."""
        # Session sources carry mutable per-session data and do not yet have a
        # dedicated generation key, so only stable formal KB targets are cached.
        cache_enabled = self._cache is not None and target.expr is None
        cache_scope = target.expr or ""
        if cache_enabled:
            assert self._cache is not None
            cached = await self._cache.get(
                target.kb_id,
                query,
                "agent",
                scope=cache_scope,
                top_k=top_k,
                tenant_id=self._tenant_id,
            )
            if cached is not None:
                return cached, False, True

        result, route_degraded = await self._retriever.search_with_degraded(
            query=query,
            kb_id=target.kb_id,
            top_k=top_k,
            expr=target.expr,
            tenant_id=self._tenant_id,
        )
        if cache_enabled and not route_degraded and result:
            assert self._cache is not None
            await self._cache.set(
                target.kb_id,
                query,
                "agent",
                result,
                scope=cache_scope,
                top_k=top_k,
                tenant_id=self._tenant_id,
            )
        return result, route_degraded, False

    def _deduplicate_by_chunk_id(
        self, results: list[RetrievalResult]
    ) -> list[RetrievalResult]:
        """按 chunk_id 去重，保留最高分的结果"""
        best: dict[str, RetrievalResult] = {}
        for r in results:
            if r.chunk_id not in best or r.score > best[r.chunk_id].score:
                best[r.chunk_id] = r
        return list(best.values())

    def _mark_seen_chunks(
        self, results: list[RetrievalResult]
    ) -> list[tuple[RetrievalResult, bool]]:
        """标记已在之前调用中返回过的 chunk

        chunk_id 为空时跳过去重逻辑，始终视为未见过。

        Returns:
            list of (result, is_already_seen) tuples
        """
        marked: list[tuple[RetrievalResult, bool]] = []
        for r in results:
            # chunk_id 为空时跳过去重逻辑
            if not r.chunk_id:
                marked.append((r, False))
                continue
            # state 为 None（无状态调用/测试）时不做跨调用去重，一律视为未见过。
            if self._state is None:
                marked.append((r, False))
                continue
            already_seen = r.chunk_id in self._state.seen_chunk_ids
            # 无论是否 seen，都记录到 seen_chunk_ids
            self._state.seen_chunk_ids.add(r.chunk_id)
            marked.append((r, already_seen))
        return marked

    def _format_xml_output(
        self, results_with_status: list[tuple[RetrievalResult, bool]]
    ) -> str:
        """生成 XML 格式化输出

        格式：
        <search_results count="N">
          <chunk rank="1" chunk_id="..." doc_id="..." score="0.85">
            <content>...</content>
          </chunk>
          <chunk rank="2" chunk_id="..." doc_id="..." score="0.80" status="already_retrieved">
            <content>(content omitted, already returned)</content>
          </chunk>
        </search_results>
        """
        lines: list[str] = []
        count = len(results_with_status)
        lines.append(f'<search_results count="{count}">')

        for rank, (result, is_seen) in enumerate(results_with_status, start=1):
            if is_seen:
                lines.append(
                    f'<chunk rank="{rank}" chunk_id="{xml_escape(result.chunk_id)}" '
                    f'doc_id="{xml_escape(result.doc_id)}" '
                    f'score="{result.score:.2f}" status="already_retrieved">'
                )
                lines.append("<content>(content omitted, already returned)</content>")
            else:
                lines.append(
                    f'<chunk rank="{rank}" chunk_id="{xml_escape(result.chunk_id)}" '
                    f'doc_id="{xml_escape(result.doc_id)}" '
                    f'score="{result.score:.2f}">'
                )
                # 优先使用 child_content（精准命中部分），fallback 到 content（父块完整内容）
                content = result.child_content if result.child_content else result.content
                lines.append(f"<content>{xml_escape(content)}</content>")
            lines.append("</chunk>")

        lines.append("</search_results>")
        return "\n".join(lines)
