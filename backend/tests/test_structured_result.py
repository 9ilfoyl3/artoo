from __future__ import annotations

import asyncio
import time

import pytest

from app.agent.config import AgentConfig
from app.agent.tools.base import BaseTool, ToolResult
from app.agent.engine import AgentEngine, _tool_feedback_notice
from app.agent.events import AgentEvent, EventBus, EventType
from app.api.chat import _agent_event_to_sse
from app.agent.tools.registry import ToolRegistry
from app.agent.tools.structured_result import (
    MAX_STRUCTURED_ARRAY_ITEMS,
    MAX_STRUCTURED_CONTENT_BYTES,
    StructuredResultValidationError,
    normalize_output_schema,
    normalize_structured_content,
)
from app.models.provider import LLMToolCall, StreamChunk


class _StructuredTool(BaseTool):
    @property
    def name(self) -> str:
        return "structured"

    @property
    def description(self) -> str:
        return "structured result test"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict) -> ToolResult:
        return ToolResult(
            success=True,
            output="x" * 100,
            structured_content={
                "kind": "dev.artoo.test.structured.v1",
                "data": {"value": "kept"},
            },
        )


class _RecoverableFeedbackTool(BaseTool):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "recoverable_feedback"

    @property
    def description(self) -> str:
        return "returns recoverable structured feedback"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict) -> ToolResult:
        self.calls += 1
        return ToolResult(
            success=False,
            error="temporary validation failure",
            structured_content={
                "kind": "io.example.tool-result.v1",
                "data": {"attempt": self.calls},
                "feedback": {
                    "status": "retryable_error",
                    "code": "io.example.temporary-failure",
                    "message": "Retry after correcting the request.",
                    "recoverable": True,
                    "next_actions": ["retry"],
                },
            },
        )


class _FeedbackAwareLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_with_tools(self, messages, tools, **kwargs):
        self.calls += 1
        if any(
            "retry_policy=budget_exhausted" in str(message.get("content") or "")
            for message in messages
        ):
            yield StreamChunk(content="finished", finish_reason="stop")
            return
        yield StreamChunk(
            tool_calls=[LLMToolCall(
                f"call-{self.calls}",
                "recoverable_feedback",
                "{}",
            )],
            finish_reason="tool_calls",
        )


class _TimedTool(BaseTool):
    def __init__(self, name: str, delay: float) -> None:
        self._name = name
        self._delay = delay
        self.started_at: float | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "timed test tool"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict) -> ToolResult:
        self.started_at = time.perf_counter()
        await asyncio.sleep(self._delay)
        return ToolResult(success=True, output=self._name)


class _TwoToolLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_with_tools(self, messages, tools, **kwargs):
        self.calls += 1
        if any(message.get("role") == "tool" for message in messages):
            yield StreamChunk(content="finished", finish_reason="stop")
            return
        yield StreamChunk(
            tool_calls=[
                LLMToolCall("slow-call", "slow_tool", "{}"),
                LLMToolCall("fast-call", "fast_tool", "{}"),
            ],
            finish_reason="tool_calls",
        )


def test_normalize_structured_content_accepts_valid_envelope():
    content = {
        "kind": "dev.law-agent-lite.legal-provision-search.v1",
        "data": {
            "query": "民法典第146条",
            "items": [{"lawName": "中华人民共和国民法典"}],
        },
    }

    assert normalize_structured_content(content) == content


def test_normalize_structured_content_accepts_bounded_feedback():
    content = {
        "kind": "io.example.tool-result.v1",
        "data": {"value": "kept"},
        "feedback": {
            "status": "needs_evidence",
            "code": "io.example.needs-evidence",
            "message": "Provide a source identifier.",
            "recoverable": True,
            "next_actions": ["request_source", "retry"],
        },
    }

    assert normalize_structured_content(content) == content
    notice = _tool_feedback_notice(content)
    assert "status=needs_evidence" in notice
    assert "next_actions=request_source | retry" in notice


def test_normalize_structured_content_rejects_invalid_feedback():
    with pytest.raises(StructuredResultValidationError):
        normalize_structured_content({
            "kind": "io.example.tool-result.v1",
            "data": {},
            "feedback": {"status": "unknown"},
        })
    with pytest.raises(StructuredResultValidationError):
        normalize_structured_content({
            "kind": "io.example.tool-result.v1",
            "data": {},
            "feedback": {
                "status": "retryable_error",
                "next_actions": ["x" * 201],
            },
        })


def test_normalize_structured_content_validates_output_schema():
    schema = {
        "type": "object",
        "required": ["kind", "data"],
        "properties": {
            "kind": {"const": "dev.law-agent-lite.legal-provision-search.v1"},
            "data": {
                "type": "object",
                "required": ["items"],
                "properties": {"items": {"type": "array"}},
            },
        },
    }
    normalized_schema = normalize_output_schema(schema)

    normalized = normalize_structured_content(
        {
            "kind": "dev.law-agent-lite.legal-provision-search.v1",
            "data": {"items": []},
        },
        output_schema=normalized_schema,
    )

    assert normalized["data"] == {"items": []}


@pytest.mark.parametrize(
    "content",
    [
        {"kind": "Invalid Kind", "data": {}},
        {"kind": "dev.test.result.v1"},
        {"kind": "dev.test.result.v1", "data": {"__proto__": {}}},
        {
            "kind": "dev.test.result.v1",
            "data": {"items": list(range(MAX_STRUCTURED_ARRAY_ITEMS + 1))},
        },
    ],
)
def test_normalize_structured_content_rejects_invalid_generic_shape(content):
    with pytest.raises(StructuredResultValidationError):
        normalize_structured_content(content)


def test_normalize_structured_content_rejects_oversized_payload():
    content = {
        "kind": "dev.test.result.v1",
        "data": {"text": "x" * MAX_STRUCTURED_CONTENT_BYTES},
    }

    with pytest.raises(StructuredResultValidationError):
        normalize_structured_content(content)


def test_output_schema_rejects_external_refs():
    with pytest.raises(StructuredResultValidationError):
        normalize_output_schema({
            "$ref": "https://example.com/schema.json",
        })


def test_unresolvable_local_ref_degrades_to_validation_error():
    schema = normalize_output_schema({"$ref": "#/$defs/missing"})

    with pytest.raises(StructuredResultValidationError):
        normalize_structured_content(
            {"kind": "dev.test.result.v1", "data": {}},
            output_schema=schema,
        )


def test_tool_registry_preserves_structured_content_when_text_is_truncated():
    registry = ToolRegistry(max_tool_output_chars=10)
    registry.register(_StructuredTool())

    result = asyncio.run(registry.execute("structured", {}))

    assert "[...truncated" in result.output
    assert result.structured_content == {
        "kind": "dev.artoo.test.structured.v1",
        "data": {"value": "kept"},
    }


def test_structured_feedback_is_included_in_tool_result_sse():
    payload = {
        "kind": "io.example.tool-result.v1",
        "data": {"value": "kept"},
        "feedback": {
            "status": "rejected",
            "code": "io.example.validation-failed",
            "message": "Fix the proposal.",
            "recoverable": True,
            "next_actions": ["revise"],
        },
    }

    event = AgentEvent(
        EventType.TOOL_RESULT,
        "session",
        {
            "tool_call_id": "call-1",
            "tool_name": "external_tool",
            "success": False,
            "duration_ms": 3,
            "files": [],
            "structured_content": payload,
        },
    )

    assert _agent_event_to_sse(event)["structured_content"] == payload


def test_recoverable_feedback_has_a_bounded_retry_budget():
    tool = _RecoverableFeedbackTool()
    llm = _FeedbackAwareLLM()
    registry = ToolRegistry()
    registry.register(tool)
    engine = AgentEngine(
        AgentConfig(
            system_prompt="stable",
            max_iterations=8,
        ),
        llm,
        registry,
        EventBus(),
    )

    state = asyncio.run(engine.execute("session", "retry safely"))

    assert state.final_answer == "finished"
    assert tool.calls == 3
    assert llm.calls == 4


def test_non_recoverable_feedback_marks_retry_forbidden():
    notice = _tool_feedback_notice({
        "kind": "io.example.tool-result.v1",
        "data": {},
        "feedback": {
            "status": "rejected",
            "code": "io.example.invalid",
            "recoverable": False,
        },
    })

    assert "recoverable=false" in notice
    assert "retry_policy=do_not_retry" in notice


def test_sequential_tool_result_is_emitted_before_next_tool_starts():
    slow = _TimedTool("slow_tool", 0.05)
    fast = _TimedTool("fast_tool", 0.01)
    registry = ToolRegistry()
    registry.register(slow)
    registry.register(fast)
    bus = EventBus()
    result_times = {}

    async def collect(event):
        if event.type == EventType.TOOL_RESULT:
            result_times[event.data["tool_name"]] = time.perf_counter()

    bus.on(EventType.TOOL_RESULT, collect)
    engine = AgentEngine(
        AgentConfig(
            system_prompt="stable",
            max_iterations=2,
            parallel_tool_calls=False,
        ),
        _TwoToolLLM(),
        registry,
        bus,
    )

    state = asyncio.run(engine.execute("session", "run tools"))

    assert state.final_answer == "finished"
    assert fast.started_at is not None
    assert result_times["slow_tool"] < fast.started_at


def test_tool_progress_serializes_as_a_separate_sse_event():
    event = AgentEvent(
        EventType.TOOL_PROGRESS,
        "session",
        {
            "tool_call_id": "call-1",
            "tool_name": "summarize_documents",
            "iteration": 1,
            "phase": "document",
            "completed": 4,
            "total": 20,
            "filename": "a.pdf",
            "cache_hit": False,
            "elapsed_ms": 1200,
        },
    )

    payload = _agent_event_to_sse(event)

    assert payload["type"] == "tool_progress"
    assert payload["completed"] == 4
    assert payload["total"] == 20
    assert payload["filename"] == "a.pdf"
