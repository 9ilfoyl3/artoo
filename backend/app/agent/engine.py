"""DSH-style ReAct engine.

A model response is a typed assistant message: reasoning, text, and tool
calls arrive as separate channels. The loop terminates on a natural text
response; tool calls are executed and fed back as observations. There is no
answer-carrying tool.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, replace

from app.agent.config import AgentConfig
from app.agent.content_classifier import ChannelFragment, PlainContentClassifier
from app.agent.events import AgentEvent, EventBus, EventType
from app.agent.harness import RepeatToolTracker
from app.agent.json_repair import parse_tool_arguments
from app.agent.memory.compress import compress_context, prune_context
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
_MAX_FEEDBACK_RETRIES = 2
_TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
_RETRYABLE_FEEDBACK_STATUSES = {"rejected", "needs_evidence", "retryable_error"}
_STUCK_LOOP_THRESHOLD = 3
_KB_TOOL_NAMES = {"search_knowledge"}
_KB_REDACTION_MARKER = "[Previous retrieval omitted — perform a fresh search.]"
_TURN_CONTRACT_OPEN = "<response_contract>"
_TURN_CONTRACT_CLOSE = "</response_contract>"


def _message_cursor(message: dict) -> str:
    """Stable signature for one model-visible message."""
    serialized = json.dumps(
        message,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _find_current_turn_start(messages: list[dict]) -> int:
    """Return the current turn's marker-bearing user message."""
    for index in range(len(messages) - 1, 0, -1):
        message = messages[index]
        if (
            message.get("role") == "user"
            and _TURN_CONTRACT_OPEN in (message.get("content") or "")
        ):
            return index
    return len(messages) - 1


def _tool_feedback_notice(
    structured_content: dict | None,
    *,
    retry_budget: int | None = None,
    exhausted: bool = False,
) -> str:
    """Render generic structured feedback as a bounded model-visible notice."""
    if not structured_content:
        return ""
    feedback = structured_content.get("feedback")
    if not isinstance(feedback, dict):
        return ""
    lines = [
        "[Tool feedback]",
        f"status={feedback.get('status', 'unknown')}",
    ]
    if feedback.get("code"):
        lines.append(f"code={feedback['code']}")
    if feedback.get("message"):
        lines.append(f"message={feedback['message']}")
    if feedback.get("recoverable") is not None:
        lines.append(f"recoverable={str(bool(feedback['recoverable'])).lower()}")
    actions = feedback.get("next_actions")
    if isinstance(actions, list) and actions:
        lines.append("next_actions=" + " | ".join(str(action) for action in actions))
    if exhausted:
        lines.append(
            "retry_policy=budget_exhausted; do not retry this tool/status again "
            "in this turn without a materially different approach"
        )
    elif retry_budget is not None:
        lines.append(f"retry_policy=recoverable; retries_remaining={retry_budget}")
    elif feedback.get("recoverable") is False:
        lines.append("retry_policy=do_not_retry")
    return "\n".join(lines)


def _feedback_retry_key(
    tool_name: str,
    structured_content: dict | None,
) -> tuple[str, str] | None:
    """Return a bounded-retry key for recoverable structured feedback."""
    if not structured_content:
        return None
    feedback = structured_content.get("feedback")
    if not isinstance(feedback, dict):
        return None
    if feedback.get("recoverable") is not True:
        return None
    if feedback.get("status") not in _RETRYABLE_FEEDBACK_STATUSES:
        return None
    stable_code = feedback.get("code") or feedback.get("status")
    return tool_name, str(stable_code)


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
        self._language_directive = ""
        self._clock_context = ""
        self._checkpoint_needed = False
        self._checkpoint_messages: list[dict] | None = None

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

        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        history = llm_context or []
        checkpoint_history = await self._load_checkpoint_history(
            session_id,
            system_prompt,
            history,
        )
        if checkpoint_history is not None:
            history = checkpoint_history
        if history:
            messages.extend(_redact_history_kb_results(history))

        try:
            from app.agent.prompts.progressive_rag import build_language_directive
            from app.agent.prompts.runtime_context import render_clock_context
            self._language_directive = build_language_directive(query, llm_context)
            self._clock_context = render_clock_context(self._config.timezone_name)
        except Exception as exc:
            self._language_directive = ""
            self._clock_context = ""
            logger.warning("[Agent] failed to build language directive: %s", exc)

        user_content = query
        turn_context = "\n\n".join(
            part for part in (self._language_directive, self._clock_context) if part
        )
        if turn_context:
            user_content = (
                f"{query}\n\n{_TURN_CONTRACT_OPEN}\n"
                f"{turn_context}\n"
                f"{_TURN_CONTRACT_CLOSE}"
            )
        messages.append({"role": "user", "content": user_content})
        self._last_query = query
        self._checkpoint_needed = False

        tools = self._tool_registry.get_function_definitions()
        await self._execute_loop(state, messages, tools, session_id)
        if state.final_answer:
            if (
                not messages
                or messages[-1].get("role") != "assistant"
                or (messages[-1].get("content") or "") != state.final_answer
            ):
                messages.append(
                    {"role": "assistant", "content": state.final_answer}
                )
        self._checkpoint_messages = messages
        if self._checkpoint_needed:
            await self._save_checkpoint(
                session_id=session_id,
                system_prompt=system_prompt,
                messages=messages,
            )
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
        repeat_tracker = (
            RepeatToolTracker() if self._config.repeat_tool_reminder_enabled else None
        )
        inventory_reminder_sent = False
        feedback_retry_counts: dict[tuple[str, str], int] = {}
        checkpoint_needed = False

        while state.current_round < self._config.max_iterations:
            iteration = state.current_round
            logger.info("[Agent][Round-%d/%d] starting", iteration + 1, self._config.max_iterations)

            current_tokens = self._usage_tracker.estimate_current_tokens(messages)
            if self._config.context_pruning_enabled:
                messages, pruned_count = prune_context(
                    messages,
                    self._config.max_context_tokens,
                    current_tokens,
                    tail_start_index=_find_current_turn_start(messages),
                    threshold_ratio=self._config.context_prune_threshold,
                    head_chars=self._config.context_prune_head_chars,
                    tail_chars=self._config.context_prune_tail_chars,
                )
                if pruned_count:
                    current_tokens = self._usage_tracker.estimate_current_tokens(messages)
                    checkpoint_needed = True
            if self._consolidator.should_consolidate(current_tokens):
                before_consolidation = len(messages)
                messages = await self._consolidator.consolidate(messages)
                current_tokens = self._usage_tracker.estimate_current_tokens(messages)
                checkpoint_needed = checkpoint_needed or len(messages) != before_consolidation
            before_compression = len(messages)
            messages = compress_context(
                messages, self._token_estimator, self._config.max_context_tokens, current_tokens,
            )
            checkpoint_needed = checkpoint_needed or len(messages) != before_compression
            self._checkpoint_needed = checkpoint_needed
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
                self._usage_tracker.update(response.usage, sent_count, messages)
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
            tool_results = await self._execute_tool_calls(
                response.tool_calls,
                step,
                session_id,
                repeat_tracker=repeat_tracker,
                finish_reason=response.finish_reason,
            )
            rewrite_reminder_added = False
            for call, result, reminder in tool_results:
                tool_content = (
                    result.output
                    if result.success
                    else (result.error or "Tool execution failed.")
                )
                retry_key = _feedback_retry_key(
                    call.function_name,
                    result.structured_content,
                )
                retry_budget: int | None = None
                exhausted = False
                if retry_key is not None:
                    attempts = feedback_retry_counts.get(retry_key, 0)
                    if attempts < _MAX_FEEDBACK_RETRIES:
                        retry_budget = _MAX_FEEDBACK_RETRIES - attempts
                        feedback_retry_counts[retry_key] = attempts + 1
                    else:
                        exhausted = True
                feedback_notice = _tool_feedback_notice(
                    result.structured_content,
                    retry_budget=retry_budget,
                    exhausted=exhausted,
                )
                if feedback_notice:
                    tool_content = f"{tool_content}\n\n{feedback_notice}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": tool_content,
                })
                if reminder:
                    messages.append({"role": "user", "content": reminder})
                if (
                    not rewrite_reminder_added
                    and result.data
                    and result.data.get("suggest_rewrite")
                ):
                    if (
                        state.query_rewrite_attempts
                        < self._config.query_rewrite_max_attempts
                    ):
                        messages.append({
                            "role": "user",
                            "content": (
                                "The latest retrieval returned no new evidence. "
                                "Reformulate the query once with different entities, "
                                "constraints, or wording before deciding to answer."
                            ),
                        })
                        state.query_rewrite_attempts += 1
                        rewrite_reminder_added = True
                    elif (
                        not inventory_reminder_sent
                        and "list_documents"
                        in self._tool_registry.list_tools()
                    ):
                        messages.append({
                            "role": "user",
                            "content": (
                                "Retrieval still has no new evidence. Call "
                                "list_documents to verify the authorized "
                                "document scope and available doc_ids before issuing "
                                "more broad searches."
                            ),
                        })
                        inventory_reminder_sent = True
            state.steps.append(step)
            state.current_round += 1

        logger.warning("[Agent] max iterations reached (%d)", self._config.max_iterations)
        self._checkpoint_needed = checkpoint_needed
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
                if self._config.reasoning_effort is not None:
                    call_kwargs["reasoning_effort"] = self._config.reasoning_effort

                stream = self._llm.stream_with_tools(messages, tools, **call_kwargs)
                iterator = stream.__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            anext(iterator),
                            timeout=max(0.001, float(self._config.llm_stall_timeout)),
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError as exc:
                        close = getattr(iterator, "aclose", None)
                        if close is not None:
                            await close()
                        raise RuntimeError(
                            "LLM stream stalled: no output for "
                            f"{self._config.llm_stall_timeout}s"
                        ) from exc
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
                    or "stream stalled" in message.lower()
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
                include_clock=False,
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
                    "answer in the required language. Stop after the answer."
                    + (
                        "\n\n" + "\n\n".join(
                            part
                            for part in (
                                self._language_directive,
                                self._clock_context,
                            )
                            if part
                        )
                        if self._language_directive or self._clock_context
                        else ""
                    )
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
        *,
        repeat_tracker: RepeatToolTracker | None = None,
        finish_reason: str = "",
    ) -> list[tuple[LLMToolCall, ToolResult, str | None]]:
        parsed: list[tuple[LLMToolCall, dict, str | None]] = []
        for call in tool_calls:
            parse_result = parse_tool_arguments(call.arguments or "{}")
            args = parse_result.value if isinstance(parse_result.value, dict) else {}
            parse_error: str | None = None
            if finish_reason == "length":
                parse_error = (
                    "Tool arguments were truncated by the provider output limit. "
                    "The tool was not executed; retry with a smaller call."
                )
            elif not parse_result.ok:
                parse_error = (
                    "Invalid tool arguments after bounded JSON repair: "
                    f"{parse_result.error or 'unknown parse error'}"
                )
            elif parse_result.truncated:
                parse_error = (
                    "Tool arguments appear incomplete after JSON repair. "
                    "The tool was not executed; retry with a smaller call."
                )
            if parse_error:
                logger.warning(
                    "[Agent] rejected tool arguments for %s: %s",
                    call.function_name,
                    parse_error,
                )
            parsed.append((call, args, parse_error))

        for call, args, _parse_error in parsed:
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

        records: list[ToolCallRecord | None] = [None for _ in parsed]
        results: list[tuple[LLMToolCall, ToolResult, str | None] | None] = [
            None for _ in parsed
        ]

        async def record_outcome(
            index: int,
            call: LLMToolCall,
            args: dict,
            result: ToolResult,
            duration_ms: int,
        ) -> None:
            records[index] = ToolCallRecord(
                id=call.id,
                name=call.function_name,
                args=args,
                result=result,
                duration_ms=duration_ms,
            )
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
            retrieval_audit = (result.data or {}).get("retrieval_audit")
            if retrieval_audit is not None:
                event_data["retrieval_audit"] = retrieval_audit
            await self._event_bus.emit(AgentEvent(
                type=EventType.TOOL_RESULT,
                session_id=session_id,
                data=event_data,
            ))
            reminder = (
                repeat_tracker.observe(call.function_name, args)
                if repeat_tracker is not None
                else None
            )
            results[index] = (call, result, reminder)

        max_concurrency = max(
            1, int(getattr(self._config, "tool_call_max_concurrency", 1) or 1)
        )
        semaphore = asyncio.Semaphore(max_concurrency)

        async def execute_one(
            index: int,
            call: LLMToolCall,
            args: dict,
            parse_error: str | None,
        ) -> tuple[int, LLMToolCall, dict, ToolResult, int]:
            if parse_error:
                return (
                    index,
                    call,
                    args,
                    ToolResult(success=False, error=parse_error),
                    0,
                )
            async with semaphore:
                result, duration_ms = await self._execute_single_tool(
                    call,
                    args,
                    step=step,
                    session_id=session_id,
                )
            return index, call, args, result, duration_ms

        async def execute_parallel_group(
            group: list[tuple[int, LLMToolCall, dict, str | None]],
        ) -> None:
            tasks = [
                asyncio.create_task(execute_one(index, call, args, parse_error))
                for index, call, args, parse_error in group
            ]
            try:
                for completed in asyncio.as_completed(tasks):
                    index, call, args, result, duration_ms = await completed
                    await record_outcome(index, call, args, result, duration_ms)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        index = 0
        while index < len(parsed):
            call, args, parse_error = parsed[index]
            if (
                parse_error
                or not self._config.parallel_tool_calls
                or not self._tool_registry.is_read_only(call.function_name)
            ):
                result, duration_ms = await self._execute_single_tool(
                    call,
                    args,
                    step=step,
                    session_id=session_id,
                ) if not parse_error else (
                    ToolResult(success=False, error=parse_error),
                    0,
                )
                await record_outcome(index, call, args, result, duration_ms)
                index += 1
                continue

            group: list[tuple[int, LLMToolCall, dict, str | None]] = []
            while index < len(parsed):
                next_call, next_args, next_error = parsed[index]
                if (
                    next_error
                    or not self._tool_registry.is_read_only(next_call.function_name)
                ):
                    break
                group.append((index, next_call, next_args, next_error))
                index += 1

            if len(group) > 1:
                logger.info(
                    "[Agent] executing %d read-only tool calls concurrently "
                    "(max_concurrency=%d)",
                    len(group),
                    max_concurrency,
                )
                await execute_parallel_group(group)
            else:
                group_index, group_call, group_args, group_error = group[0]
                (
                    _group_index,
                    _group_call,
                    _group_args,
                    result,
                    duration_ms,
                ) = await execute_one(
                    group_index,
                    group_call,
                    group_args,
                    group_error,
                )
                await record_outcome(
                    group_index,
                    group_call,
                    group_args,
                    result,
                    duration_ms,
                )

        step.tool_calls.extend(record for record in records if record is not None)
        return [item for item in results if item is not None]

    async def _execute_single_tool(
        self,
        call: LLMToolCall,
        args: dict,
        *,
        step: AgentStep,
        session_id: str,
    ) -> tuple[ToolResult, int]:
        started = time.time()

        async def emit_progress(progress: dict) -> None:
            await self._event_bus.emit(AgentEvent(
                type=EventType.TOOL_PROGRESS,
                session_id=session_id,
                data={
                    "tool_call_id": call.id,
                    "tool_name": call.function_name,
                    "iteration": step.iteration,
                    "elapsed_ms": int((time.time() - started) * 1000),
                    **progress,
                },
            ))

        if self._tool_context is None:
            tool_context = ToolContext(progress=emit_progress)
        else:
            tool_context = replace(self._tool_context, progress=emit_progress)

        try:
            result = await self._tool_registry.execute(
                call.function_name,
                args,
                ctx=tool_context,
            )
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

    async def _load_checkpoint_history(
        self,
        session_id: str,
        system_prompt: str,
        incoming_history: list[dict],
    ) -> list[dict] | None:
        """Load a durable compacted prefix when its cursor is still present."""
        if (
            not self._config.compaction_checkpoint_enabled
            or not session_id
            or not incoming_history
        ):
            return None
        from sqlalchemy import select

        from app.schema.db import AgentCompactionCheckpoint
        from app.storage.database import async_session

        prompt_fingerprint = hashlib.sha256(
            system_prompt.encode("utf-8")
        ).hexdigest()
        try:
            async with async_session() as session:
                checkpoint = await session.scalar(
                    select(AgentCompactionCheckpoint).where(
                        AgentCompactionCheckpoint.session_id == session_id
                    )
                )
        except Exception as exc:  # noqa: BLE001 - checkpoint is an optimization
            logger.warning("[Agent] checkpoint load failed: %s", exc)
            return None
        if (
            checkpoint is None
            or checkpoint.prompt_fingerprint != prompt_fingerprint
            or not checkpoint.cursor
            or not isinstance(checkpoint.messages, list)
        ):
            return None

        cursor_index = -1
        for index in range(len(incoming_history) - 1, -1, -1):
            if _message_cursor(incoming_history[index]) == checkpoint.cursor:
                cursor_index = index
                break
        if cursor_index < 0:
            return None

        stored = checkpoint.messages
        if stored and isinstance(stored[0], dict) and stored[0].get("role") == "system":
            stored = stored[1:]
        return [dict(message) for message in stored] + [
            dict(message) for message in incoming_history[cursor_index + 1 :]
        ]

    async def _save_checkpoint(
        self,
        *,
        session_id: str,
        system_prompt: str,
        messages: list[dict],
    ) -> None:
        """Persist the compacted context prefix for later turns."""
        if (
            not self._config.compaction_checkpoint_enabled
            or not session_id
            or not messages
        ):
            return
        if len(messages) == 1:
            return

        from sqlalchemy import select

        from app.schema.db import AgentCompactionCheckpoint
        from app.storage.database import async_session

        prompt_fingerprint = hashlib.sha256(
            system_prompt.encode("utf-8")
        ).hexdigest()
        cursor = _message_cursor(messages[-1])
        token_estimate = self._usage_tracker.estimate_current_tokens(messages)
        try:
            async with async_session() as session:
                checkpoint = await session.scalar(
                    select(AgentCompactionCheckpoint).where(
                        AgentCompactionCheckpoint.session_id == session_id
                    )
                )
                if checkpoint is None:
                    checkpoint = AgentCompactionCheckpoint(
                        id=str(uuid.uuid4()),
                        session_id=session_id,
                        prompt_fingerprint=prompt_fingerprint,
                        cursor=cursor,
                        messages=messages,
                        prompt_tokens=token_estimate,
                    )
                    session.add(checkpoint)
                else:
                    checkpoint.prompt_fingerprint = prompt_fingerprint
                    checkpoint.cursor = cursor
                    checkpoint.messages = messages
                    checkpoint.prompt_tokens = token_estimate
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - checkpoint is an optimization
            logger.warning("[Agent] checkpoint save failed: %s", exc)

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
