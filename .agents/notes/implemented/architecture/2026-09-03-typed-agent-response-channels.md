# Agent Note: Typed agent response channels and natural-stop ReAct

Status: implemented

English | [中文](2026-09-03-typed-agent-response-channels.zh.md)

## Problem

The Agent protocol previously required the model to submit its visible answer through a `final_answer` tool and its deliberation through a `thinking` tool. Provider adapters extracted those fields from incrementally streamed tool-call JSON, while the UI inferred whether ordinary text was thinking or an answer from the surrounding tool-call state. That design made the answer payload a second wire format inside the tool-call protocol.

The coupling failed in several ways. A truncated tool argument could leave an unparsable or incomplete answer. Weak function-calling models forgot to call `final_answer`, emitted answer-like text early, or repeated the same content in thinking and final answer. Provider-level routers could not distinguish provider reasoning from user-facing text without encoding every model quirk into the frontend. Real `finish_reason` values were also vulnerable to being replaced by generic stream teardown, so a length-truncated response could appear complete. Separate thought and answer UI panels made live streaming and historical replay diverge.

## Decision

**The Agent uses typed model channels and a natural-stop ReAct loop.** The model calls tools when it needs evidence; otherwise, ordinary assistant text is the final answer. There is no answer-carrying tool and no separate thinking tool.

Providers expose three semantic channels: `reasoning`, `content`, and `tool_calls`, together with usage and the upstream `finish_reason`. `ChatResponse` additionally carries `display_reasoning` and `content_channel` so the engine can present one stable view across models with native reasoning and models that use `<think>` markers. `PlainContentClassifier` performs incremental think-tag extraction and buffers unmarked plain content until the loop can classify it from tool-call intent.

The public Agent SSE contract is event-typed:

| Event | Meaning |
|---|---|
| `reasoning_delta` | User-visible reasoning/planning increment. |
| `tool_call` | A model-initiated tool invocation and arguments. |
| `tool_progress` | Bounded progress for a long-running tool, keyed by tool call ID. |
| `tool_result` | Safe execution metadata; raw tool output is not exposed. |
| `text_delta` | User-visible final or fallback answer increment. |
| `token_usage` | Context usage for the current step. |
| `turn_end` | Typed turn completion with `finish_reason`. |
| `complete` | Aggregate step count, real total elapsed time, and first-answer latency. |
| `error` | User-safe failure text. |

The retired `thought` and `final_answer` events and tools are not part of the new contract. Saved legacy agent steps remain readable for old sessions, but new events persist and replay in the same order and shape as live SSE. The frontend renders one ordered transcript instead of separate thinking and answer panels.

Long-running tools may emit `tool_progress` without creating a second transcript
segment. Progress is bounded metadata only. `complete.total_duration_ms` now
measures the full request; `complete.first_answer_ms` preserves the time to the
first `text_delta`, so clients no longer conflate first-answer latency with
total execution time.

On thinking-capable providers, native reasoning from an assistant tool-call turn is replayed as `reasoning_content` on subsequent requests, matching DeepSeek's official multi-turn tool-calling rule. Provider requests preserve the upstream `finish_reason`, and `turn_end.finish_reason` exposes `stop`, `length`, `max_iterations`, `empty`, and `error` as applicable.

This is a breaking change for third-party consumers. `artoo-open-api.md` is the migration contract and documents the event fields, ordering, fallback-answer behavior, and history replay rules.

## Alternatives considered

**Keep `final_answer` and improve its parser.** Rejected. A parser can recover from some partial JSON, but the design still made ordinary answer text illegal, increased token overhead, and forced weak models through two protocol layers. It also left the provider adapter responsible for answer semantics.

**Keep the old events and let the frontend classify provider content.** Rejected. Frontends would need to replicate native-reasoning, `<think>`, fallback-answer, and provider-dialect rules for every client. Historical replay would still need a second inference path.

**Run a v2 endpoint beside the old SSE contract.** Rejected. The old protocol could not express the desired semantics without compatibility branches in the engine and frontend. Artoo's third-party consumers can migrate with the documented event mapping, and carrying both contracts would leave two turn-completion models in production.

## Consequences

The loop now has one termination rule: a model response without tool calls ends the turn, and its text channel is the answer. This removes the most common silent-failure mode where reasoning completed but the answer tool was never invoked. Providers, rather than adapters or clients, own native reasoning; the engine owns model-capability normalization and tool policy; the SSE bridge owns protocol translation; the frontend owns presentation only.

Truncation is explicit through `finish_reason=length` / `turn_end.finish_reason=length`. A missing answer is a loop-control problem handled by retries or fallback synthesis rather than a malformed tool-argument parser. Native reasoning replay preserves multi-turn tool context and avoids a 400 from the DeepSeek official endpoint when a thinking assistant turn is followed by tool observations.

The cost is a breaking API change: third parties must consume `reasoning_delta` and `text_delta`, stop looking for `thought`/`final_answer`, and use `turn_end` rather than a final-answer done marker. The simpler loop also gives the model more freedom over when to stop; loop hygiene such as duplicate-tool suppression and repeated-search guards remains future work.

## Testing

Backend tests cover native reasoning and natural text termination, plain content classified as reasoning when followed by tool calls, cross-chunk `<think>` parsing, typed SSE event names, tool progress, real total versus first-answer timing, legacy history reconstruction, and native reasoning replay with tool calls. The backend suite reported 800 passed and 13 skipped; three fixture-setup errors in `test_kb_list_count_filter.py` remain separate from this decision. Frontend build and tests covered the interleaved transcript rendering path.
