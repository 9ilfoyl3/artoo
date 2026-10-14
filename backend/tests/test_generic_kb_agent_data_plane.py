"""Regression tests for generic knowledge-base inventory and read coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import agent_config
from app.agent.config import AgentConfig
from app.agent.evidence import evidence_id_for, register_chunk_evidence
from app.agent.engine import AgentEngine
from app.agent.events import EventBus
from app.agent.prompts.progressive_rag import render_system_prompt
from app.agent.state import AgentState
from app.agent.summary_cache import SummaryCache
from app.agent.tools.base import BaseTool, ToolResult
from app.agent.tools.calculator import CalculateTool
from app.agent.tools.list_chunks import ListKnowledgeChunksTool, ReadDocumentTool
from app.agent.tools.list_documents import ListKnowledgeDocumentsTool
from app.agent.tools.summarize_documents import SummarizeDocumentsTool
from app.agent.tools.registry import ToolRegistry
from app.models.provider import LLMToolCall, StreamChunk
from app.schema.db import AgentPreset, Base, Chunk, Document, KnowledgeBase, SessionFile


async def _make_session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


class _FakeSummaryLLM:
    def __init__(self):
        self.calls = 0

    async def generate(self, messages, **kwargs):
        self.calls += 1
        return "faithful summary"


class _MemorySummaryCache:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, summary: str) -> None:
        self.values[key] = summary


class _SuggestRewriteTool(BaseTool):
    @property
    def name(self) -> str:
        return "knowledge_search"

    @property
    def description(self) -> str:
        return "test"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict) -> ToolResult:
        return ToolResult(
            success=True,
            output="<search_results count=\"0\" />",
            data={"suggest_rewrite": True},
        )


class _InventoryStubTool(BaseTool):
    @property
    def name(self) -> str:
        return "list_knowledge_documents"

    @property
    def description(self) -> str:
        return "test"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict) -> ToolResult:
        return ToolResult(success=True, output="<documents />")


class _InventoryAwareLLM:
    def __init__(self):
        self.calls = 0

    async def stream_with_tools(self, messages, tools, **kwargs):
        self.calls += 1
        if any(
            "list_knowledge_documents to verify" in (message.get("content") or "")
            for message in messages
        ):
            yield StreamChunk(content="finished", finish_reason="stop")
            return
        yield StreamChunk(
            tool_calls=[LLMToolCall("call-1", "knowledge_search", "{}")],
            finish_reason="tool_calls",
        )


def test_builtin_smart_reasoning_preset_upgrades_to_new_kb_tools(
    tmp_path, monkeypatch
):
    async def run():
        engine, factory = await _make_session_factory(tmp_path)
        try:
            monkeypatch.setattr(agent_config, "async_session", factory)
            async with factory() as session:
                session.add(
                    AgentPreset(
                        id="preset-smart-reasoning",
                        name="智能推理",
                        config_json={
                            "agent_mode": "agent",
                            "allowed_tools": [
                                "knowledge_search",
                                "grep_chunks",
                                "list_knowledge_chunks",
                            ],
                            "custom_rollout_flag": True,
                        },
                        is_default=True,
                        tenant_id=None,
                        owner_user_id=None,
                        is_shared=True,
                    )
                )
                await session.commit()

            await agent_config._ensure_builtin_presets()

            async with factory() as session:
                preset = await session.get(AgentPreset, "preset-smart-reasoning")
                assert preset is not None
                assert preset.config_json["custom_rollout_flag"] is True
                assert {
                    "list_knowledge_documents",
                    "read_document",
                    "summarize_documents",
                    "calculate",
                }.issubset(set(preset.config_json["allowed_tools"]))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_document_catalog_lists_only_authorized_completed_documents(tmp_path, monkeypatch):
    import app.agent.tools.list_documents as module

    async def run():
        engine, factory = await _make_session_factory(tmp_path)
        try:
            monkeypatch.setattr(module, "async_session", factory)
            async with factory() as session:
                session.add_all([
                    KnowledgeBase(id="kb-a", name="A"),
                    KnowledgeBase(id="kb-b", name="B"),
                ])
                session.add_all([
                    Document(
                        id="doc-a1",
                        kb_id="kb-a",
                        filename="a1.pdf",
                        file_type="pdf",
                        status="completed",
                        chunk_count=3,
                    ),
                    Document(
                        id="doc-a2",
                        kb_id="kb-a",
                        filename="a2.pdf",
                        file_type="pdf",
                        status="failed",
                        chunk_count=0,
                    ),
                    Document(
                        id="doc-b1",
                        kb_id="kb-b",
                        filename="b1.pdf",
                        file_type="pdf",
                        status="completed",
                        chunk_count=5,
                    ),
                ])
                await session.commit()

            state = AgentState()
            tool = ListKnowledgeDocumentsTool(
                kb_ids=["kb-a"],
                state=state,
            )
            result = await tool.execute({
                "page": 1,
                "page_size": 10,
                "status": "completed",
            })

            assert result.success is True
            assert result.data["total"] == 1
            assert [item["doc_id"] for item in result.data["documents"]] == ["doc-a1"]
            assert result.data["has_more"] is False
            assert state.document_catalog["doc-a1"]["filename"] == "a1.pdf"

            all_statuses = await tool.execute({"page": 1, "page_size": 10})
            assert {
                item["doc_id"] for item in all_statuses.data["documents"]
            } == {"doc-a1", "doc-a2"}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_evidence_state_has_stable_id_and_chunk_index():
    state = AgentState()
    first = register_chunk_evidence(
        state,
        doc_id="doc-1",
        chunk_id="chunk-1",
        content="source text",
        source="read_document",
    )
    second = register_chunk_evidence(
        state,
        doc_id="doc-1",
        chunk_id="chunk-1",
        content="source text",
        source="read_document",
    )

    assert first == (evidence_id_for("doc-1", "chunk-1"), True)
    assert second == (first[0], False)
    assert state.evidence[first[0]]["excerpt"] == "source text"


def test_empty_retrieval_falls_back_to_document_inventory_once():
    llm = _InventoryAwareLLM()
    registry = ToolRegistry()
    registry.register(_SuggestRewriteTool())
    registry.register(_InventoryStubTool())
    engine = AgentEngine(
        AgentConfig(
            system_prompt="stable",
            max_iterations=4,
            query_rewrite_max_attempts=0,
        ),
        llm,
        registry,
        EventBus(),
    )

    state = asyncio.run(engine.execute("session", "summarize all documents"))

    assert state.final_answer == "finished"
    assert llm.calls == 2


def test_prompt_guides_collection_tasks_when_catalog_tool_is_available():
    prompt = render_system_prompt(
        AgentConfig(),
        available_tools=["list_knowledge_documents", "summarize_documents"],
    )

    assert "Document collection tasks" in prompt
    assert "Never infer missing files" in prompt
    assert "summarize_documents" in prompt


def test_summary_skills_prefer_catalog_and_document_tools():
    skill_root = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "agent"
        / "skills"
        / "preloaded"
    )
    for skill_name in ("meeting-summarizer", "document-analyzer"):
        instructions = (skill_root / skill_name / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "list_knowledge_documents" in instructions
        assert "read_document" in instructions
        assert "summarize_documents" in instructions


def test_deterministic_calculator_uses_decimal_and_rejects_unsafe_input():
    calculator = CalculateTool()

    result = asyncio.run(calculator.execute({
        "expression": "(750 + 1120 - 1600)",
        "precision": 2,
        "rounding": "half_up",
    }))
    percentage = asyncio.run(calculator.execute({
        "expression": "percent(30, 100)",
    }))
    traversal = asyncio.run(calculator.execute({
        "expression": "__import__('os').system('id')",
    }))

    assert result.success is True
    assert result.data["result"] == "270.00"
    assert percentage.data["result"] == "30.00"
    assert traversal.success is False


def test_summary_cache_key_changes_with_document_version_or_instruction():
    base = SummaryCache.build_key("doc-1", "v1", "model-a", "Summarize")
    changed_version = SummaryCache.build_key("doc-1", "v2", "model-a", "Summarize")
    changed_instruction = SummaryCache.build_key("doc-1", "v1", "model-a", "Extract risks")

    assert len({base, changed_version, changed_instruction}) == 3


def test_document_catalog_paginates_without_merging_unauthorized_kbs(tmp_path, monkeypatch):
    import app.agent.tools.list_documents as module

    async def run():
        engine, factory = await _make_session_factory(tmp_path)
        try:
            monkeypatch.setattr(module, "async_session", factory)
            async with factory() as session:
                session.add(KnowledgeBase(id="kb-a", name="A"))
                session.add_all([
                    Document(
                        id=f"doc-{index}",
                        kb_id="kb-a",
                        filename=f"doc-{index}.txt",
                        file_type="txt",
                        status="completed",
                        chunk_count=1,
                    )
                    for index in range(3)
                ])
                await session.commit()

            tool = ListKnowledgeDocumentsTool(kb_ids=["kb-a"])
            first = await tool.execute({"page": 1, "page_size": 2})
            second = await tool.execute({"page": 2, "page_size": 2})

            assert first.data["has_more"] is True
            assert first.data["next_page"] == 2
            assert len(first.data["documents"]) == 2
            assert second.data["has_more"] is False
            assert second.data["next_page"] is None
            assert len(second.data["documents"]) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_document_catalog_includes_session_files_when_enabled(tmp_path, monkeypatch):
    import app.agent.tools.list_documents as module

    async def run():
        engine, factory = await _make_session_factory(tmp_path)
        try:
            monkeypatch.setattr(module, "async_session", factory)
            async with factory() as session:
                session.add(KnowledgeBase(id="kb-a", name="A"))
                session.add_all([
                    Document(
                        id="doc-a",
                        kb_id="kb-a",
                        filename="formal.pdf",
                        file_type="pdf",
                        status="completed",
                        chunk_count=2,
                    ),
                    SessionFile(
                        id="session-file-1",
                        session_id="session-1",
                        filename="upload.txt",
                        file_type="txt",
                        status="completed",
                        chunk_count=1,
                    ),
                ])
                await session.commit()

            tool = ListKnowledgeDocumentsTool(
                kb_ids=["kb-a"],
                session_id="session-1",
                include_session_source=True,
            )
            result = await tool.execute({})

            assert result.data["total"] == 2
            assert {item["source"] for item in result.data["documents"]} == {
                "document",
                "session-file",
            }
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_document_read_uses_parent_chunks_and_tracks_coverage(tmp_path, monkeypatch):
    import app.agent.tools.list_chunks as module

    async def run():
        engine, factory = await _make_session_factory(tmp_path)
        try:
            monkeypatch.setattr(module, "async_session", factory)
            async with factory() as session:
                session.add(KnowledgeBase(id="kb-a", name="A"))
                session.add(
                    Document(
                        id="doc-a",
                        kb_id="kb-a",
                        filename="doc.pdf",
                        file_type="pdf",
                        status="completed",
                        chunk_count=2,
                    )
                )
                session.add_all([
                    Chunk(
                        id="parent-1",
                        doc_id="doc-a",
                        kb_id="kb-a",
                        parent_id=None,
                        content="parent one",
                        chunk_index=0,
                    ),
                    Chunk(
                        id="child-1",
                        doc_id="doc-a",
                        kb_id="kb-a",
                        parent_id="parent-1",
                        content="child one",
                        chunk_index=0,
                    ),
                    Chunk(
                        id="parent-2",
                        doc_id="doc-a",
                        kb_id="kb-a",
                        parent_id=None,
                        content="parent two",
                        chunk_index=1,
                    ),
                ])
                await session.commit()

            state = AgentState()
            tool = ReadDocumentTool(state=state)
            assert tool.name == "read_document"
            assert ListKnowledgeChunksTool(state=state).name == "list_knowledge_chunks"
            first = await tool.execute({"doc_id": "doc-a", "page": 1, "page_size": 1})
            second = await tool.execute({"doc_id": "doc-a", "page": 2, "page_size": 1})

            assert first.data["total_chunks"] == 2
            assert first.data["has_more"] is True
            assert first.data["fully_read"] is False
            assert "child one" not in first.output
            assert second.data["fully_read"] is True
            assert second.data["next_page"] is None
            assert state.document_read_ranges["doc-a"] == {0, 1}
            assert state.document_catalog["doc-a"]["fully_read"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_document_summarizer_maps_each_document_independently(tmp_path, monkeypatch):
    import app.agent.tools.doc_files as doc_files_module
    import app.agent.tools.summarize_documents as module

    async def run():
        engine, factory = await _make_session_factory(tmp_path)
        try:
            monkeypatch.setattr(module, "async_session", factory)
            monkeypatch.setattr(doc_files_module, "async_session", factory)
            async with factory() as session:
                session.add(KnowledgeBase(id="kb-a", name="A"))
                session.add_all([
                    Document(
                        id="doc-a",
                        kb_id="kb-a",
                        filename="a.pdf",
                        file_type="pdf",
                        status="completed",
                        chunk_count=2,
                    ),
                    Document(
                        id="doc-b",
                        kb_id="kb-a",
                        filename="b.pdf",
                        file_type="pdf",
                        status="completed",
                        chunk_count=1,
                    ),
                ])
                session.add_all([
                    Chunk(
                        id="a-parent-1",
                        doc_id="doc-a",
                        kb_id="kb-a",
                        parent_id=None,
                        content="A one",
                        chunk_index=0,
                    ),
                    Chunk(
                        id="a-child-1",
                        doc_id="doc-a",
                        kb_id="kb-a",
                        parent_id="a-parent-1",
                        content="A child",
                        chunk_index=0,
                    ),
                    Chunk(
                        id="a-parent-2",
                        doc_id="doc-a",
                        kb_id="kb-a",
                        parent_id=None,
                        content="A two",
                        chunk_index=1,
                    ),
                    Chunk(
                        id="b-parent-1",
                        doc_id="doc-b",
                        kb_id="kb-a",
                        parent_id=None,
                        content="B one",
                        chunk_index=0,
                    ),
                ])
                await session.commit()

            llm = _FakeSummaryLLM()
            state = AgentState()
            cache = _MemorySummaryCache()
            tool = SummarizeDocumentsTool(llm=llm, state=state, cache=cache)
            result = await tool.execute({
                "doc_ids": ["doc-a", "doc-b"],
                "instruction": "Summarize the core points.",
            })
            cached_result = await tool.execute({
                "doc_ids": ["doc-a", "doc-b"],
                "instruction": "Summarize the core points.",
            })

            assert result.success is True
            assert llm.calls == 2
            assert cached_result.data["cache_hits"] == 2
            assert llm.calls == 2
            assert result.data["documents"][0]["chunk_count"] == 2
            assert "A child" not in result.output
            assert state.document_catalog["doc-a"]["fully_read"] is True
            assert state.document_catalog["doc-b"]["fully_read"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())
