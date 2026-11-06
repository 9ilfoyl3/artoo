# Agent Note: Safe LLM retry events and structured failure settlement

Status: implemented

## Problem

Transient LLM failures were retried without any user-visible state. Retry
eligibility depended on substring matching over rendered error messages, and the
loop could retry after reasoning had already been emitted to the client. That
duplicated visible reasoning across attempts. If a later retry failed after tools had
run, the engine called `_synthesize_answer`, hiding the transport failure behind a
second model request.

## Decision

LLM failures are classified into stable facts before retry. `llm_retry` is an Agent
event emitted before every automatic backoff, carrying the failed attempt, next
attempt, maximum attempts, delay, code, stage, and retryable flag. The event is
persisted in `agent_steps` and projected to Agent SSE.

Automatic retry remains bounded to three total attempts with exponential backoff and
jitter. It is allowed only when the failed attempt produced no visible reasoning or
text and no completed tool call. Non-retryable failures, partially visible attempts,
and attempts whose tool calls are already formed fail without another provider call.

Terminal model failures emit a structured `error` event with code, stage, retryable,
and attempts, then `turn_end(finish_reason="error")`. The engine no longer invokes
`_synthesize_answer` on a transport failure. The user-facing error text remains the
saved assistant body, while safe partial output stays represented by its persisted
stream events.

The web client renders retry attempts and terminal failures as separate status rows.
Historical replay reconstructs the same rows from `agent_steps`, so refreshing or
switching sessions does not lose retry history. Older clients may ignore the new
event and optional error fields.

## Alternatives considered

**Retry after partial generation and replace the previous attempt.** Rejected for
now because the public SSE and persisted `agent_steps` contract has no attempt
revision or client-side rollback. Retrying without that mechanism duplicates visible
content.

**Always retry every failure code.** Rejected because authentication, invalid request,
context overflow, and other deterministic failures cannot recover by repeating the
same request.

**Keep substring-based retry classification.** Rejected because provider wording is
not a stable contract and can confuse HTTP status text with unrelated numbers.

**Continue synthesizing an answer after transport failure.** Rejected because a second
model request can fail the same way, hides the real error, and adds cost and latency.
Synthesis remains reserved for deliberate loop termination such as max iterations.

**Emit retry events only in logs.** Rejected because users otherwise see a stalled
assistant message with no explanation, and the frontend cannot distinguish a normal
wait from an automatic retry.

## Consequences

Agent SSE and persisted `agent_steps` gain `llm_retry`; `error` gains optional
structured fields. The frontend renders retry and failure states in stream order.

Retries are safer but less aggressive: a failure after visible output is surfaced for
an explicit user retry instead of being retried automatically. This avoids duplicated
reasoning and accidental repeated side effects.

## Testing

`backend/tests/test_structured_result.py` covers retry-event emission and ordering,
visible-output retry suppression, structured SSE projection, non-retryable HTTP
classification, and no post-tool synthesis after exhausted retries.
`frontend/src/components/chat/MessageBubble.test.tsx` verifies retry and terminal error
rows render with attempt and stable failure metadata.
