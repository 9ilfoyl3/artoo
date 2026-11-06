# Agent Note: Per-turn output-language contract

Status: implemented

## Problem

Agent presets could contain a language instruction in `custom_instructions`, but
the engine appended a dynamic language directive after the complete system
prompt. That directive therefore appeared later in the same system message than
the preset text, while all conversation history still appeared after the system
message. A prior turn whose reasoning was English could therefore dominate the
language instruction during tool-call rounds.

Language detection also inspected only the current query. Short follow-ups such
as `continue`, `ok`, `1`, paths, or symbol-only turns fell through to English,
even when the active conversation was Chinese, Japanese, or Korean.

## Decision

The system prompt remains the stable home for core behavior and preset
customization. Preset customization is explicitly limited to role, tone, domain
vocabulary, and workflow; it does not override the response contract, evidence
rules, or tool semantics.

The agent builds one per-turn output contract from the current query and recent
user messages. The current query wins when it carries a detectable language;
ambiguous short turns inherit the most recent user language; with no usable
signal, no language contract is added. The contract is appended to the current
user message as a model-only `<response_contract>` block, after the query and
after prior history. It requires the final answer language and treats concise
reasoning as best-effort: technical identifiers, code, paths, tool names, and
quoted source text may remain in their original language.

The fallback synthesis path receives the same contract. The preset editor and
the prompt-rewrite assistant tell users not to duplicate output-language rules
inside custom instructions.

## Alternatives considered

**Append the dynamic contract after preset customization but keep it in the
system prompt.** Rejected because conversation history would still follow the
contract, allowing an earlier English reasoning turn to dominate the active
turn.

**Rewrite or retry the final answer whenever language detection fails.**
Rejected because it adds another model call, complicates streaming, and makes
deterministic language classification part of every response path.

**Require the chain of thought to use the user's language.** Rejected as a hard
contract because provider reasoning is model-owned, may be replayed verbatim,
and language-forcing can reduce reasoning quality or increase reasoning tokens.
The contract therefore prefers the user's language when practical without
promising control over hidden reasoning.

**Add a separate preset language field.** Rejected for this change because the
existing contract already follows the current user language, and a new
configuration axis would add UI, API, storage, and migration surface without
fixing the ordering and ambiguous-turn defects.

## Consequences

The dynamic language contract now has recency relative to conversation history
and ambiguous follow-ups inherit the active conversation language. The stable
system prompt no longer changes with each turn's language, which keeps the
provider prefix eligible for reuse.

The model can still violate the contract, and reasoning language is not
guaranteed. The final answer remains the only hard language target. No public
API, SSE event, persisted message, or storage format changes.

## Testing

Focused backend tests cover Chinese and short-follow-up language detection,
history inheritance, no forced English for neutral turns, bounded preset
precedence, stable system-prompt placement, replay of native reasoning, and
fallback synthesis. The frontend production build verifies the preset-editor
copy.
