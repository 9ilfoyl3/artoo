"""A/B benchmark for generic knowledge-base inventory and collection coverage.

The baseline models the existing search-only discovery path. The optimized path
uses the document catalog and map-reduce document summarization.

Run against a local database:

    JWT_SECRET=test-secret PYTHONPATH=backend ./.venv/bin/python \
      -m benchmarks.generic_kb_agent_ab --kb-id <kb_id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from sqlalchemy import func, select

from app.agent.state import AgentState
from app.agent.tools.list_documents import ListKnowledgeDocumentsTool
from app.agent.tools.summarize_documents import SummarizeDocumentsTool
from app.retrieval.bm25 import BM25Retriever
from app.schema.db import Chunk, Document, KnowledgeBase
from app.storage.database import async_session
from app.storage.milvus import get_milvus_client

BROAD_QUERY_GROUPS = [
    ["知识库文档的核心主题与要点", "各文件的主要内容概述", "文档的关键结论与建议"],
    ["这份文档讲了什么", "概述 摘要 结论", "背景 目的 内容"],
    ["合同", "证据"],
    ["律师函", "法定代表人", "送达地址"],
]


class _DeterministicMapLLM:
    """Benchmark-only map step; it measures orchestration, not summary quality."""

    def __init__(self):
        self.calls = 0
        self.output_chars = 0

    async def generate(self, messages, **kwargs):
        self.calls += 1
        prompt = messages[-1]["content"]
        start = prompt.find("<document_content>")
        end = prompt.find("</document_content>")
        content = prompt[start + 18:end].strip() if start >= 0 and end >= 0 else prompt
        summary = f"summary:{content[:200].replace(chr(10), ' ')}"
        self.output_chars += len(summary)
        return summary


class _MemorySummaryCache:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, summary: str) -> None:
        self.values[key] = summary


async def _select_kb(kb_id: str | None) -> str:
    async with async_session() as session:
        if kb_id:
            return kb_id
        row = (
            await session.execute(
                select(KnowledgeBase.id)
                .outerjoin(Document, Document.kb_id == KnowledgeBase.id)
                .group_by(KnowledgeBase.id)
                .having(func.count(Document.id).between(6, 12))
                .order_by(func.count(Document.id).desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if not row:
        raise RuntimeError("No local KB with 6-12 documents was found; pass --kb-id.")
    return row


async def _baseline_search_discovery(kb_id: str) -> dict:
    retriever = BM25Retriever(get_milvus_client())
    started = time.perf_counter()
    query_count = 0
    discovered: set[str] = set()
    for queries in BROAD_QUERY_GROUPS:
        for query in queries:
            query_count += 1
            results = await retriever.search(query=query, kb_id=kb_id, top_k=20)
            discovered.update(result.doc_id for result in results if result.doc_id)
    return {
        "search_calls": len(BROAD_QUERY_GROUPS),
        "queries": query_count,
        "documents_discovered": len(discovered),
        "failed_documents_known": False,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }


async def _optimized_inventory_and_summary(kb_id: str) -> dict:
    started = time.perf_counter()
    state = AgentState()
    catalog = ListKnowledgeDocumentsTool(kb_ids=[kb_id], state=state)

    inventory_started = time.perf_counter()
    inventory = await catalog.execute({"page": 1, "page_size": 100, "status": "completed"})
    inventory_ms = round((time.perf_counter() - inventory_started) * 1000)
    documents = inventory.data["documents"]

    async with async_session() as session:
        failed_count = int(
            (
                await session.scalar(
                    select(func.count(Document.id)).where(
                        Document.kb_id == kb_id,
                        Document.status == "failed",
                    )
                )
            )
            or 0
        )
        total_parent_chars = int(
            (
                await session.scalar(
                    select(func.coalesce(func.sum(func.length(Chunk.content)), 0))
                    .where(
                        Chunk.doc_id.in_(
                            [doc["doc_id"] for doc in documents]
                        ),
                        Chunk.parent_id.is_(None),
                    )
                )
            )
            or 0
        )

    llm = _DeterministicMapLLM()
    cache = _MemorySummaryCache()
    summarizer = SummarizeDocumentsTool(llm=llm, state=state, cache=cache)
    summary_success = True
    first_pass_cache_hits = 0
    for start in range(0, len(documents), 20):
        batch = documents[start:start + 20]
        summary_result = await summarizer.execute({
            "doc_ids": [doc["doc_id"] for doc in batch],
            "instruction": "Summarize core points for coverage benchmarking.",
        })
        summary_success = summary_success and summary_result.success
        first_pass_cache_hits += summary_result.data.get("cache_hits", 0)

    second_pass_cache_hits = 0
    llm_calls_after_first_pass = llm.calls
    for start in range(0, len(documents), 20):
        batch = documents[start:start + 20]
        summary_result = await summarizer.execute({
            "doc_ids": [doc["doc_id"] for doc in batch],
            "instruction": "Summarize core points for coverage benchmarking.",
        })
        second_pass_cache_hits += summary_result.data.get("cache_hits", 0)

    return {
        "inventory_calls": 1,
        "inventory_ms": inventory_ms,
        "documents_in_inventory": inventory.data["total"],
        "completed_documents_listed": len(documents),
        "failed_documents_known": failed_count,
        "documents_summarized": len(state.document_catalog),
        "fully_read_documents": sum(
            1 for doc in state.document_catalog.values() if doc.get("fully_read")
        ),
        "map_summary_calls": llm.calls,
        "llm_calls_before_second_pass": llm_calls_after_first_pass,
        "cache_hits_first_pass": first_pass_cache_hits,
        "cache_hits_second_pass": second_pass_cache_hits,
        "map_calls_per_document": round(
            llm.calls / max(1, len(state.document_catalog)),
            2,
        ),
        "source_parent_chars": total_parent_chars,
        "mapped_summary_chars": llm.output_chars,
        "context_reduction_percent": round(
            (total_parent_chars - llm.output_chars)
            / max(1, total_parent_chars)
            * 100,
            1,
        ),
        "summary_success": summary_success,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }


async def run(kb_id: str | None = None) -> dict:
    selected_kb = await _select_kb(kb_id)
    baseline = await _baseline_search_discovery(selected_kb)
    optimized = await _optimized_inventory_and_summary(selected_kb)
    return {
        "kb_id": selected_kb,
        "baseline_search_only": baseline,
        "optimized_catalog_map_reduce": optimized,
        "coverage_gain": (
            optimized["fully_read_documents"] - baseline["documents_discovered"]
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb-id")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.kb_id)), ensure_ascii=False, indent=2))
