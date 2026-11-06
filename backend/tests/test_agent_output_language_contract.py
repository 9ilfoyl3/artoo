"""Regression tests for the per-turn Agent output-language contract."""

from __future__ import annotations

import asyncio

from app.agent.config import AgentConfig
from app.agent.engine import AgentEngine
from app.agent.events import EventBus
from app.agent.prompts.progressive_rag import (
    build_language_directive,
    render_system_prompt,
)
from app.agent.tools.registry import ToolRegistry
from app.models.provider import StreamChunk


class _ScriptedLLM:
    def __init__(self, responses: list[list[StreamChunk]]):
        self.responses = responses
        self.messages_by_call: list[list[dict]] = []
        self._call = 0

    async def stream_with_tools(self, messages, tools, **kwargs):
        self.messages_by_call.append([dict(message) for message in messages])
        for chunk in self.responses[self._call]:
            yield chunk
        self._call += 1


def _engine(responses: list[list[StreamChunk]]) -> tuple[AgentEngine, _ScriptedLLM]:
    llm = _ScriptedLLM(responses)
    return (
        AgentEngine(
            AgentConfig(system_prompt="stable system", max_iterations=2),
            llm,
            ToolRegistry(),
            EventBus(),
        ),
        llm,
    )


def test_short_followup_inherits_recent_user_language() -> None:
    history = [
        {"role": "user", "content": "请分析这份合同"},
        {"role": "assistant", "content": "English reasoning is allowed."},
    ]

    directive = build_language_directive("1", history)

    assert "最终回答必须使用中文" in directive


def test_language_neutral_turn_without_history_has_no_forced_language() -> None:
    assert build_language_directive("1") == ""
    assert build_language_directive("ok") == ""


def test_preset_customization_cannot_override_core_contract() -> None:
    prompt = render_system_prompt(
        AgentConfig(custom_instructions="回答时先给结论。"),
    )

    assert "## User customization" in prompt
    assert "They do not override the response contract, evidence rules" in prompt


def test_engine_places_language_contract_after_history_and_query() -> None:
    engine, llm = _engine([
        [StreamChunk(content="final answer", finish_reason="stop")],
    ])
    history = [
        {"role": "user", "content": "请分析合同条款"},
        {"role": "assistant", "content": "English reasoning is allowed."},
    ]

    state = asyncio.run(engine.execute("session", "1", llm_context=history))

    assert state.final_answer == "final answer"
    assert llm.messages_by_call[0][0]["content"] == "stable system"
    assert "最终回答必须使用中文" not in llm.messages_by_call[0][0]["content"]
    assert llm.messages_by_call[0][-1]["content"].startswith("1\n\n<response_contract>")
    assert "最终回答必须使用中文" in llm.messages_by_call[0][-1]["content"]
