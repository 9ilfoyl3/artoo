"""Regression tests for the unified retrieval and production Agent core."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.config import AgentConfig
from app.agent.engine import AgentEngine
from app.agent.events import EventBus
from app.agent.json_repair import parse_json_with_repair, parse_tool_arguments
from app.agent.memory.token_estimator import TokenEstimator
from app.agent.retrieval_postprocess import postprocess_retrieval
from app.agent.state import AgentState
from app.agent.tools.base import BaseTool, ToolResult
from app.agent.tools.registry import ToolRegistry
from app.agent.tools.list_documents import ListDocumentsTool
from app.agent.tools.read_document import ReadDocumentTool
from app.agent.tools.search_knowledge import SearchKnowledgeTool, SearchTarget
from app.agent.tools.todo import TodoWriteTool
from app.models.provider import LLMToolCall, StreamChunk
from app.retrieval.base import RetrievalResult
from app.schema.db import (
    AgentCompactionCheckpoint,
    Base,
    Chunk,
    Document,
    DocumentSummary,
    KnowledgeBase,
)
from app.summary.worker import DocumentSummaryWorker


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
        return ToolResult(success=True, output="x" * 50)


class _FakeRetriever:
    async def search_with_degraded(self, **kwargs):
        return [], False


class _TimedReadTool(BaseTool):
    def __init__(self, name: str, events: list[tuple[str, float, float]]):
        self._name = name
        self._events = events

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict) -> ToolResult:
        start = asyncio.get_running_loop().time()
        await asyncio.sleep(0.02)
        end = asyncio.get_running_loop().time()
        self._events.append((self._name, start, end))
        return ToolResult(success=True, output=self._name)


class _TimedWriteTool(_TimedReadTool):
    read_only = False


class _ToolPairLLM:
    def __init__(self):
        self.calls = 0

    async def stream_with_tools(self, messages, tools, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                tool_calls=[
                    LLMToolCall("read-1", "read_a", "{}"),
                    LLMToolCall("write-1", "write_a", "{}"),
                    LLMToolCall("read-2", "read_b", "{}"),
                ],
                finish_reason="tool_calls",
            )
        else:
            yield StreamChunk(content="finished", finish_reason="stop")


class _StallLLM:
    async def stream_with_tools(self, messages, tools, **kwargs):
        await asyncio.sleep(0.05)
        yield StreamChunk(content="late", finish_reason="stop")


def test_json_repair_is_conservative_and_marks_truncation() -> None:
    repaired = parse_tool_arguments('{"query": "annual report", "mode": "hybrid",}')
    truncated = parse_json_with_repair('{"query": "annual report"')

    assert repaired.ok is True
    assert repaired.repaired is True
    assert repaired.truncated is False
    assert repaired.value == {"query": "annual report", "mode": "hybrid"}
    assert truncated.value == {"query": "annual report"}
    assert truncated.truncated is True


def test_tool_registry_enforces_per_tool_output_budget() -> None:
    registry = ToolRegistry(output_budgets={"echo": 40})
    registry.register(_EchoTool())

    result = asyncio.run(registry.execute("echo", {}))

    assert result.success is True
    assert len(result.output) <= 40
    assert "truncated" in result.output


def test_todo_write_validates_and_tracks_plan() -> None:
    state = AgentState()
    tool = TodoWriteTool(state)

    result = asyncio.run(
        tool.execute(
            {
                "todos": [
                    {"content": "Inspect documents", "status": "completed"},
                    {"content": "Synthesize answer", "status": "in_progress"},
                ]
            }
        )
    )
    invalid = asyncio.run(
        tool.execute(
            {
                "todos": [
                    {"content": "A", "status": "in_progress"},
                    {"content": "B", "status": "in_progress"},
                ]
            }
        )
    )

    assert result.success is True
    assert state.todos[0]["content"] == "Inspect documents"
    assert invalid.success is False


def test_retrieval_postprocess_deduplicates_and_audits_cutoff() -> None:
    results = [
        RetrievalResult("c1", "same normalized content", 0.9, "doc-a"),
        RetrievalResult("c2", "same   normalized content", 0.8, "doc-a"),
        RetrievalResult(
            "c3",
            "policy requires original invoice and approval before payment",
            0.7,
            "doc-b",
        ),
        RetrievalResult(
            "c4",
            "policy requires original invoice and approval before the payment",
            0.6,
            "doc-b",
        ),
    ]

    processed = asyncio.run(
        postprocess_retrieval(results, limit=1, expand_neighbors=False)
    )

    assert processed.audit.signature_duplicates_removed == 1
    assert processed.audit.near_duplicates_removed >= 1
    assert processed.audit.cutoff_applied is True
    assert len(processed.results) == 1


def test_token_estimator_calibrates_from_provider_usage() -> None:
    estimator = TokenEstimator()
    messages = [{"role": "user", "content": "A reasonably long retrieval question."}]
    before = estimator.estimate_messages(messages)

    estimator.calibrate_from_usage(
        prompt_tokens=before * 2,
        messages=messages,
        smoothing=1.0,
    )

    assert estimator.estimate_messages(messages) > before


def test_compaction_checkpoint_round_trip(tmp_path, monkeypatch) -> None:
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/checkpoint.db")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        import app.storage.database as database

        monkeypatch.setattr(database, "async_session", factory)
        agent = AgentEngine(
            AgentConfig(system_prompt="stable system"),
            _FakeRetriever(),
            ToolRegistry(),
            EventBus(),
        )
        messages = [
            {"role": "system", "content": "stable system"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
        ]
        await agent._save_checkpoint(
            session_id="session-1",
            system_prompt="stable system",
            messages=messages,
        )
        loaded = await agent._load_checkpoint_history(
            "session-1",
            "stable system",
            [{"role": "user", "content": "old question"}, {"role": "assistant", "content": "old answer"}],
        )
        async with factory() as session:
            from sqlalchemy import select

            row = await session.scalar(
                select(AgentCompactionCheckpoint).where(
                    AgentCompactionCheckpoint.session_id == "session-1"
                )
            )
            assert row is not None
            assert row.messages == messages
        await engine.dispose()
        return loaded

    loaded = asyncio.run(run())
    assert loaded == [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]


def test_search_knowledge_summary_mode_uses_durable_profiles() -> None:
    state = AgentState()
    tool = SearchKnowledgeTool(
        _FakeRetriever(),
        search_targets=[SearchTarget("kb-1")],
        state=state,
    )
    profile = {
        "doc_id": "doc-1",
        "kb_id": "kb-1",
        "session_id": None,
        "source": "document",
        "filename": "report.pdf",
        "content_version": "v1",
        "summary": "A concise report summary.",
        "gist": "Report summary.",
        "topics": ["finance"],
        "doc_type": "report",
        "typical_question": "What does the report conclude?",
        "chunk_count": 4,
        "score": 0.9,
    }

    with patch(
        "app.agent.tools.search_knowledge.search_summary_artifacts",
        new=AsyncMock(return_value=[profile]),
    ):
        result = asyncio.run(
            tool.execute({"query": "report conclusion", "mode": "summary"})
        )

    assert result.success is True
    assert result.data["count"] == 1
    assert "<knowledge_summaries" in result.output
    assert "A concise report summary." in result.output


def test_agent_serializes_mutation_between_read_only_groups() -> None:
    events: list[tuple[str, float, float]] = []
    registry = ToolRegistry()
    registry.register(_TimedReadTool("read_a", events))
    registry.register(_TimedWriteTool("write_a", events))
    registry.register(_TimedReadTool("read_b", events))
    engine = AgentEngine(
        AgentConfig(
            system_prompt="stable",
            parallel_tool_calls=True,
            tool_call_max_concurrency=2,
            compaction_checkpoint_enabled=False,
        ),
        _ToolPairLLM(),
        registry,
        EventBus(),
    )

    asyncio.run(engine.execute("session", "do two reads and one write"))

    by_name = {name: (start, end) for name, start, end in events}
    assert by_name["read_a"][0] < by_name["read_a"][1]
    assert by_name["write_a"][0] >= by_name["read_a"][1]
    assert by_name["read_b"][0] >= by_name["write_a"][1]


def test_agent_stream_stall_watchdog_terminates_silent_generation(monkeypatch) -> None:
    import app.agent.engine as engine_module

    monkeypatch.setattr(engine_module, "_MAX_TRANSIENT_RETRIES", 0)
    engine = AgentEngine(
        AgentConfig(
            system_prompt="stable",
            llm_stall_timeout=1,
            compaction_checkpoint_enabled=False,
        ),
        _StallLLM(),
        ToolRegistry(),
        EventBus(),
    )

    # Avoid waiting a full second in the unit suite while still exercising the
    # watchdog path.
    engine._config.llm_stall_timeout = 0.01  # type: ignore[misc]
    state = asyncio.run(engine.execute("session", "question"))

    assert state.is_complete is True
    assert "遇到错误" in state.final_answer


def test_list_documents_filters_and_marks_summary_status(tmp_path, monkeypatch) -> None:
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/list.db")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        import app.agent.tools.list_documents as module

        monkeypatch.setattr(module, "async_session", factory)
        async with factory() as session:
            session.add(KnowledgeBase(id="kb-1", name="KB"))
            session.add_all(
                [
                    Document(
                        id="doc-a",
                        kb_id="kb-1",
                        filename="annual-report.pdf",
                        file_type="pdf",
                        status="completed",
                        chunk_count=2,
                    ),
                    Document(
                        id="doc-b",
                        kb_id="kb-1",
                        filename="notes.txt",
                        file_type="txt",
                        status="completed",
                        chunk_count=1,
                    ),
                ]
            )
            session.add(
                DocumentSummary(
                    id="summary-a",
                    source="document",
                    doc_id="doc-a",
                    kb_id="kb-1",
                    filename="annual-report.pdf",
                    content_version="v1",
                    status="completed",
                    summary="Annual report summary",
                    gist="Annual report",
                    chunk_count=2,
                )
            )
            await session.commit()

        result = await ListDocumentsTool(kb_ids=["kb-1"]).execute(
            {"keyword": "annual"}
        )
        await engine.dispose()
        return result

    result = asyncio.run(run())
    assert result.success is True
    assert result.data["total"] == 1
    assert result.data["documents"][0]["summary_status"] == "completed"
    assert result.data["documents"][0]["gist"] == "Annual report"


def test_read_document_supports_offset_lookup_and_authorization(
    tmp_path, monkeypatch
) -> None:
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/read.db")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        import app.agent.tools.read_document as module

        monkeypatch.setattr(module, "async_session", factory)
        async with factory() as session:
            session.add_all(
                [
                    KnowledgeBase(id="kb-1", name="Allowed"),
                    KnowledgeBase(id="kb-2", name="Denied"),
                    Document(
                        id="doc-a",
                        kb_id="kb-1",
                        filename="policy.txt",
                        file_type="txt",
                        status="completed",
                        chunk_count=3,
                    ),
                    Document(
                        id="doc-b",
                        kb_id="kb-2",
                        filename="secret.txt",
                        file_type="txt",
                        status="completed",
                        chunk_count=1,
                    ),
                ]
            )
            session.add_all(
                [
                    Chunk(
                        id="p1",
                        doc_id="doc-a",
                        kb_id="kb-1",
                        parent_id=None,
                        content="Alpha section",
                        chunk_index=0,
                    ),
                    Chunk(
                        id="p2",
                        doc_id="doc-a",
                        kb_id="kb-1",
                        parent_id=None,
                        content="Beta payment rule",
                        chunk_index=1,
                    ),
                    Chunk(
                        id="p3",
                        doc_id="doc-a",
                        kb_id="kb-1",
                        parent_id=None,
                        content="Gamma close",
                        chunk_index=2,
                    ),
                    Chunk(
                        id="secret-p",
                        doc_id="doc-b",
                        kb_id="kb-2",
                        parent_id=None,
                        content="not visible",
                        chunk_index=0,
                    ),
                ]
            )
            await session.commit()

        tool = ReadDocumentTool(kb_ids=["kb-1"])
        sequential = await tool.execute({"doc_id": "doc-a", "offset": 0, "limit": 12})
        matched = await tool.execute({"doc_id": "doc-a", "query": "payment"})
        denied = await tool.execute({"doc_id": "doc-b"})
        await engine.dispose()
        return sequential, matched, denied

    sequential, matched, denied = asyncio.run(run())
    assert sequential.success is True
    assert sequential.data["fully_read"] is False
    assert sequential.data["next_offset"] == 12
    assert matched.success is True
    assert matched.data["matches"] == 1
    assert "Beta payment rule" in matched.output
    assert denied.success is False


def test_summary_worker_normalizes_structured_profile() -> None:
    profile = DocumentSummaryWorker._normalize_profile(
        '{"summary":"S","gist":"G","topics":["one","two"],'
        '"doc_type":"report","typical_question":"Q?"}',
        "source",
    )

    assert profile["summary"] == "S"
    assert profile["topics"] == ["one", "two"]
    assert profile["doc_type"] == "report"
