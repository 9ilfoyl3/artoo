# Agent Note: Provider-owned stream idle watchdog

Status: implemented

## Problem

The Agent loop applied its stream-stall timeout to typed `StreamChunk` values.
That boundary is semantically below the true provider transport: vLLM accumulates
tool-call deltas internally and SSE comments are skipped, so either form of upstream
activity could leave the Agent waiting without a chunk. A healthy long tool-call
generation was therefore retried and eventually failed as if the provider had gone
silent.

## Decision

Stream idle timing now belongs to each streaming provider. A shared
`iter_with_stream_idle_timeout` helper measures only an outstanding read from the raw
provider line iterator. Every returned line re-arms the deadline, including SSE
comments and tool-call deltas that never become Agent-facing chunks. vLLM and Ollama
both use the helper for streaming and streaming tool calls.

The default idle budget is 300 seconds. `AgentConfig.llm_stall_timeout` is passed as
`stream_idle_timeout`; the new preset key `llm_stream_idle_timeout` takes precedence
while the previous `llm_stall_timeout` key remains supported. The Agent loop no longer
applies a per-chunk timeout, so downstream processing time cannot be mistaken for
provider inactivity.

## Alternatives considered

**Emit synthetic activity chunks from the vLLM adapter.** Rejected as the permanent
design because it changes semantic stream output to carry transport liveness and must
be repeated for every provider. It remains a possible emergency mitigation, not the
timeout owner.

**Keep the Agent watchdog and also add a provider watchdog.** Rejected because the
outer timer would still fire when the provider consumes tool-call deltas without
yielding a chunk.

**Count only `data:` events.** Rejected because proxies may use SSE comments as
keep-alives. The timer belongs to raw transport reads, not one protocol payload shape.

**Apply the idle budget to the whole generation.** Rejected because a long but active
response is healthy. Only a missing upstream read should consume the deadline.

## Consequences

Long tool-call generation no longer triggers a false stall. A genuinely silent
transport still raises `LlmStreamIdleTimeout` with code
`LLM_STREAM_IDLE_TIMEOUT`, the elapsed budget, and the semantic stage last observed.
The timeout now covers vLLM's non-stream fallback request as well.

The provider owns transport liveness while the Agent loop retains orchestration
ownership. The 300-second default is intentionally configurable because local model
prefill latency varies by deployment.

## Testing

`backend/tests/test_llm_stream_idle_watchdog.py` verifies the 300-second default,
tool-call deltas and SSE comments refreshing the deadline, and a genuinely silent
transport producing a typed timeout with `tool_call_generation` stage.
`backend/tests/test_unified_knowledge_agent_core.py` verifies the Agent no longer
applies a semantic-chunk timeout and trusts the provider boundary.
