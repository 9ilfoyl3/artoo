"""Regression coverage for deterministic Agent harness P0-P3 policies."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.agent.config import AgentConfig
from app.agent.engine import AgentEngine
from app.agent.events import EventBus
from app.agent.harness import (
    RepeatToolTracker,
    canonical_tool_signature,
    dedupe_queries,
)
from app.agent.memory.compress import prune_context
from app.agent.memory.token_estimator import TokenEstimator
from app.agent.prompts.progressive_rag import render_system_prompt
from app.agent.tools.base import BaseTool, ToolResult
from app.agent.tools.knowledge_search import KnowledgeSearchTool, SearchTarget
from app.agent.tools.registry import ToolRegistry
from app.agent.state import AgentState
from app.models.provider import LLMToolCall, StreamChunk
from app.retrieval.base import RetrievalResult
from app.retrieval.cache import RetrievalCache


class _EchoTool(BaseTool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "echo"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict) -> ToolResult:
        return ToolResult(success=True, output="observed")


class _RepeatAwareLLM:
    def __init__(self, repeat_limit: int = 3):
        self.calls = 0
        self.repeat_limit = repeat_limit

    async def stream_with_tools(self, messages, tools, **kwargs):
        self.calls += 1
        has_reminder = any(
            isinstance(message.get("content"), str)
            and (
                "repeating the exact same tool call" in message["content"]
                or "Repeated tool call detected" in message["content"]
            )
            for message in messages
        )
        if has_reminder:
            yield StreamChunk(content="finished", finish_reason="stop")
            return
        yield StreamChunk(
            tool_calls=[LLMToolCall(f"call-{self.calls}", "echo", "{}")],
            finish_reason="tool_calls",
        )


class _FakeRetriever:
    def __init__(self, results: list[RetrievalResult] | None = None):
        self.calls = 0
        self.results = results or []

    async def search_with_degraded(self, query, kb_id, top_k=10, expr=None, tenant_id=None):
        self.calls += 1
        return list(self.results), False


class _RecordingLLM:
    def __init__(self):
        self.kwargs: dict = {}

    async def stream_with_tools(self, messages, tools, **kwargs):
        self.kwargs = kwargs
        yield StreamChunk(content="final", finish_reason="stop")


class _MemoryCache:
    def __init__(self):
        self.values: dict[tuple, list[RetrievalResult]] = {}

    @staticmethod
    def _key(kb_id, query, mode, *, scope="", top_k=None, tenant_id=None, config_version=""):
        return kb_id, query, mode, scope, top_k, tenant_id, config_version

    async def get(self, kb_id, query, mode, **kwargs):
        return self.values.get(self._key(kb_id, query, mode, **kwargs))

    async def set(self, kb_id, query, mode, results, **kwargs):
        self.values[self._key(kb_id, query, mode, **kwargs)] = list(results)


def test_query_normalization_and_deduplication() -> None:
    assert dedupe_queries(["  Foo   Bar ", "foo bar", "baz", "", "qux"], limit=3) == [
        "Foo Bar",
        "baz",
        "qux",
    ]
    assert canonical_tool_signature(
        "knowledge_search",
        {"queries": ["b", "a"], "top_k": 5},
    ) == canonical_tool_signature(
        "knowledge_search",
        {"queries": ["a", "b"], "top_k": 5},
    )


def test_agent_system_prompt_can_exclude_volatile_clock() -> None:
    prompt = render_system_prompt(AgentConfig(), include_clock=False)

    assert "Current date:" not in prompt
    assert "Current time:" not in prompt


def test_reasoning_effort_is_only_sent_when_configured() -> None:
    llm = _RecordingLLM()
    engine = AgentEngine(
        AgentConfig(
            system_prompt="stable",
            reasoning_effort="low",
        ),
        llm,
        ToolRegistry(),
        EventBus(),
    )

    asyncio.run(engine.execute("session", "question"))

    assert llm.kwargs["reasoning_effort"] == "low"


def test_repeat_tool_tracker_reminds_at_thresholds() -> None:
    tracker = RepeatToolTracker()

    assert tracker.observe("echo", {"value": 1}) is None
    assert tracker.observe("echo", {"value": 1}) is None
    assert "repeating the exact same tool call" in (
        tracker.observe("echo", {"value": 1}) or ""
    )
    tracker.observe("echo", {"value": 2})
    assert tracker.observe("echo", {"value": 2}) is None


def test_engine_repeat_reminder_breaks_exact_loop() -> None:
    llm = _RepeatAwareLLM()
    registry = ToolRegistry()
    registry.register(_EchoTool())
    engine = AgentEngine(
        AgentConfig(
            system_prompt="stable",
            max_iterations=6,
            repeat_tool_reminder_enabled=True,
        ),
        llm,
        registry,
        EventBus(),
    )

    state = asyncio.run(engine.execute("session", "question"))

    assert state.final_answer == "finished"
    assert llm.calls == 4


def test_context_pruning_only_touches_history_tool_results() -> None:
    huge = "x" * 20_000
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "echo", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": huge},
        {"role": "user", "content": "current query"},
    ]
    estimator = TokenEstimator()

    pruned, count = prune_context(
        messages,
        100,
        current_tokens=estimator.estimate_messages(messages),
        tail_start_index=3,
    )

    assert count == 1
    assert "[... tool result middle pruned ...]" in pruned[2]["content"]
    assert pruned[3]["content"] == "current query"


def test_agent_search_cache_avoids_repeated_retrieval() -> None:
    result = RetrievalResult(
        chunk_id="chunk-1",
        content="cached content",
        score=0.9,
        doc_id="doc-1",
    )
    retriever = _FakeRetriever([result])
    cache = _MemoryCache()
    tool = KnowledgeSearchTool(
        retriever,
        state=None,
        search_targets=[SearchTarget(kb_id="kb-1")],
        cache=cache,
    )

    first = asyncio.run(tool._search_with_cache(SearchTarget("kb-1"), "query", 5))
    second = asyncio.run(tool._search_with_cache(SearchTarget("kb-1"), "query", 5))

    assert retriever.calls == 1
    assert first[2] is False
    assert second[2] is True
    assert second[0][0].chunk_id == "chunk-1"


def test_knowledge_search_deduplicates_and_suggests_one_rewrite() -> None:
    result = RetrievalResult(
        chunk_id="chunk-1",
        content="content",
        score=0.9,
        doc_id="doc-1",
    )
    retriever = _FakeRetriever([result])
    state = AgentState()
    tool = KnowledgeSearchTool(
        retriever,
        state=state,
        search_targets=[SearchTarget("kb-1")],
    )

    with patch(
        "app.agent.tools.knowledge_search.build_tool_files",
        new=AsyncMock(return_value=[]),
    ):
        first = asyncio.run(tool.execute({"queries": ["alpha", " alpha ", "beta"]}))
        second = asyncio.run(tool.execute({"queries": ["alpha"]}))

    assert retriever.calls == 3
    assert first.data is not None and first.data["new_count"] == 1
    assert second.data is not None
    assert second.data["new_count"] == 0
    assert second.data["suggest_rewrite"] is True


def test_retrieval_cache_key_includes_scope_and_top_k() -> None:
    first = RetrievalCache._build_key("kb", "query", "agent", scope="session-a", top_k=5)
    second = RetrievalCache._build_key("kb", "query", "agent", scope="session-b", top_k=5)
    third = RetrievalCache._build_key("kb", "query", "agent", scope="session-a", top_k=10)

    assert len({first, second, third}) == 3


def test_session_scoped_retrieval_is_not_cached() -> None:
    result = RetrievalResult(
        chunk_id="chunk-1",
        content="session content",
        score=0.9,
        doc_id="doc-1",
    )
    retriever = _FakeRetriever([result])
    tool = KnowledgeSearchTool(
        retriever,
        search_targets=[SearchTarget("session-kb", 'session_id == "s-1"')],
        cache=_MemoryCache(),
    )
    target = SearchTarget("session-kb", 'session_id == "s-1"')

    asyncio.run(tool._search_with_cache(target, "query", 5))
    second = asyncio.run(tool._search_with_cache(target, "query", 5))

    assert retriever.calls == 2
    assert second[2] is False
