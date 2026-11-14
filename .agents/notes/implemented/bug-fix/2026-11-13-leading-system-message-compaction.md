# Agent Note: Leading system message after context compaction

Status: implemented

## Problem

Memory consolidation represented the compacted history as a second `system`
message after the original system prompt. OpenAI-compatible providers such as
vLLM require `system` to appear only at the beginning of the conversation, so the
next model call failed with `System message must be at the beginning.` The same
shape had already been persisted in some agent compaction checkpoints, meaning a
consolidator-only fix would not repair existing long-running sessions.

## Decision

All system messages are collapsed into one leading system message. The compaction
summary is appended to the original system prompt, preserving the summary's
priority while keeping one `system` role at index zero. The normalizer runs both
when consolidation constructs its result and immediately before each Agent model
call, so legacy checkpoints are repaired on their next turn.

## Alternatives considered

**Use the summary as a `user` message.** Rejected because historical model output
would gain new user-level instruction authority and could conflict with the current
request.

**Fix only the consolidator output.** Rejected because checkpoints written by the
previous behavior can still contain a second system message after the original
prompt is restored.

**Normalize inside the vLLM adapter.** Rejected because the leading-system contract
is shared by OpenAI-compatible providers. A provider-specific workaround would need
to be repeated and would leave other provider paths exposed.

## Consequences

Compacted conversations now keep the summary and target the provider contract. The
leading system prompt grows by the summary text, while all non-system message order
is preserved. If multiple system messages are present, content from later system
messages is appended to the first and their separate message metadata is discarded.
No public API or SSE event contract changes.

## Testing

`backend/tests/test_agent_harness_p0_p3.py` verifies that consolidation produces a
single leading system message, retains summary content, and preserves the current
turn. It also verifies normalization of legacy duplicate-system history without
mutating the input, and that the Agent repairs duplicate-system history before
calling the provider.
