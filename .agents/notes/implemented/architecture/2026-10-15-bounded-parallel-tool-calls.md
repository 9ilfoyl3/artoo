# Agent Note: Bounded parallel tool calls

Status: implemented

## Problem

`AgentConfig.parallel_tool_calls` existed but the production chat runtime never
passed a preset value into it, so every normal Agent request executed tool calls
serially. The apparent parallel start came from emitting all `tool_call` events
before execution; `tool_result` events still arrived one at a time.

The dormant parallel branch was also broken. It used the objects yielded by
plain `asyncio.as_completed()` iteration as keys into a metadata dictionary, but
plain iteration yields result coroutines rather than the original tasks. Turning
the flag on therefore raised `KeyError` instead of executing tools.

## Decision

Parallel tool execution is a preset capability with a platform-owned concurrency
cap.

`preset-smart-reasoning` explicitly enables `parallel_tool_calls`. Custom and
legacy presets default to off when the key is absent, so upgrading does not
silently change execution order for side-effectful tool sets. The Agent preset
editor exposes the boolean switch and preserves it when a preset is edited.

`AGENT_TOOL_CALL_MAX_CONCURRENCY` defaults to 4. The runtime passes it into
`AgentConfig.tool_call_max_concurrency`, clamps it to at least 1, and keeps it
outside tenant-editable preset JSON so a preset cannot raise platform resource
limits.

When parallel execution is enabled and a model response contains multiple tool
calls, the engine creates one wrapper task per call and limits active wrapper
tasks with an `asyncio.Semaphore`. Each wrapper returns its original index and
metadata with the tool result, which removes the invalid task-to-metadata map.

Tool results continue to stream as `tool_result` events in completion order.
`AgentState.steps[*].tool_calls` and the model-visible `tool` messages remain in
the model's original tool-call order, so persistence and protocol replay are
deterministic even when completion order differs.

Tool exceptions remain isolated per call and become failed `ToolResult`
observations. Cancellation of the Agent turn cancels unfinished tool tasks and
waits for their cleanup. A concurrency value of 1 is valid and behaves as a
bounded serial execution.

The decision uses Agent-runtime concurrency only. It does not add or change a
provider request parameter, SSE payload field, or persisted event schema.

## Alternatives considered

**Enable parallel execution for every preset.** Rejected because the previous
runtime behavior was always serial. Silent activation could reorder dependent
or side-effectful MCP tools in custom presets. Presets opt in explicitly, while
the built-in smart-reasoning preset ships enabled.

**Let each preset choose its concurrency limit.** Rejected because preset JSON
is tenant-editable. A tenant could otherwise raise platform-wide task, thread,
retrieval, or remote-service pressure. The platform setting owns the cap.

**Use `asyncio.gather` and record all results after completion.** Rejected
because it keeps actual overlap but loses completion-order `tool_result`
streaming and progress visibility.

**Map completion results by scanning completed tasks.** Rejected because it
would duplicate bookkeeping and remain sensitive to Python's
`asyncio.as_completed()` yield semantics. Wrapper tasks carry metadata with the
result by construction.

**Keep result records in completion order.** Rejected because model replay,
history reconstruction, and debugging should not depend on network timing. Only
public completion events follow completion order; state and protocol messages
stay in source order.

## Consequences

The built-in smart-reasoning Agent now executes independent tool calls
concurrently, up to the platform cap. Custom preset owners can enable or disable
the behavior through the preset form. Existing presets without the key keep
serial execution.

Parallelism is cooperative asyncio concurrency, suitable for retrieval, model,
database, and remote MCP I/O. CPU-bound or synchronous blocking work can still
block the event loop and must move to a worker thread or process.

Per-tool side effects and ordering assumptions remain an operator concern.
Presets that expose dependent or non-idempotent tools should leave parallel
execution disabled.

The public SSE event names and fields are unchanged. Completion order may now
differ from call order when parallel execution is enabled, which clients already
support because every result is keyed by `tool_call_id`.

## Testing

`backend/tests/test_structured_result.py` covers the serial regression,
overlapping slow/fast calls, completion-order `tool_result` events,
source-order persisted records, per-call exception isolation, the configured
concurrency cap, runtime preset opt-in, and normalization of invalid
concurrency values. `backend/tests/test_agent_config_default.py` verifies that
the built-in smart-reasoning preset enables the capability. The frontend
production build verifies the preset editor contract. The targeted backend
tests passed 26/26, the full backend suite passed 818 tests with 3 pre-existing
`client` fixture setup errors in `test_kb_list_count_filter.py`, and the Python
3.12 wrapper-task smoke test confirmed completion-order results.
