# Agent Note: Visible tool-call arguments

Status: implemented

## Problem

The tool-call argument path was intact: the provider accumulated streaming
`arguments`, the Agent engine parsed them into an object, `tool_call.arguments` was
carried through SSE and persisted `agent_steps`, and the Chat client stored it as
`toolArgs`. The expanded tool row did not render that field, however, so users could
not distinguish a legitimate empty object from lost arguments. This was especially
ambiguous for `list_documents`, whose `page`, `page_size`, `status`, `keyword`, and
`kb_ids` parameters are all optional.

## Decision

An expanded tool-call row now displays a "调用参数" section containing the normalized
argument object as pretty-printed JSON, including `{}` when the model supplied no
arguments. The current and legacy transcript renderers share the same
`ToolCallArguments` component.

The UI shows the parsed object that the runtime actually executed, not the provider's
raw JSON fragment. Invalid or truncated raw fragments remain represented by the
existing rejection result and normalized replay behavior; the event and persistence
contracts are unchanged.

## Alternatives considered

**Show arguments inline in the collapsed row for every tool call.** Rejected because
searches, document IDs, and larger MCP payloads would add noise and crowd out the tool
name, status, and file result summary.

**Show the provider's raw argument string.** Rejected because the provider fragment may
be malformed before local parsing, while the normalized object is the call that was
actually validated and executed. Diagnosing transport corruption should remain a
logging concern rather than mixing two representations in the normal transcript.

**Require users to inspect `agent_steps` or server logs.** Rejected because the
information was already present in the browser and persisted for replay. A collapsible
diagnostic view is enough and does not require another endpoint or debug mode.

## Consequences

Users can verify exactly which object the Agent executed, including explicit empty
arguments for optional-parameter tools. The information remains behind the existing
row expansion, so normal transcript density is unchanged.

Tool arguments can include user-provided query text or document identifiers. They are
shown only to the same authenticated transcript viewer who can already access the
conversation and its persisted `agent_steps`; no additional data is exposed through the
SSE contract.

## Testing

`backend/tests/test_unified_knowledge_agent_core.py` verifies that a non-empty argument
object survives the engine event, SSE conversion, persisted step record, and assistant
history replay. `frontend/src/components/chat/MessageBubble.test.tsx` verifies expanded
argument JSON and the explicit `{}` rendering for omitted optional arguments.
