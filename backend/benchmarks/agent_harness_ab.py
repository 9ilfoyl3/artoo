"""Deterministic A/B benchmark for Agent harness P0-P3 policies.

Run from the repository root:

    JWT_SECRET=test-secret PYTHONPATH=backend ./.venv/bin/python -m benchmarks.agent_harness_ab
"""

from __future__ import annotations

import asyncio
import json

from app.agent.harness import RepeatToolTracker, dedupe_queries
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


def _retrieval_agent_loop_metrics() -> dict:
    raw_queries = [
        "contract liability",
        " Contract   Liability ",
        "payment terms",
        "payment terms",
        "termination clause",
        "unused fourth query",
    ]
    optimized = dedupe_queries(raw_queries, limit=3)
    tracker = RepeatToolTracker()
    reminders = sum(
        tracker.observe("echo", {"value": 1}) is not None
        for _ in range(6)
    )
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

    target_count = 2
    baseline_search_units = len(raw_queries) * target_count
    optimized_search_units = len(optimized) * target_count
    return {
        "query_batch": {
            "baseline_queries": len(raw_queries),
            "optimized_queries": len(optimized),
            "duplicates_removed": len(raw_queries) - len(optimized),
        },
        "backend_search_units": {
            "targets": target_count,
            "baseline": baseline_search_units,
            "optimized": optimized_search_units,
            "reduction_percent": round(
                (baseline_search_units - optimized_search_units)
                / baseline_search_units
                * 100,
                1,
            ),
        },
        "repeat_guard": {
            "identical_calls": 6,
            "baseline_reminders": 0,
            "optimized_reminders": reminders,
            "first_reminder_at_call": 3,
        },
        "retrieval_cache": {
            "identical_searches": 2,
            "baseline_backend_retrievals": no_cache_retriever.calls,
            "optimized_backend_retrievals": cached_retriever.calls,
            "cache_hit_rate": 0.5,
        },
    }


def _context_governance_metrics() -> dict:
    estimator = TokenEstimator()
    tool_result_count = 6
    tool_result_chars = 12_000
    max_context_tokens = 12_000
    prune_threshold_tokens = max_context_tokens // 2
    messages = [{"role": "system", "content": "system"}]
    for index in range(tool_result_count):
        messages.extend([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"call-{index}",
                    "type": "function",
                    "function": {"name": "knowledge_search", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": f"call-{index}",
                "content": "x" * tool_result_chars,
            },
        ])
    messages.append({
        "role": "user",
        "content": "current query\n\n<response_contract>clock</response_contract>",
    })
    baseline_tokens = estimator.estimate_messages(messages)
    pruned, pruned_messages = prune_context(
        messages,
        max_context_tokens,
        baseline_tokens,
        tail_start_index=len(messages) - 1,
        threshold_ratio=0.5,
        head_chars=4096,
        tail_chars=1024,
    )
    optimized_tokens = estimator.estimate_messages(pruned)
    return {
        "historical_tool_results": tool_result_count,
        "tool_result_chars_each": tool_result_chars,
        "prune_threshold_tokens": prune_threshold_tokens,
        "baseline_context_tokens": baseline_tokens,
        "optimized_context_tokens": optimized_tokens,
        "token_reduction_percent": round(
            (baseline_tokens - optimized_tokens) / baseline_tokens * 100,
            1,
        ),
        "pruned_tool_results": pruned_messages,
        "baseline_summary_triggered": baseline_tokens > prune_threshold_tokens,
        "optimized_summary_triggered": optimized_tokens > prune_threshold_tokens,
        "llm_summaries_avoided": int(
            baseline_tokens > prune_threshold_tokens
            and optimized_tokens <= prune_threshold_tokens
        ),
    }


def run() -> dict:
    return {
        "retrieval_agent_loop": _retrieval_agent_loop_metrics(),
        "context_governance": _context_governance_metrics(),
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, sort_keys=True))
