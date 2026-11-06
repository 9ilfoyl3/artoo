from __future__ import annotations

import asyncio

from app.agent.config import AgentConfig
from app.agent.engine import AgentEngine
from app.agent.events import AgentEvent, EventBus, EventType
from app.agent.tools.base import BaseTool, ToolResult
from app.agent.tools.registry import ToolRegistry
from app.api.chat import _agent_event_to_sse
from app.models.llm.failures import LlmFailureInfo
from app.models.provider import LLMToolCall, StreamChunk


class _TransientThenSuccessLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_with_tools(self, messages, tools, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("vLLM 连接失败: timed out")
        yield StreamChunk(content="finished", finish_reason="stop")


class _ReasoningThenTimeoutLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_with_tools(self, messages, tools, **kwargs):
        self.calls += 1
        yield StreamChunk(reasoning="visible plan", response_type="thinking")
        raise RuntimeError("LLM stream stalled: no output for 45s")


class _EchoTool(BaseTool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict) -> ToolResult:
        return ToolResult(success=True, output="observed")


class _ToolThenPermanentTimeoutLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_with_tools(self, messages, tools, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                tool_calls=[LLMToolCall("call-1", "echo", "{}")],
                finish_reason="tool_calls",
            )
            return
        raise RuntimeError("vLLM 连接失败: timed out")


def _engine(llm, *, with_echo: bool = False):
    registry = ToolRegistry()
    if with_echo:
        registry.register(_EchoTool())
    events: list[AgentEvent] = []
    bus = EventBus()

    async def collect(event: AgentEvent) -> None:
        events.append(event)

    bus.on(None, collect)
    engine = AgentEngine(
        AgentConfig(
            system_prompt="test",
            max_iterations=3,
            compaction_checkpoint_enabled=False,
        ),
        llm,
        registry,
        bus,
    )
    return engine, events


def test_transient_failure_emits_retry_event_before_next_attempt(monkeypatch) -> None:
    import app.agent.engine as engine_module

    monkeypatch.setattr(engine_module, "_retry_delay_ms", lambda _attempt: 0)
    llm = _TransientThenSuccessLLM()
    engine, events = _engine(llm)

    state = asyncio.run(engine.execute("session", "question"))

    retry_events = [event for event in events if event.type == EventType.LLM_RETRY]
    assert state.final_answer == "finished"
    assert llm.calls == 2
    assert len(retry_events) == 1
    assert retry_events[0].data == {
        "message": "模型响应中断，正在重试（第 2/3 次）",
        "code": "LLM_TIMEOUT",
        "stage": "llm_call",
        "retryable": True,
        "attempt": 1,
        "next_attempt": 2,
        "max_attempts": 3,
        "delay_ms": 0,
        "iteration": 0,
    }


def test_visible_reasoning_disables_automatic_retry() -> None:
    llm = _ReasoningThenTimeoutLLM()
    engine, events = _engine(llm)

    state = asyncio.run(engine.execute("session", "question"))

    assert llm.calls == 1
    assert not [event for event in events if event.type == EventType.LLM_RETRY]
    assert state.final_answer == "模型请求超时，请重试。"
    error_events = [event for event in events if event.type == EventType.ERROR]
    assert len(error_events) == 1
    assert error_events[0].data["code"] == "LLM_TIMEOUT"
    assert error_events[0].data["attempts"] == 1


def test_llm_failure_after_tool_execution_does_not_synthesize(monkeypatch) -> None:
    import app.agent.engine as engine_module

    monkeypatch.setattr(engine_module, "_retry_delay_ms", lambda _attempt: 0)
    llm = _ToolThenPermanentTimeoutLLM()
    engine, events = _engine(llm, with_echo=True)

    state = asyncio.run(engine.execute("session", "question"))

    assert llm.calls == 4
    assert state.final_answer == "模型请求超时，请重试。"
    assert len([event for event in events if event.type == EventType.LLM_RETRY]) == 2
    assert len([event for event in events if event.type == EventType.TOOL_RESULT]) == 1
    assert len([event for event in events if event.type == EventType.ERROR]) == 1


def test_llm_retry_and_error_map_to_structured_sse() -> None:
    retry_sse = _agent_event_to_sse(AgentEvent(
        type=EventType.LLM_RETRY,
        session_id="session",
        data={
            "message": "模型响应中断，正在重试（第 2/3 次）",
            "code": "LLM_STREAM_IDLE_TIMEOUT",
            "stage": "tool_call_generation",
            "retryable": True,
            "attempt": 1,
            "next_attempt": 2,
            "max_attempts": 3,
            "delay_ms": 1100,
            "iteration": 2,
        },
    ))
    error_sse = _agent_event_to_sse(AgentEvent(
        type=EventType.ERROR,
        session_id="session",
        data={
            "error": "模型响应超时，已停止等待。请重试。",
            "code": "LLM_STREAM_IDLE_TIMEOUT",
            "stage": "tool_call_generation",
            "retryable": True,
            "attempts": 3,
        },
    ))

    assert retry_sse == {
        "type": "llm_retry",
        "message": "模型响应中断，正在重试（第 2/3 次）",
        "code": "LLM_STREAM_IDLE_TIMEOUT",
        "stage": "tool_call_generation",
        "retryable": True,
        "attempt": 1,
        "next_attempt": 2,
        "max_attempts": 3,
        "delay_ms": 1100,
        "iteration": 2,
    }
    assert error_sse == {
        "type": "error",
        "content": "模型响应超时，已停止等待。请重试。",
        "code": "LLM_STREAM_IDLE_TIMEOUT",
        "stage": "tool_call_generation",
        "retryable": True,
        "attempts": 3,
    }


def test_non_transient_http_failure_is_not_retryable() -> None:
    failure = LlmFailureInfo.from_exception(RuntimeError("HTTP 400 Bad Request"))

    assert failure.code == "LLM_HTTP_400"
    assert failure.status == 400
    assert failure.retryable is False
