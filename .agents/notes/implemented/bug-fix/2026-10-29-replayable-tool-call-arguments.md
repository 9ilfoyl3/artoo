# Agent Note: Replayable tool-call arguments

Status: implemented

## Problem

The Agent loop rejected malformed or truncated model-generated tool arguments and
returned a model-visible "not executed" observation, but it appended the original
provider fragment to the assistant message unchanged. On the next round, vLLM parsed
historical `assistant.tool_calls[].function.arguments` with `json.loads()` during chat
template rendering and returned HTTP 400. The turn then fell back to answer synthesis
instead of letting the model retry the failed tool call.

The same risk existed when historical `agent_steps` contained a string-form argument
fragment rather than the normalized object normally written by the runtime.

## Decision

Every tool call replayed in assistant history now passes through
`canonicalize_tool_arguments_for_replay`. Valid JSON objects are serialized
canonically. Invalid, truncated, or non-object arguments are replaced with `{}` in the
history message only. The adjacent tool observation still records the original failure
and instructs the model to retry with a smaller call; tool execution remains blocked.

The normalization applies both to the live Agent loop and to reconstruction from
persisted `agent_steps`. Event payloads and `ToolCallRecord.args` retain their existing
normalized-object behavior.

## Alternatives considered

**Drop malformed tool calls and their matching tool observations from model context.**
Rejected because removing only part of a tool-call exchange can leave orphaned tool
messages and creates a more complex second protocol shape. Replacing `arguments` with
`{}` preserves the exact call/result pairing and remains valid for OpenAI-compatible
servers.

**Replay the locally repaired partial object.** Rejected because `truncated=True` means
the model may have lost a value boundary. Showing partial arguments as if they were
accepted could mislead the retry or imply that a side effect occurred.

**Require vLLM to accept malformed historical JSON.** Rejected because the OpenAI tool
protocol requires `function.arguments` to be a JSON string and vLLM's validation is a
reasonable server-side check. Artoo must not emit a request it already knows is invalid.

**Abort the turn immediately after malformed tool arguments.** Rejected because the
failure is often recoverable by asking the model for a smaller or differently shaped
call. The existing bounded Agent loop and tool observation provide that recovery path.

## Consequences

A malformed tool call no longer turns the following model request into a vLLM HTTP 400.
The model sees `{}` as its historical argument value plus an explicit incomplete-call
observation, so it can retry without the platform inventing missing fields.

The history representation is intentionally normalized rather than byte-for-byte equal
to the provider response. This is limited to protocol replay and does not change public
SSE tool-call arguments, persisted tool results, or authorization.

## Testing

`backend/tests/test_unified_knowledge_agent_core.py` covers canonicalization of valid,
truncated, and invalid arguments, live-loop replay after a rejected tool call, and
historical reconstruction from a malformed string argument.
