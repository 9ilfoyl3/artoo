"""DSH-style ReAct engine.

A model response is a typed assistant message: reasoning, text, and tool
calls arrive as separate channels. The loop terminates on a natural text
response; tool calls are executed and fed back as observations. There is no
answer-carrying tool.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from app.agent.config import AgentConfig
from app.agent.content_classifier import ChannelFragment, PlainContentClassifier
from app.agent.events import AgentEvent, EventBus, EventType
from app.agent.memory.compress import compress_context
from app.agent.memory.consolidator import MemoryConsolidator
from app.agent.memory.token_estimator import TokenEstimator
from app.agent.memory.usage_tracker import UsageTracker
from app.agent.state import AgentState, AgentStep, ToolCallRecord
from app.agent.tools.base import ToolContext, ToolResult
from app.agent.tools.registry import ToolRegistry
from app.agent.tools.text_sanitize import strip_think_blocks
from app.models.provider import ChatResponse, LLMProvider, LLMToolCall, StreamChunk, TokenUsage

logger = logging.getLogger(__name__)

_DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant with knowledge-base tools. Use tools when "
    "evidence is needed. When you can answer, write the complete answer as "
    "ordinary assistant text and stop without calling a tool."
)
_NUDGE_MESSAGE = (
    "Continue. If you need evidence, call one of the available tools. "
    "If you can answer now, write the complete answer as plain text and stop."
)
_EMPTY_FINAL_ANSWER = "抱歉，模型没有返回可展示的回答。请重试或调整问题。"
_MAX_EMPTY_RETRIES = 2
_MAX_TRANSIENT_RETRIES = 2
_TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
_STUCK_LOOP_THRESHOLD = 3
_KB_TOOL_NAMES = {"knowledge_search", "grep_chunks", "search_knowledge_base"}
_KB_REDACTION_MARKER = "[Previous retrieval omitted — perform a fresh search.]"


def _redact_history_kb_results(messages: list[dict]) -> list[dict]:
    """Redact old KB observations so each factual turn retrieves fresh evidence."""
    if not messages:
        return messages

    call_names: dict[str, str] = {}
    call_rounds: dict[str, int] = {}
    model_round = -1
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            model_round += 1
            for call in msg.get("tool_calls") or []:
                call_id = call.get("id", "")
                if call_id:
                    call_names[call_id] = call.get("function", {}).get("name", "")
                    call_rounds[call_id] = model_round
    if not call_rounds:
        return messages

    latest_round = max(call_rounds.values())
    result: list[dict] = []
    for msg in messages:
        if msg.get("role") == "tool":
            call_id = msg.get("tool_call_id", "")
            if call_names.get(call_id) in _KB_TOOL_NAMES and call_rounds.get(call_id) != latest_round:
                redacted = dict(msg)
                redacted["content"] = _KB_REDACTION_MARKER
                result.append(redacted)
                continue
        result.append(msg)
    return result


class _AssistantResponseBuilder:
    """Classify provider chunks and emit stable public channel events."""

    def __init__(self, event_bus: EventBus, session_id: str, iteration: int):
        self.event_bus = event_bus
        self.session_id = session_id
        self.iteration = iteration
        self.content = ""
        self.native_reasoning = ""
        self.display_reasoning = ""
        self.answer_streamed = False
        self.reasoning_streamed = False
        self.tool_calls: list[LLMToolCall] = []
        self.finish_reason = ""
        self.usage: TokenUsage | None = None
        self._classifier = PlainContentClassifier()
        self._has_native_reasoning = False

    async def add(self, chunk: StreamChunk) -> None:
        if chunk.reasoning:
            self._has_native_reasoning = True
            self.native_reasoning += chunk.reasoning
            self.display_reasoning += chunk.reasoning
            self.reasoning_streamed = True
            await self._emit(EventType.REASONING_DELTA, chunk.reasoning)

        if chunk.content:
            self.content += chunk.content
            if self._has_native_reasoning:
                await self._emit_text(chunk.content)
            else:
                for fragment in self._classifier.feed(chunk.content):
                    await self._emit_fragment(fragment)

        if chunk.tool_calls:
            self.tool_calls = list(chunk.tool_calls)
        if chunk.usage:
            self.usage = chunk.usage
        if chunk.finish_reason:
            self.finish_reason = chunk.finish_reason

    async def finalize(self) -> ChatResponse:
        fallback = "reasoning" if self.tool_calls else "text"
        for fragment in self._classifier.finalize(fallback):
            await self._emit_fragment(fragment)

        content_channel = "text" if self.answer_streamed or not self.tool_calls else "reasoning"
        return ChatResponse(
            content=self.content,
            reasoning_content=self.native_reasoning,
            display_reasoning=self.display_reasoning,
            tool_calls=self.tool_calls,
            finish_reason=self.finish_reason or "stop",
            usage=self.usage,
            content_channel=content_channel,
        )

    async def _emit_fragment(self, fragment: ChannelFragment) -> None:
        if fragment.kind == "reasoning":
            self.display_reasoning += fragment.text
            self.reasoning_streamed = True
            await self._emit(EventType.REASONING_DELTA, fragment.text)
        else:
            await self._emit_text(fragment.text)

    async def _emit_text(self, text: str) -> None:
        self.answer_streamed = True
        await self._emit(EventType.TEXT_DELTA, text)

    async def _emit(self, event_type: EventType, text: str) -> None:
        if not text:
            return
        await self.event_bus.emit(AgentEvent(
            type=event_type,
            session_id=self.session_id,
            data={"content": text, "iteration": self.iteration},
        ))


@dataclass
class _RoundResult:
    response: ChatResponse
    step: AgentStep


class AgentEngine:
    """ReAct loop over typed provider channels."""

    def __init__(
        self,
        config: AgentConfig,
        llm: LLMProvider,
        tool_registry: ToolRegistry,
        event_bus: EventBus,
        tool_context: ToolContext | None = None,
    ) -> None:
        self._config = config
        self._llm = llm
        self._tool_registry = tool_registry
        self._event_bus = event_bus
        self._tool_context = tool_context
        self._token_estimator = TokenEstimator()
        self._usage_tracker = UsageTracker(self._token_estimator)
        self._consolidator = MemoryConsolidator(
            self._llm,
            self._token_estimator,
            self._config.max_context_tokens,
            self._config.consolidation_threshold,
        )
        self._last_query = ""

    async def execute(
        self,
        session_id: str,
        query: str,
        llm_context: list[dict] | None = None,
        image_urls: list[str] | None = None,
    ) -> AgentState:
        del image_urls
        logger.info(
            "[Agent] start session=%s query_len=%d context_msgs=%d",
            session_id, len(query), len(llm_context or []),
        )
        state = AgentState()
        system_prompt = self._config.system_prompt or _DEFAULT_SYSTEM_PROMPT
        if self._config.knowledge_base_ids and not self._config.system_prompt:
            try:
                from app.agent.prompts.progressive_rag import render_system_prompt
                kb_names = await self._query_kb_names(self._config.knowledge_base_ids)
                system_prompt = render_system_prompt(
                    self._config,
                    kb_names=kb_names,
                    available_tools=self._tool_registry.list_tools(),
                )
            except Exception as exc:
                logger.warning("[Agent] failed to render KB prompt: %s", exc)

        try:
            from app.agent.prompts.progressive_rag import build_language_directive
            directive = build_language_directive(query)
            if directive:
                system_prompt = f"{system_prompt}\n\n{directive}"
        except Exception as exc:
            logger.warning("[Agent] failed to build language directive: %s", exc)

        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        if llm_context:
            messages.extend(_redact_history_kb_results(llm_context))
        messages.append({"role": "user", "content": query})
        self._last_query = query

        tools = self._tool_registry.get_function_definitions()
        await self._execute_loop(state, messages, tools, session_id)
        logger.info(
            "[Agent] complete session=%s rounds=%d complete=%s answer_len=%d",
            session_id, len(state.steps), state.is_complete, len(state.final_answer),
        )
        return state

    async def _execute_loop(
        self,
        state: AgentState,
        messages: list[dict],
        tools: list[dict],
        session_id: str,
    ) -> None:
        empty_retries = 0

        while state.current_round < self._config.max_iterations:
            iteration = state.current_round
            logger.info("[Agent][Round-%d/%d] starting", iteration + 1, self._config.max_iterations)

            current_tokens = self._usage_tracker.estimate_current_tokens(messages)
            if self._consolidator.should_consolidate(current_tokens):
                messages = await self._consolidator.consolidate(messages)
                current_tokens = self._usage_tracker.estimate_current_tokens(messages)
            messages = compress_context(
                messages, self._token_estimator, self._config.max_context_tokens, current_tokens,
            )
            sent_count = len(messages)

            try:
                result = await self._call_model(messages, tools, session_id, iteration)
            except RuntimeError as exc:
                logger.exception("[Agent][Round-%d] model call failed", iteration + 1)
                if state.steps:
                    await self._synthesize_answer(state, session_id, "error")
                else:
                    state.final_answer = f"抱歉，处理请求时遇到错误：{exc}"
                    state.is_complete = True
                    await self._event_bus.emit(AgentEvent(
                        type=EventType.ERROR, session_id=session_id,
                        data={"error": str(exc), "stage": "llm_call"},
                    ))
                    await self._emit_turn_end(session_id, "error")
                return

            response, step = result.response, result.step
            if response.usage:
                self._usage_tracker.update(response.usage, sent_count)
            await self._emit_token_usage(messages, response.usage)

            if not response.content and not response.tool_calls:
                empty_retries += 1
                if empty_retries <= _MAX_EMPTY_RETRIES:
                    logger.warning("[Agent][Round-%d] empty response; nudging", iteration + 1)
                    messages.append({"role": "user", "content": _NUDGE_MESSAGE})
                    continue
                logger.warning("[Agent][Round-%d] empty response after retries", iteration + 1)
                state.final_answer = _EMPTY_FINAL_ANSWER
                state.is_complete = True
                await self._emit_turn_end(session_id, "empty")
                return

            if not response.tool_calls:
                answer = strip_think_blocks(response.content).strip()
                state.final_answer = answer
                state.is_complete = True
                state.steps.append(step)
                state.current_round += 1
                await self._emit_turn_end(session_id, response.finish_reason)
                return

            # A content prefix in a tool-call round is planning for models without
            # a native reasoning channel. It is never treated as the answer.
            step.content = strip_think_blocks(response.content)
            messages.append({
                "role": "assistant",
                # DSH/official DeepSeek rule: replay reasoning on tool-call turns;
                # a null content is less portable than an empty string.
                "content": step.content or "",
                **(
                    {"reasoning_content": response.reasoning_content}
                    if response.reasoning_content else {}
                ),
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function_name,
                            "arguments": call.arguments,
                        },
                    }
                    for call in response.tool_calls
                ],
            })
            tool_results = await self._execute_tool_calls(response.tool_calls, step, session_id)
            for call, result in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result.output if result.success else (result.error or "Tool execution failed."),
                })
            state.steps.append(step)
            state.current_round += 1

        logger.warning("[Agent] max iterations reached (%d)", self._config.max_iterations)
        await self._synthesize_answer(state, session_id, "max_iterations")

    async def _call_model(
        self,
        messages: list[dict],
        tools: list[dict],
        session_id: str,
        iteration: int,
    ) -> _RoundResult:
        last_error: Exception | None = None
        for attempt in range(_MAX_TRANSIENT_RETRIES + 1):
            try:
                builder = _AssistantResponseBuilder(self._event_bus, session_id, iteration)
                call_kwargs: dict = {
                    "temperature": self._config.temperature,
                    "enable_thinking": self._config.thinking_enabled,
                }
                if self._config.max_output_tokens is not None:
                    call_kwargs["max_tokens"] = self._config.max_output_tokens

                async for chunk in self._llm.stream_with_tools(messages, tools, **call_kwargs):
                    await builder.add(chunk)
                response = await builder.finalize()
                step = AgentStep(
                    iteration=iteration,
                    reasoning=response.display_reasoning,
                    content=strip_think_blocks(response.content),
                )
                logger.info(
                    "[Agent][Round-%d] finished=%s tools=%d content=%d reasoning=%d channel=%s",
                    iteration + 1, response.finish_reason, len(response.tool_calls),
                    len(response.content), len(response.display_reasoning), response.content_channel,
                )
                return _RoundResult(response, step)
            except RuntimeError as exc:
                last_error = exc
                message = str(exc)
                transient = (
                    any(str(code) in message for code in _TRANSIENT_HTTP_CODES)
                    or "timeout" in message.lower()
                    or "timed out" in message.lower()
                )
                if not transient:
                    raise
                if attempt < _MAX_TRANSIENT_RETRIES:
                    await asyncio.sleep(2**attempt)

        raise RuntimeError(f"LLM call failed after retries: {last_error}")

    async def _synthesize_answer(
        self,
        state: AgentState,
        session_id: str,
        finish_reason: str,
    ) -> None:
        """Produce an answer after loop exhaustion or an unexpected model failure."""
        tool_outputs = [
            record.result.output
            for step in state.steps
            for record in step.tool_calls
            if record.result and record.result.success and record.result.output
        ]
        if not tool_outputs:
            state.final_answer = "抱歉，在有限步骤内未能获得足够信息。请重试或调整问题。"
            state.is_complete = True
            await self._emit_text(session_id, state.final_answer, len(state.steps))
            await self._emit_turn_end(session_id, finish_reason)
            return

        try:
            from app.agent.prompts.progressive_rag import render_system_prompt
            prompt = render_system_prompt(
                self._config,
                available_tools=self._tool_registry.list_tools(),
            )
            messages: list[dict] = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": self._last_query},
            ]
            for step in state.steps:
                for record in step.tool_calls:
                    if record.result and record.result.success and record.result.output:
                        messages.append({
                            "role": "user",
                            "content": f"Tool {record.name} returned: {record.result.output}",
                        })
            messages.append({
                "role": "user",
                "content": (
                    "Based only on the observations above, write the complete final "
                    "answer in the user's language. Stop after the answer."
                ),
            })

            builder = _AssistantResponseBuilder(self._event_bus, session_id, len(state.steps))
            kwargs: dict = {"temperature": self._config.temperature}
            if self._config.max_output_tokens is not None:
                kwargs["max_tokens"] = self._config.max_output_tokens
            async for chunk in self._llm.stream_with_tools(messages, [], **kwargs):
                await builder.add(chunk)
            response = await builder.finalize()
            state.final_answer = strip_think_blocks(response.content).strip()
            state.is_complete = True
            await self._emit_turn_end(session_id, finish_reason)
            logger.info("[Agent] synthesized answer from observations")
        except Exception as exc:
            logger.warning("[Agent] answer synthesis failed: %s", exc)
            state.final_answer = "抱歉，已检索到内容但生成答案失败。请重试或调整问题。"
            state.is_complete = True
            await self._emit_text(session_id, state.final_answer, len(state.steps))
            await self._emit_turn_end(session_id, "error")

    async def _execute_tool_calls(
        self,
        tool_calls: list[LLMToolCall],
        step: AgentStep,
        session_id: str,
    ) -> list[tuple[LLMToolCall, ToolResult]]:
        parsed: list[tuple[LLMToolCall, dict]] = []
        for call in tool_calls:
            try:
                args = json.loads(call.arguments) if call.arguments else {}
            except json.JSONDecodeError:
                args = {}
                logger.warning("[Agent] invalid JSON arguments for %s", call.function_name)
            parsed.append((call, args))

        for call, args in parsed:
            await self._event_bus.emit(AgentEvent(
                type=EventType.TOOL_CALL,
                session_id=session_id,
                data={
                    "tool_name": call.function_name,
                    "tool_call_id": call.id,
                    "arguments": args,
                    "iteration": step.iteration,
                },
            ))

        if self._config.parallel_tool_calls and len(parsed) > 1:
            outcomes = await asyncio.gather(*(
                self._execute_single_tool(call, args) for call, args in parsed
            ))
        else:
            outcomes = [await self._execute_single_tool(call, args) for call, args in parsed]

        results: list[tuple[LLMToolCall, ToolResult]] = []
        for (call, args), (result, duration_ms) in zip(parsed, outcomes):
            step.tool_calls.append(ToolCallRecord(
                id=call.id,
                name=call.function_name,
                args=args,
                result=result,
                duration_ms=duration_ms,
            ))
            event_data = {
                "tool_call_id": call.id,
                "tool_name": call.function_name,
                "success": result.success,
                "duration_ms": duration_ms,
                "iteration": step.iteration,
                "files": (result.data or {}).get("files", []),
            }
            if result.structured_content is not None:
                event_data["structured_content"] = result.structured_content
            await self._event_bus.emit(AgentEvent(
                type=EventType.TOOL_RESULT,
                session_id=session_id,
                data=event_data,
            ))
            results.append((call, result))

        return results

    async def _execute_single_tool(
        self, call: LLMToolCall, args: dict
    ) -> tuple[ToolResult, int]:
        started = time.time()
        try:
            result = await self._tool_registry.execute(call.function_name, args, ctx=self._tool_context)
        except Exception as exc:
            logger.exception("[Agent] tool %s failed", call.function_name)
            result = ToolResult(success=False, error=str(exc))
        return result, int((time.time() - started) * 1000)

    async def _emit_token_usage(self, messages: list[dict], usage: TokenUsage | None) -> None:
        await self._event_bus.emit(AgentEvent(
            type=EventType.TOKEN_USAGE,
            session_id="",
            data={
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "total_tokens": usage.total_tokens if usage else 0,
                "max_context_tokens": self._config.max_context_tokens,
                "current_context_tokens": self._usage_tracker.estimate_current_tokens(messages),
            },
        ))

    async def _emit_text(self, session_id: str, text: str, iteration: int) -> None:
        await self._event_bus.emit(AgentEvent(
            type=EventType.TEXT_DELTA,
            session_id=session_id,
            data={"content": text, "iteration": iteration},
        ))

    async def _emit_turn_end(self, session_id: str, finish_reason: str) -> None:
        await self._event_bus.emit(AgentEvent(
            type=EventType.TURN_END,
            session_id=session_id,
            data={"finish_reason": finish_reason or "stop"},
        ))

    async def _query_kb_names(self, kb_ids: list[str]) -> list[str]:
        from sqlalchemy import select
        from app.schema.db import KnowledgeBase
        from app.storage.database import async_session

        async with async_session() as session:
            rows = await session.execute(
                select(KnowledgeBase.id, KnowledgeBase.name).where(KnowledgeBase.id.in_(kb_ids))
            )
            names = dict(rows.all())
        return [names.get(kb_id, kb_id) for kb_id in kb_ids]
