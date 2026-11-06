from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.config import AgentConfig
from app.models.llm.stream_idle import (
    DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
    LlmStreamIdleTimeout,
)
from app.models.llm.vllm import VllmLLM


class _FakeResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        async for line in self._lines():
            yield line

    async def aclose(self):
        return None


class _FakeStream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeClient:
    def __init__(self, lines):
        self._response = _FakeResponse(lines)
        self.timeouts: list[float] = []

    def stream(self, *args, **kwargs):
        self.timeouts.append(kwargs["timeout"])
        return _FakeStream(self._response)


def _tool_delta(arguments: str, finish_reason: str | None = None) -> str:
    chunk = {
        "choices": [{
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": "call-1",
                    "function": {
                        "name": "search_knowledge",
                        "arguments": arguments,
                    },
                }],
            },
            "finish_reason": finish_reason,
        }],
    }
    return f"data: {json.dumps(chunk)}"


def test_default_stream_idle_timeout_is_300_seconds() -> None:
    assert DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS == 300.0
    assert AgentConfig().llm_stall_timeout == 300.0


def test_tool_call_deltas_and_comments_keep_provider_stream_alive() -> None:
    async def lines():
        yield _tool_delta('{"query":')
        await asyncio.sleep(0.03)
        yield ": keep-alive"
        await asyncio.sleep(0.03)
        yield _tool_delta('"contract"}', finish_reason="tool_calls")
        yield "data: [DONE]"

    async def run():
        llm = VllmLLM("http://llm.test/v1", "test-model")
        llm._client = _FakeClient(lines)
        chunks = [
            chunk
            async for chunk in llm.stream_with_tools(
                [], [], stream_idle_timeout=0.05
            )
        ]
        return chunks, llm._client.timeouts

    chunks, timeouts = asyncio.run(run())

    assert timeouts == [0.05]
    assert chunks[-1].finish_reason == "tool_calls"
    assert chunks[-1].tool_calls is not None
    assert chunks[-1].tool_calls[0].arguments == '{"query":"contract"}'


def test_silent_provider_transport_raises_typed_idle_timeout() -> None:
    async def lines():
        yield _tool_delta('{"query":')
        await asyncio.sleep(0.1)
        yield "data: [DONE]"

    async def run():
        llm = VllmLLM("http://llm.test/v1", "test-model")
        llm._client = _FakeClient(lines)
        with pytest.raises(LlmStreamIdleTimeout) as caught:
            async for _chunk in llm.stream_with_tools(
                [], [], stream_idle_timeout=0.02
            ):
                pass
        return caught.value

    failure = asyncio.run(run())

    assert failure.code == "LLM_STREAM_IDLE_TIMEOUT"
    assert failure.stage == "tool_call_generation"
    assert failure.retryable is True
