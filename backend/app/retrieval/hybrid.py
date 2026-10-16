"""混合检索器

结合三路检索：稠密向量 + 稀疏向量 + BM25 全文检索，
通过 RRF 融合排序，再经 Rerank 精排，最后执行父块扩展以返回完整上下文。

参考主流 RAG 的三路检索架构：
- Dense（语义相似度）：擅长理解意图和语义匹配
- Sparse（BGE-M3 稀疏向量）：擅长 subword 级别的模糊匹配
- BM25（全文检索）：擅长精确关键词匹配（条款编号、人名、案号等）
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.provider import RerankProvider
from app.repositories.tenant_repo import current_tenant_scope
from app.retrieval.base import BaseRetriever, RetrievalResult
from app.retrieval.log_safety import sanitize_for_log
from app.retrieval.textutil import jaccard as _jaccard_word_sets
from app.retrieval.textutil import tokenize as _tokenize
from app.retrieval.config import (
    PlatformConfigStore,
    RetrievalConfig,
    RetrievalConfigStore,
    get_platform_config_store,
    get_retrieval_config_store,
)
from app.schema.db import Chunk, SessionChunk

logger = logging.getLogger(__name__)


def _current_tenant_id() -> str | None:
    """读取当前请求级租户上下文的 tenant_id（无上下文 / 超管 platform 态 → None）。

    检索发生在 API 请求上下文内，由鉴权守卫在入口 ``set_tenant_scope(...)``。
    - tenant / external 态：返回具体 ``tenant_id``，据此读该租户检索配置。
    - platform 态（超管跨租户）：``scope.tenant_id`` 本就是 None。
    - 无上下文（离线评测 / Worker）：``current_tenant_scope()`` 为 None。
    两种 None 经 ``RetrievalConfigStore.get_effective(None)`` 短路为全 Safe_Default（Req 1.9）。
    """
    scope = current_tenant_scope()
    return scope.tenant_id if scope else None


# ============================================================
# Rerank_Filter 常量（B2 软阈值多重兜底，禁止魔法值）
# 对照 design.md Components C3 常量表的 rerank 三层软阈值。
# ============================================================

# 阈值降级触发线：仅当 rerank_threshold 严格大于此值才允许降级（Req 8.1/8.2）。
_DEGRADE_TRIGGER_THRESHOLD = 0.3
# 阈值降级系数：降级阈值 = rerank_threshold × 此系数（Req 8.1）。
_DEGRADE_FACTOR = 0.7
# 阈值降级下限：降级阈值不低于此值（Req 8.1）。
_DEGRADE_FLOOR = 0.3
# top-1 兜底分数下限：降级后仍空时，最高分不低于此值才保底返回 top-1（Req 9.1/9.2）。
_TOP1_FALLBACK_MIN = 0.15


class HybridRetriever(BaseRetriever):
    """混合检索器：稠密 + 稀疏 + BM25 三路融合 + Rerank + 父块扩展"""

    def __init__(
        self,
        vector_retriever: BaseRetriever,
        sparse_retriever: BaseRetriever,
        rerank_provider: RerankProvider,
        db_session_factory: async_sessionmaker[AsyncSession],
        bm25_retriever: BaseRetriever | None = None,
        config_store: RetrievalConfigStore | None = None,
        platform_store: PlatformConfigStore | None = None,
        graph_retriever: BaseRetriever | None = None,
    ):
        self.vector_retriever = vector_retriever
        self.sparse_retriever = sparse_retriever
        self.bm25_retriever = bm25_retriever
        # 第四路：图谱召回（可选注入，design.md 4.5）。默认 None —— 未注入时 tasks 列表与
        # RRF 行为与未引入本功能时完全一致（Property 8，零回归）。仅当全局开关开启且图存储
        # 可用时由注入方（chat._build_hybrid_retriever）构造并传入 GraphRetriever。
        self.graph_retriever = graph_retriever
        self.reranker = rerank_provider
        self.db_session_factory = db_session_factory
        # 延迟到实例化时取进程内单例，避免 import 期副作用。
        # 检索参数（召回/融合/打分/去重）每次检索从此读取一次有效配置快照。
        self._config_store = config_store or get_retrieval_config_store()
        # 平台级配置（Load_Cache_TTL），单次检索取一次 TTL 快照透传给 Milvus 各搜索方法。
        self._platform_store = platform_store or get_platform_config_store()
        # 最近一次 search() 是否发生三路路级降级（H2）——**非权威、仅调试/回归信号**。
        # 任务 3 已把 degraded 改为经返回结构承载（见 search_with_degraded）：并发请求共享
        # 同一 HybridRetriever 实例时，该实例标志会被并发调用互相覆盖（串扰），故生产消费方
        # （chat._retrieve_chunks）一律读 search_with_degraded 的返回值，**不得**读此标志。
        # 此处保留赋值仅为兼容既有单测对单线程下"最近一次降级"的断言。
        self._last_route_degraded = False

    async def search(
        self, query: str, kb_id: str, top_k: int = 10, expr: str | None = None,
        tenant_id: str | None = None, **kwargs
    ) -> list[RetrievalResult]:
        """执行混合检索，仅返回结果列表（向后兼容入口）。

        degraded（本次三路是否路级降级）经返回结构承载以规避并发隐患——见
        ``search_with_degraded``。需要 degraded 的调用方（chat._retrieve_chunks）改调
        ``search_with_degraded``；其余既有调用点（agent grep/单库 direct/mcp 等）继续用
        本方法,行为不变。

        Args:
            expr: Milvus pre-filter 表达式，传递给子检索器进行元数据过滤
            tenant_id: 显式租户 ID（H5）。语义同 ``search_with_degraded``。
            skip_rerank: 跳过 rerank 和父块扩展，仅返回 RRF 融合结果
        """
        results, _degraded = await self.search_with_degraded(
            query, kb_id, top_k=top_k, expr=expr, tenant_id=tenant_id, **kwargs
        )
        return results

    async def search_with_degraded(
        self, query: str, kb_id: str, top_k: int = 10, expr: str | None = None,
        tenant_id: str | None = None, routes: set[str] | None = None, **kwargs
    ) -> tuple[list[RetrievalResult], bool]:
        """执行混合检索，返回 ``(结果列表, 本次是否路级降级)``。

        流程：并行三路检索 → RRF 融合 → Rerank 精排 → 父块扩展

        三路检索（参考主流 RAG），每路取 config.recall_k 条候选：
        - Dense：语义相似度
        - Sparse：BGE-M3 稀疏向量
        - BM25：全文检索（精确关键词匹配）

        Rerank 候选池取 config.rerank_candidate_k 条（reranker 处理较少候选时性能最优）。
        最终返回 top_k 条精选结果。

        **degraded 经返回结构承载（H3，规避并发隐患）**：``route_degraded`` 是本次调用的
        局部变量，随返回值一并向上传递。并发请求各自持有独立的局部 ``route_degraded``，
        互不串扰；不依赖 ``self._last_route_degraded`` 实例标志（该标志会被并发调用覆盖）。

        Args:
            expr: Milvus pre-filter 表达式，传递给子检索器进行元数据过滤
            tenant_id: 显式租户 ID（H5）。传入非 None 时据此读该租户检索配置；
                未传（None）时回退 ``_current_tenant_id()`` contextvar，保证既有调用点行为不变。
                流式响应中 contextvar 已被依赖 reset，必须由 chat 端点显式下传以免静默降级。
            routes: Optional subset of retrieval routes. ``None`` preserves the
                historical dense+sparse+bm25(+graph) behavior. The unified
                ``search_knowledge`` tool uses this to expose semantic and
                keyword modes without duplicating retriever implementations.
            skip_rerank: 跳过 rerank 和父块扩展，仅返回 RRF 融合结果

        Returns:
            ``(结果列表, degraded)``。degraded=True 表示三路中至少一路异常被降级为空
            （其余路照常融合返回）；三路全失败则抛 RuntimeError 不返回。
        """
        skip_rerank = kwargs.pop("skip_rerank", False)
        # 单次检索取一次有效配置快照，整条链路（召回→融合→rerank→MMR）复用同一份，
        # 保证单次检索参数一致性（Req 5.4）。租户优先级：显式 tenant_id > contextvar 回退（Req 1.3）。
        effective_tenant = tenant_id if tenant_id is not None else _current_tenant_id()
        config = await self._config_store.get_effective(effective_tenant)
        # 单次检索取一次 TTL 快照，透传给三路 Milvus 搜索（Req 15.1/17.3）。
        ttl = await self._platform_store.get_load_cache_ttl()
        # 每路召回数取自配置（默认 128），确保候选池足够大
        recall_k = config.recall_k
        selected_routes = set(routes or {"dense", "sparse", "bm25", "graph"})

        # 1. 并行执行三路检索
        tasks = []
        route_names: list[str] = []
        if "dense" in selected_routes:
            tasks.append(
                self.vector_retriever.search(
                    query,
                    kb_id,
                    top_k=recall_k,
                    expr=expr,
                    ef=config.hnsw_ef,
                    load_cache_ttl=ttl,
                    **kwargs,
                )
            )
            route_names.append("dense")
        if "sparse" in selected_routes:
            tasks.append(
                self.sparse_retriever.search(
                    query,
                    kb_id,
                    top_k=recall_k,
                    expr=expr,
                    load_cache_ttl=ttl,
                    **kwargs,
                )
            )
            route_names.append("sparse")
        # BM25 是可选的（兼容旧 schema collection）
        has_bm25 = self.bm25_retriever is not None and "bm25" in selected_routes
        if has_bm25:
            tasks.append(
                self.bm25_retriever.search(
                    query,
                    kb_id,
                    top_k=recall_k,
                    expr=expr,
                    load_cache_ttl=ttl,
                    **kwargs,
                )
            )
            route_names.append("bm25")

        # 第四路：图谱召回（可选，design.md 4.5）。仅当注入了 graph_retriever 时追加该路；
        # 未注入（None）→ tasks 与下方 RRF 输入与未引入本功能时逐字节一致（Property 8，零回归）。
        # KB 未开启图谱 / store 不可用时由 GraphRetriever.search 自身早退返回 []（自降级），
        # 空列表并入 RRF 不改变其余路名次（标准 RRF 仅用名次累加，空路无贡献）。
        # 图查询异常 → 经下方 _safe 路级降级为空，其余路不受影响（Req 7.3）。
        has_graph = self.graph_retriever is not None and "graph" in selected_routes
        graph_idx = -1
        if has_graph:
            graph_idx = len(tasks)
            tasks.append(self.graph_retriever.search(query, kb_id, top_k=recall_k, expr=expr, **kwargs))
            route_names.append("graph")

        # 三路容错（H2）：return_exceptions=True 收集各路结果 / 异常，逐路经 _safe 包装。
        # 任一路抛异常 → 该路降级为空、其余路照常融合，与 search_with_trace() 行为一致
        # （把调参链路已验证的 _safe 容错下沉到生产 search()）。
        # 参考 open-webui query_collection 的 (result, err) partial-result 范式。
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        # 本次检索是否发生路级降级（任一路抛异常被当空）。供任务 3 向上透传至 SSE meta。
        route_degraded = False

        def _safe(idx: int, route: str) -> list[RetrievalResult]:
            """取第 idx 路结果；该路抛异常则记 WARNING、降级为空并置 route_degraded=True。

            索引 idx 严格对齐上方 tasks 构建顺序：dense=0 / sparse=1 / bm25=2
            （bm25 仅 has_bm25 时存在于 tasks 与 results_list）。
            route 路名为固定字面量（非用户输入）；异常文本经 CR/LF/Tab 替换脱敏后再入日志，
            防日志注入（脱敏统一走 app.retrieval.log_safety.sanitize_for_log）。
            """
            nonlocal route_degraded
            if idx >= len(results_list):
                return []
            r = results_list[idx]
            if isinstance(r, Exception):
                safe_err = sanitize_for_log(r)
                logger.warning("[Retrieval] 第 %d 路(%s)检索异常，降级为空: %s", idx, route, safe_err)
                route_degraded = True
                return []
            return r

        # 索引映射严格对齐 tasks 构建顺序：dense=0 / sparse=1 / bm25=2 / graph=graph_idx。
        def _route_items(name: str) -> list[RetrievalResult]:
            if name not in route_names:
                return []
            return _safe(route_names.index(name), name)

        dense_results = _route_items("dense")
        sparse_results = _route_items("sparse")
        bm25_results = _route_items("bm25")
        # 图谱召回（第四路）：未注入时为空，不参与融合（Property 8 零回归）。
        graph_results = _route_items("graph")

        # 三路全失败（确有降级且 dense/sparse/bm25 融合输入全空）→ 抛 RuntimeError 交上层降级，
        # 区别于"正常无结果"返回空：后者 route_degraded=False 不抛错（Req H2-4）。
        # 注：图谱路属增强项，不纳入"全失败"判定——它本就可能因 KB 未启用/无命中而正常为空，
        # 不应让一条空的图谱路阻止"三主路全失败应抛错"的既有语义（保持现状行为）。
        if route_degraded and not dense_results and not sparse_results and not bm25_results:
            raise RuntimeError("检索三路全部失败，无可用结果")

        # 实例标志仅作单线程下"最近一次降级"的兼容信号（既有单测断言用）；**权威 degraded
        # 经返回结构向上传**（见下方各 return 的元组第二位），并发安全。生产消费方读返回值。
        self._last_route_degraded = route_degraded

        print(f"[Retrieval] 稠密检索: {len(dense_results)} 条, "
              f"稀疏检索: {len(sparse_results)} 条, "
              f"BM25 检索: {len(bm25_results)} 条, "
              f"图谱检索: {len(graph_results)} 条")

        # 2. RRF 融合多路结果
        all_results = [dense_results, sparse_results]
        if bm25_results:
            all_results.append(bm25_results)
        # 图谱路仅在非空时并入融合：空列表对 RRF 名次无贡献，显式不追加可保证未命中/未启用
        # 场景下融合输入与未引入本功能时完全一致（Property 8）。
        if graph_results:
            all_results.append(graph_results)

        fused = self._rrf_fusion(all_results, k=config.rrf_k)
        print(f"[Retrieval] RRF 融合后: {len(fused)} 条")

        if not fused:
            return [], route_degraded

        # 快速模式：跳过 rerank，直接返回 RRF 融合结果
        if skip_rerank:
            return fused, route_degraded

        # 3. Rerank 精排（取 top-rerank_candidate_k 送入 rerank，平衡精度和性能）
        rerank_candidates = fused[:config.rerank_candidate_k]
        try:
            reranked = await self._rerank(query, rerank_candidates, top_k, config)
            print(f"[Retrieval] Rerank 后: {len(reranked)} 条")
        except Exception as e:
            logger.warning("Reranker 异常，跳过重排序: %s", e)
            reranked = fused[:top_k]

        # 4. Composite Scoring：综合 rerank 分数、RRF 基础分数和位置先验
        reranked = self._apply_composite_scoring(reranked, config)

        # 5. MMR 去冗余：去除高度重复的 chunk，确保结果多样性
        reranked = self._apply_mmr(reranked, config.mmr_lambda, config.mmr_threshold)
        print(f"[Retrieval] MMR 去冗余后: {len(reranked)} 条")

        # 6. 父块扩展
        expanded = await self._expand_parent(reranked)

        logger.debug("HybridRetriever 在 kb=%s 中检索到 %d 条结果", kb_id, len(expanded))
        return expanded, route_degraded

    async def search_with_trace(
        self, query: str, kb_id: str, top_k: int = 10, expr: str | None = None,
        tenant_id: str | None = None, apply_rerank_filter: bool = True, **kwargs
    ) -> tuple[list[RetrievalResult], dict]:
        """带链路追踪的混合检索，供检索测试页展示各阶段中间信号

        复用与生产 search() 相同的阶段方法（_rrf_fusion / _rerank /
        _apply_composite_scoring / _apply_mmr / _expand_parent），仅在阶段之间
        捕获中间结果，因此调参看到的链路与线上实际行为一致。

        Args:
            tenant_id: 显式租户 ID（H5）。传入非 None 时据此读该租户检索配置；
                未传（None）时回退 ``_current_tenant_id()`` contextvar（向后兼容调参页等既有调用点）。

        Returns:
            (最终结果列表, trace dict)
            trace dict 结构：
            {
              "routes": [{"name": "dense", "recalled": N}, ...],
              "funnel": [{"stage": "RRF 融合", "count": N}, ...],
              "per_result": {chunk_id: {"routes": [...], "route_ranks": {...},
                                        "rrf_score": f, "rerank_score": f}},
            }
        """
        # 单次检索取一次有效配置快照，保证评测链路与线上 search() 行为一致（Req 5.4）。
        # 租户优先级：显式 tenant_id > contextvar 回退（Req 1.3）。
        effective_tenant = tenant_id if tenant_id is not None else _current_tenant_id()
        config = await self._config_store.get_effective(effective_tenant)
        # 单次检索取一次 TTL 快照，透传给三路 Milvus 搜索（Req 15.1/17.3）。
        ttl = await self._platform_store.get_load_cache_ttl()
        recall_k = config.recall_k

        # 1. 并行三路召回
        tasks = [
            self.vector_retriever.search(query, kb_id, top_k=recall_k, expr=expr, ef=config.hnsw_ef, load_cache_ttl=ttl, **kwargs),
            self.sparse_retriever.search(query, kb_id, top_k=recall_k, expr=expr, load_cache_ttl=ttl, **kwargs),
        ]
        has_bm25 = self.bm25_retriever is not None
        if has_bm25:
            tasks.append(self.bm25_retriever.search(query, kb_id, top_k=recall_k, expr=expr, load_cache_ttl=ttl, **kwargs))

        # 第四路：图谱召回（可选）。与 search_with_degraded 同构——仅当注入了 graph_retriever
        # 时追加；未注入（None）→ tasks/RRF 输入与三路时逐字节一致（Property 8 零回归）。
        # 这样调参/召回接口在图谱开启时也能看到并用上第四路，与生产问答链路同口径。
        has_graph = self.graph_retriever is not None
        graph_idx = len(tasks) if has_graph else -1
        if has_graph:
            tasks.append(self.graph_retriever.search(query, kb_id, top_k=recall_k, expr=expr, **kwargs))

        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        def _safe(idx: int) -> list[RetrievalResult]:
            if idx < 0 or idx >= len(results_list):
                return []
            r = results_list[idx]
            if isinstance(r, Exception):
                logger.warning("[Trace] 第 %d 路检索异常: %s", idx, sanitize_for_log(r))
                return []
            return r

        dense_results = _safe(0)
        sparse_results = _safe(1)
        bm25_results = _safe(2) if has_bm25 else []
        graph_results = _safe(graph_idx) if has_graph else []

        # 路由归属：chunk_id -> {route: rank}
        per_result: dict[str, dict] = {}

        def _record_route(items: list[RetrievalResult], route: str) -> None:
            for rank, item in enumerate(items):
                entry = per_result.setdefault(
                    item.chunk_id, {"routes": [], "route_ranks": {}, "rrf_score": None, "rerank_score": None}
                )
                if route not in entry["routes"]:
                    entry["routes"].append(route)
                entry["route_ranks"][route] = rank

        _record_route(dense_results, "dense")
        _record_route(sparse_results, "sparse")
        _record_route(bm25_results, "bm25")
        _record_route(graph_results, "graph")

        # 2. RRF 融合
        all_results = [dense_results, sparse_results]
        if bm25_results:
            all_results.append(bm25_results)
        # 图谱路仅在非空时并入融合：空列表对 RRF 名次无贡献，与 search_with_degraded 一致。
        if graph_results:
            all_results.append(graph_results)
        fused = self._rrf_fusion(all_results, k=config.rrf_k)

        for item in fused:
            entry = per_result.get(item.chunk_id)
            if entry is not None:
                entry["rrf_score"] = round(item.metadata.get("_rrf_score", 0.0), 6)

        routes = [
            {"name": "dense", "recalled": len(dense_results)},
            {"name": "sparse", "recalled": len(sparse_results)},
            {"name": "bm25", "recalled": len(bm25_results), "enabled": has_bm25},
            {"name": "graph", "recalled": len(graph_results), "enabled": has_graph},
        ]
        funnel: list[dict] = [
            {"stage": "三路召回去重", "count": len(per_result)},
            {"stage": "RRF 融合", "count": len(fused)},
        ]

        if not fused:
            return [], {"routes": routes, "funnel": funnel, "per_result": per_result}

        # 3. Rerank 精排
        rerank_candidates = fused[:config.rerank_candidate_k]
        funnel.append({"stage": "Rerank 候选", "count": len(rerank_candidates)})
        try:
            reranked = await self._rerank(
                query, rerank_candidates, top_k, config, apply_filter=apply_rerank_filter
            )
        except Exception as e:
            logger.warning("[Trace] Reranker 异常，跳过重排序: %s", e)
            reranked = fused[:top_k]
        funnel.append({"stage": "Rerank 输出", "count": len(reranked)})

        # 捕获 rerank 分数（composite 之前）
        for item in reranked:
            entry = per_result.get(item.chunk_id)
            if entry is not None:
                entry["rerank_score"] = round(item.score, 6)

        # 4. Composite 评分
        composited = self._apply_composite_scoring(reranked, config)

        # 5. MMR 去冗余
        after_mmr = self._apply_mmr(composited, config.mmr_lambda, config.mmr_threshold)
        removed = [c.chunk_id for c in composited if c.chunk_id not in {m.chunk_id for m in after_mmr}]
        funnel.append({"stage": "MMR 去冗余", "count": len(after_mmr)})

        # 6. 父块扩展
        expanded = await self._expand_parent(after_mmr)

        trace = {
            "routes": routes,
            "funnel": funnel,
            "per_result": per_result,
            "mmr_removed": removed,
        }
        return expanded, trace

    async def rerank_and_expand(
        self, query: str, results: list[RetrievalResult], top_k: int = 10,
        tenant_id: str | None = None, apply_rerank_filter: bool = True
    ) -> list[RetrievalResult]:
        """对已合并的结果执行 rerank 精排 + 父块扩展

        供 executor 在批量合并子查询结果后统一调用，避免多次 rerank 锁争用。

        多库联合路径同样需要应用 rerank 软阈值过滤（B2），故在此取一次有效配置快照
        传入 ``_rerank``，与单库 search / trace 三路统一。

        Args:
            tenant_id: 显式租户 ID（H5）。多库路径的 rerank 阶段同样读 config，
                必须由 ``MultiKBRetriever.search`` 显式下传，否则 rerank 阶段丢租户配置；
                未传（None）时回退 ``_current_tenant_id()`` contextvar（向后兼容）。
        """
        if not results:
            return []

        # 多库路径取一次有效配置快照，使阈值过滤在多库联合检索上同样生效（Req 5.4）。
        # 租户优先级：显式 tenant_id > contextvar 回退（Req 1.3）。多库路径只 rerank 不直接召回，不涉及 TTL。
        effective_tenant = tenant_id if tenant_id is not None else _current_tenant_id()
        config = await self._config_store.get_effective(effective_tenant)

        # 候选池取 config.rerank_candidate_k（默认 50），与单库 search 路径对齐。
        # 此前用 top_k*2（20）过小：多库 + 会话文件合并后候选可达上百条，只送 20 条进
        # reranker 会把真正相关但 RRF 排名靠后的 chunk（如另一来源的精确条文）截断在
        # rerank 之前，导致"选了知识库却答不出其内容"。reranker 本就擅长从较大候选池精选。
        rerank_candidates = results[: config.rerank_candidate_k]

        try:
            reranked = await self._rerank(
                query, rerank_candidates, top_k, config, apply_filter=apply_rerank_filter
            )
            print(f"[Retrieval] 统一 Rerank 后: {len(reranked)} 条")
        except Exception as e:
            logger.warning("Reranker 异常，跳过重排序: %s", e)
            reranked = results[:top_k]

        expanded = await self._expand_parent(reranked)
        return expanded

    def _rrf_fusion(
        self,
        results_lists: list[list[RetrievalResult]],
        k: int = 60,
        type_weights: dict[str, float] | None = None,
    ) -> list[RetrievalResult]:
        """Reciprocal Rank Fusion 融合多路检索结果，支持按元素类型施加权重

        Args:
            results_lists: 多路检索结果列表
            k: RRF 参数，默认 60
            type_weights: 元素类型权重映射，默认对 table 类型施加 0.8 降权
                         注意：CSV 文件来源的 table 类型不降权（它们的 table 标记是格式转换导致的，
                         不代表内容是辅助性表格）
        """
        if type_weights is None:
            type_weights = {"table": 0.8}

        scores: dict[str, float] = {}
        items: dict[str, RetrievalResult] = {}

        for results in results_lists:
            for rank, item in enumerate(results):
                rrf_score = 1.0 / (k + rank + 1)
                scores[item.chunk_id] = scores.get(item.chunk_id, 0) + rrf_score
                items[item.chunk_id] = item

        # 施加类型权重（CSV 文件来源的 chunk 跳过降权）
        for chunk_id, item in items.items():
            element_type = item.metadata.get("element_type", "text")
            file_type = item.metadata.get("file_type", "")
            # CSV 文件的 table 标记是格式转换导致的，不应降权
            if file_type == "csv":
                continue
            weight = type_weights.get(element_type, 1.0)
            scores[chunk_id] *= weight

        # 按分数降序排列，将 RRF 分数写入 metadata 供后续 composite scoring 使用
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        fused_results = []
        for cid in sorted_ids:
            item = items[cid]
            item.metadata["_rrf_score"] = scores[cid]
            fused_results.append(item)
        return fused_results

    @staticmethod
    def _composite_score(
        rerank_score: float,
        base_score: float,
        source_weight: float = 1.0,
        w_rerank: float = 0.6,
        w_base: float = 0.3,
        w_source: float = 0.1,
    ) -> float:
        """综合评分：融合 rerank 分数、原始检索分数和位置先验

        公式: composite = w_rerank * rerank_score + w_base * base_score + w_source * source_weight
        结果 clamp 到 [0.0, 1.0]

        权重默认 0.6 / 0.3 / 0.1（兼容历史行为）；生产链路由调用方从
        config.composite_rerank_weight / base_weight / source_weight 透传。

        Args:
            rerank_score: reranker 输出分数
            base_score: 原始检索分数（RRF 融合分数）
            source_weight: 位置先验权重，默认 1.0
            w_rerank: rerank 分数权重，默认 0.6
            w_base: 原始检索分数权重，默认 0.3
            w_source: 位置先验权重的权重，默认 0.1
        """
        composite = w_rerank * rerank_score + w_base * base_score + w_source * source_weight
        return max(0.0, min(1.0, composite))

    @staticmethod
    def _jaccard_tokens(text_a: str, text_b: str) -> float:
        """两段文本的词级 Jaccard 相似度

        先用词级分词器切词（中文 jieba 搜索模式，不可用时退化字符 bigram；
        纯非中文按空白切分，统一过滤单字 token 与纯标点），再算 token 集合的
        Jaccard。相比字符级分词，词级能更好反映语义单元、抑制单字噪声，并能
        区分仅语序不同的文本。

        Returns:
            Jaccard 相似度 [0.0, 1.0]，两个空集返回 0.0
        """
        return _jaccard_word_sets(_tokenize(text_a), _tokenize(text_b))

    @staticmethod
    def _apply_mmr(
        results: list[RetrievalResult],
        lambda_param: float = 0.7,
        threshold: float = 0.7,
    ) -> list[RetrievalResult]:
        """Maximal Marginal Relevance 去冗余（标准迭代式 MMR）

        每轮从未选候选中挑选 MMR 分数最高者，迭代直至选满或无候选：

            mmr(d) = λ · relevance(d) − (1 − λ) · max_{s ∈ selected} sim(d, s)

        - relevance：候选的 composite score（``result.score``，调用前已按其降序）。
        - sim：候选与「已选集合」中各结果的词级 Jaccard，取最大值作为冗余度。
        - λ 越大越偏相关性、越小越偏多样性（默认 0.7，平衡值）。

        相比旧实现「相似度超阈值即跳过」，标准 MMR 在相关性与多样性之间做加权
        权衡，会把「稍弱相关但带来新信息」的结果适当提前，而非简单丢弃近似项；
        因此本方法对所有候选重排序并全部保留（去冗余通过排序体现，配合上游
        rerank_top_k 截断），不再因阈值丢结果。

        Token 集合按 chunk 预计算一次并缓存，避免 O(k²) 重复分词。

        Args:
            results: 按 composite score 降序的检索结果
            lambda_param: MMR λ 参数，平衡相关性与多样性（默认 0.7）
            threshold: 保留参数仅为向后兼容签名，标准 MMR 不使用阈值；
                当前实现忽略此参数。

        Returns:
            按 MMR 顺序重排后的结果列表（长度与输入一致）
        """
        if not results:
            return results
        if len(results) == 1:
            return list(results)

        # 预计算每条候选的词级 token 集合（按 content），避免选择循环内重复分词。
        token_sets = [_tokenize(r.content) for r in results]

        n = len(results)
        selected_idx: list[int] = []
        remaining: set[int] = set(range(n))

        while remaining:
            best_i = -1
            best_mmr = float("-inf")
            for i in remaining:
                relevance = results[i].score
                # 冗余度 = 与已选集合中各结果的最大 Jaccard（无已选时为 0）。
                redundancy = 0.0
                for s in selected_idx:
                    sim = _jaccard_word_sets(token_sets[i], token_sets[s])
                    if sim > redundancy:
                        redundancy = sim
                mmr = lambda_param * relevance - (1.0 - lambda_param) * redundancy
                if mmr > best_mmr:
                    best_mmr = mmr
                    best_i = i
            selected_idx.append(best_i)
            remaining.discard(best_i)

        return [results[i] for i in selected_idx]

    def _apply_composite_scoring(
        self, results: list[RetrievalResult], config=None
    ) -> list[RetrievalResult]:
        """对 rerank 后的结果应用综合评分，按 composite score 重新排序

        source_weight 基于结果在原始检索列表中的位置：
        第 1 名 = 1.0，逐步递减，最低 0.1

        Args:
            results: rerank 后的结果列表
            config: 本次检索的 RetrievalConfig 快照；提供时用其 composite_*_weight
                替代默认 0.6/0.3/0.1。为 None 时退回默认权重（兼容旧调用点 / 单测）。
        """
        if not results:
            return results

        # 从配置取 composite 权重，缺省退回历史默认值
        if config is not None:
            w_rerank = config.composite_rerank_weight
            w_base = config.composite_base_weight
            w_source = config.composite_source_weight
        else:
            w_rerank, w_base, w_source = 0.6, 0.3, 0.1

        scored = []
        total = len(results)
        for i, r in enumerate(results):
            rerank_score = r.score
            base_score = r.metadata.get("_rrf_score", 0.0)
            # 位置先验：排名越靠前权重越高，线性递减，最低 0.1
            source_weight = max(0.1, 1.0 - (i / total)) if total > 1 else 1.0
            composite = self._composite_score(
                rerank_score, base_score, source_weight, w_rerank, w_base, w_source
            )
            scored.append(
                RetrievalResult(
                    chunk_id=r.chunk_id,
                    content=r.content,
                    score=composite,
                    doc_id=r.doc_id,
                    metadata=r.metadata,
                    child_content=r.child_content,
                )
            )

        scored.sort(key=lambda x: x.score, reverse=True)
        return scored

    def _apply_rerank_filter(
        self, reranked: list[RetrievalResult], config: RetrievalConfig
    ) -> list[RetrievalResult]:
        """rerank 软阈值过滤 + 多重兜底（B2，对照 design.md rerank 三层）。

        作用在 **rerank 原始分数** 上（输入 ``reranked`` 的 ``score`` 必须是 rerank 原始分，
        即在 ``_apply_composite_scoring`` 改写 score 之前调用）。输出保持降序、score 不变。

        三层逻辑（对照 design.md Components C3 伪代码）：

        1. 软阈值过滤（Req 7.1/7.4）：保留 ``score >= rerank_threshold`` 的结果。
           ``rerank_threshold == 0.0`` 时全留（等价不过滤）。
        2. 阈值降级（Req 8）：仅当第一层为空、降级开关开启且 ``threshold > 0.3`` 时，
           以 ``max(0.3, threshold * 0.7)`` 为新阈值重过滤一次（一次检索最多降级一次）。
        3. top-1 兜底（Req 9）：仍为空时，若最高分 ``>= 0.15`` 返回该 top-1 单条，否则返回 []。

        不抛异常：空输入与全低分输入均返回 ``[]``（Req 9.3）。

        Args:
            reranked: 已按 rerank 原始分数降序的结果列表。
            config: 本次检索的 ``RetrievalConfig`` 快照（读 ``rerank_threshold`` 与
                ``threshold_degradation_enabled``）。

        Returns:
            过滤/兜底后的结果列表（降序、score 不变）。
        """
        # 空输入直接返回（Req 9.3 不抛异常）。
        if not reranked:
            return []

        threshold = config.rerank_threshold

        # 第 1 层：软阈值过滤（Req 7.1）。
        # threshold == 0.0 时 score(>=0 或任意) >= 0.0 恒真，等价不过滤（Req 7.4）。
        filtered = [r for r in reranked if r.score >= threshold]

        # 第 2 层：阈值降级（Req 8）。仅在第一层为空时尝试，且最多一次。
        if not filtered:
            if config.threshold_degradation_enabled and threshold > _DEGRADE_TRIGGER_THRESHOLD:
                degraded = max(_DEGRADE_FLOOR, threshold * _DEGRADE_FACTOR)
                filtered = [r for r in reranked if r.score >= degraded]

        # 第 3 层：top-1 兜底（Req 9）。降级后仍空时启用。
        if not filtered:
            top1 = reranked[0]  # 已降序，第 0 个即最高分
            if top1.score >= _TOP1_FALLBACK_MIN:
                filtered = [top1]
            else:
                filtered = []

        return filtered

    async def _rerank(
        self,
        query: str,
        results: list[RetrievalResult],
        top_k: int,
        config: RetrievalConfig | None = None,
        apply_filter: bool = True,
    ) -> list[RetrievalResult]:
        """调用 Reranker 对融合结果精排，返回 top_k 结果

        对"结构性碎片"（标题、目录标记等无实质信息的短文本）施加分数惩罚，
        避免它们因关键词匹配获得虚高分数。

        排序后、返回前统一应用 ``_apply_rerank_filter``（软阈值 + 多重兜底，B2），
        作用在 rerank 原始分数上（此时 score 仍是 rerank 原始分，尚未经
        ``_apply_composite_scoring`` 改写）。单库 / 多库 / trace 三路统一在此过滤。

        Args:
            config: 本次检索的 ``RetrievalConfig`` 快照。为 None 时（兼容旧调用点 / 单测）
                用全 Safe_Default 配置，使阈值过滤行为可预期。
            apply_filter: 是否应用软阈值过滤（默认 True，问答链路保持防幻觉过滤）。
                对外「纯检索召回」接口（/retrieval/search、/test）传 False：软阈值是为问答
                避免把低相关内容喂给 LLM 而设，检索召回接口应返回 rerank 排序后的 top_k 由
                调用方按分数自行取舍，不应被问答阈值静默滤空（否则短/泛 query 常被兜底也救不回）。
        """
        if not results:
            return []

        # 阈值过滤需要配置；缺省时退回全默认（rerank_threshold=0.2 等）。
        if config is None:
            config = RetrievalConfig()

        documents = [r.content for r in results]
        ranked_pairs = await self.reranker.rerank(query, documents, top_k=top_k)

        # 打印分数分布用于调试
        if ranked_pairs:
            scores = [score for _, score in ranked_pairs]
            print(f"[Rerank] 分数范围: {min(scores):.3f} ~ {max(scores):.3f}, 返回 top {top_k}")

        # ranked_pairs: list[(原始索引, 分数)]，按分数降序已排好
        reranked = []
        for idx, score in ranked_pairs:
            item = results[idx]
            # 对结构性碎片施加惩罚
            if self._is_structural_fragment(item.content):
                score = score * 0.5
            reranked.append(
                RetrievalResult(
                    chunk_id=item.chunk_id,
                    content=item.content,
                    score=score,
                    doc_id=item.doc_id,
                    metadata=item.metadata,
                )
            )

        # 惩罚后重新排序（rerank 原始分数降序）
        reranked.sort(key=lambda x: x.score, reverse=True)

        # 软阈值过滤 + 多重兜底（B2）：作用在 rerank 原始分数上，返回前统一应用。
        # 纯检索召回接口传 apply_filter=False 跳过此过滤，直接返回 rerank 排序结果。
        if apply_filter:
            reranked = self._apply_rerank_filter(reranked, config)
        return reranked

    @staticmethod
    def _is_structural_fragment(content: str) -> bool:
        """判断内容是否为结构性碎片（标题、目录标记等无实质信息的短文本）

        判定条件：内容短（<15字符）且整体匹配常见的结构标记模式。
        像"2023-3378"、"386489元"这类虽短但有实质信息的内容不会被误判。
        """
        text = content.strip()
        if len(text) >= 15:
            return False
        # 匹配纯标题/角色标记：原告、被告、第三人、证据目录等
        import re
        structural_pattern = re.compile(
            r'^(原告|被告|第三人|诉讼请求|事实与理由|事实和理由|证据目录|证据清单|'
            r'判决如下|裁定如下|本院认为|经审理查明|审判长|审判员|'
            r'目录|附录|附件|备注|注|说明)$'
        )
        return bool(structural_pattern.match(text))

    async def _expand_parent(
        self, results: list[RetrievalResult]
    ) -> list[RetrievalResult]:
        """父块扩展：若 chunk 有 parent_id，用父块内容替换 content，子块内容保留到 child_content"""
        # 空值兜底：无检索结果（未选知识库 / 未上传附件 / 检索为空）直接返回，
        # 不进行任何 DB 查询。
        if not results:
            return results

        # 收集需要扩展的 parent_id
        parent_ids = set()
        for r in results:
            parent_id = r.metadata.get("parent_id", "")
            if parent_id:
                parent_ids.add(parent_id)

        if not parent_ids:
            # 没有父块，child_content 就是 content 本身
            for r in results:
                r.child_content = r.content
            return results

        # 批量查询父块内容。
        # 正式知识库父块存 ``chunks`` 表，会话上传文件父块存 ``session_chunks`` 表
        # （两表 id 均为 Milvus chunk_id，UUID 全局唯一，不会冲突）。父块扩展对两条
        # 来源都要生效——否则会话文件命中查不到父块、回退到子块小片段，LLM 拿不到
        # 完整父块上下文，问答中会话文件被系统性矮化（session-file 父块扩展缺失修复）。
        # 仅收集非空父块内容：父块行存在但内容为空（异常数据）时不写入，
        # 让下方 .get 回退到子块内容，避免给 LLM 空上下文。
        parent_id_list = list(parent_ids)
        parent_contents: dict[str, str] = {}
        async with self.db_session_factory() as session:
            kb_rows = await session.execute(
                select(Chunk.id, Chunk.content).where(Chunk.id.in_(parent_id_list))
            )
            for row in kb_rows:
                if row.content:
                    parent_contents[row.id] = row.content

            # 未在正式库命中的 parent_id，再查会话文件父块表（避免无谓查询）。
            missing_ids = [pid for pid in parent_id_list if pid not in parent_contents]
            if missing_ids:
                session_rows = await session.execute(
                    select(SessionChunk.id, SessionChunk.content).where(
                        SessionChunk.id.in_(missing_ids)
                    )
                )
                for row in session_rows:
                    if row.content:
                        parent_contents[row.id] = row.content

        # 保留子块内容，用父块内容替换 content。
        # 任一环节缺失（无 parent_id / 两表都查不到 / 父块内容为空）均回退到子块内容，
        # 保证 content 永不为空。
        expanded = []
        for r in results:
            parent_id = r.metadata.get("parent_id", "")
            child_content = r.content  # 原始子块内容
            parent_content = parent_contents.get(parent_id) if parent_id else None
            if not parent_content:
                parent_content = child_content
            expanded.append(
                RetrievalResult(
                    chunk_id=r.chunk_id,
                    content=parent_content,
                    score=r.score,
                    doc_id=r.doc_id,
                    metadata=r.metadata,
                    child_content=child_content,
                )
            )
        return expanded
