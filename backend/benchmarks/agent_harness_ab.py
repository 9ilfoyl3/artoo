"""Deterministic A/B benchmark for Agent harness P0-P3 policies.

Run from the repository root:

    JWT_SECRET=test-secret PYTHONPATH=backend ./.venv/bin/python -m benchmarks.agent_harness_ab
"""

from __future__ import annotations

import asyncio
import json

from app.agent.harness import RepeatToolTracker, dedupe_queries
from app.agent.memory.context_manager import prune_tool_output
from app.agent.memory.compress import prune_context
from app.agent.memory.token_estimator import TokenEstimator
from app.agent.tools.knowledge_search import KnowledgeSearchTool, SearchTarget
from app.retrieval.base import RetrievalResult


class _FakeRetriever:
    def __init__(self, result: RetrievalResult):
        self.calls = 0
        self.result = result

    async def search_with_degraded(self, query, kb_id, top_k=10, expr=None, tenant_id=None):
        self.calls += 1
        return [self.result], False


class _MemoryCache:
    def __init__(self):
        self.values = {}

    @staticmethod
    def _key(kb_id, query, mode, *, scope="", top_k=None, tenant_id=None, config_version=""):
        return kb_id, query, mode, scope, top_k, tenant_id, config_version

    async def get(self, kb_id, query, mode, **kwargs):
        return self.values.get(self._key(kb_id, query, mode, **kwargs))

    async def set(self, kb_id, query, mode, results, **kwargs):
        self.values[self._key(kb_id, query, mode, **kwargs)] = list(results)


def _query_dedup_metrics() -> dict:
    raw_queries = [
        "contract liability",
        " Contract   Liability ",
        "payment terms",
        "payment terms",
        "termination clause",
        "unused fourth query",
    ]
    optimized = dedupe_queries(raw_queries, limit=3)
    return {
        "baseline_queries": len(raw_queries),
        "optimized_queries": len(optimized),
        "removed_queries": len(raw_queries) - len(optimized),
    }


def _repeat_guard_metrics() -> dict:
    tracker = RepeatToolTracker()
    reminders = sum(
        tracker.observe("echo", {"value": 1}) is not None
        for _ in range(6)
    )
    return {
        "baseline_reminders": 0,
        "optimized_reminders": reminders,
        "calls_before_first_reminder": 3,
    }


def _pruning_metrics() -> dict:
    huge = "x" * 20_000
    messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "tool_call_id": "call-1", "content": huge},
        {"role": "user", "content": "current query"},
    ]
    estimator = TokenEstimator()
    pruned, count = prune_context(
        messages,
        100,
        estimator.estimate_messages(messages),
        tail_start_index=3,
    )
    bounded = prune_tool_output(huge, 4096, 1024)
    return {
        "baseline_chars": len(huge),
        "optimized_chars": len(bounded),
        "pruned_messages": count,
        "optimized_chars_saved": len(huge) - len(pruned[2]["content"]),
    }


def _cache_metrics() -> dict:
    result = RetrievalResult(
        chunk_id="chunk-1",
        content="cached",
        score=0.9,
        doc_id="doc-1",
    )
    no_cache_retriever = _FakeRetriever(result)
    no_cache_tool = KnowledgeSearchTool(
        no_cache_retriever,
        search_targets=[SearchTarget("kb-1")],
    )
    asyncio.run(no_cache_tool._search_with_cache(SearchTarget("kb-1"), "query", 5))
    asyncio.run(no_cache_tool._search_with_cache(SearchTarget("kb-1"), "query", 5))

    cached_retriever = _FakeRetriever(result)
    cached_tool = KnowledgeSearchTool(
        cached_retriever,
        search_targets=[SearchTarget("kb-1")],
        cache=_MemoryCache(),
    )
    asyncio.run(cached_tool._search_with_cache(SearchTarget("kb-1"), "query", 5))
    asyncio.run(cached_tool._search_with_cache(SearchTarget("kb-1"), "query", 5))

    return {
        "baseline_retrievals": no_cache_retriever.calls,
        "optimized_retrievals": cached_retriever.calls,
        "cache_hits": 1,
    }


def run() -> dict:
    return {
        "query_dedup": _query_dedup_metrics(),
        "repeat_guard": _repeat_guard_metrics(),
        "context_pruning": _pruning_metrics(),
        "retrieval_cache": _cache_metrics(),
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, sort_keys=True))
