# Agent Note: Deterministic Agent harness P0-P3 policies

Status: implemented

## Problem

The ReAct loop accepted repeated exact tool calls, retrieval tools accepted
duplicate or unbounded query lists, low-quality searches had no bounded rewrite
path, historical tool output reached compaction before deterministic pruning,
and the Agent path bypassed the existing retrieval cache. Dynamic clock text
also remained in the stable system prompt, preventing prefix reuse across turns.

These are harness problems rather than model-capability problems. Leaving them
inside prompt prose makes behavior depend on model compliance and increases
token use without enforcing progress.

## Decision

The Agent harness owns four deterministic policy layers.

P0 loop hygiene and identity:

- Retrieval queries are normalized with Unicode NFKC and whitespace collapse.
- Query lists are deduplicated while preserving first-seen order.
- `knowledge_search` and `grep_chunks` share `seen_chunk_ids` and omit repeated
  chunk bodies from model-visible XML.
- Exact consecutive tool calls are tracked with canonical arguments. Advisory
  reminders fire at 3, 5, and 8 repeats. They never veto a call.

P1 bounded query repair:

- A `knowledge_search` call accepts at most three unique queries.
- Zero results or zero new chunks set `suggest_rewrite` in tool metadata.
- The engine gives the model at most one explicit rewrite opportunity per turn.

P2 context economy and reasoning effort:

- When context pressure reaches the pruning threshold, older tool results are
  reduced deterministically to 4096 head characters, a prune marker, and 1024
  tail characters before LLM summarization is considered.
- The current turn is never pruned by this pass.
- An optional `reasoning_effort` preset value is forwarded only when explicitly
  configured, so unsupported providers retain their previous request shape.

P3 retrieval and prefix caching:

- Formal knowledge-base searches use the existing Redis cache with a key that
  includes query, mode, scope, top-k, tenant, and an optional config version.
- Failed or route-degraded results are not cached.
- Session-file sources are never cached until they have a dedicated generation
  key, because their contents can change without a KB-level invalidation.
- The dynamic clock is appended to the current user turn instead of the stable
  system prompt, matching the per-turn output-language contract.

All policies are represented by `AgentConfig` flags and default to enabled
except the optional reasoning effort. This allows deterministic A/B runs without
changing public API or SSE payloads.

## Alternatives considered

**Hard-stop repeated tools.** Rejected because some repeated calls are
legitimate, and the event is better handled as model-visible advice before a
larger guard is justified.

**Always summarize tool output.** Rejected because an extra model call at every
step is expensive and rewrites reusable history earlier than necessary.
Deterministic pruning runs only under measured pressure.

**Cache session-file retrieval under the same key as formal KB retrieval.**
Rejected because session uploads mutate independently of KB invalidation and
would produce stale answers.

**Add a semantic retrieval cache.** Rejected because similar queries can carry
different filters, intent, or point-in-time requirements. Only normalized exact
keys are safe at this stage.

**Keep time in the stable system prompt.** Rejected because every turn changes
the prefix. The clock is already a per-turn fact and belongs after reusable
history.

## Consequences

The Agent loop spends fewer calls on exact repetition and duplicate queries,
prunes tool-result bulk before considering an LLM summary, and can reuse cached
formal-KB results without caching mutable session data. System prompt prefixes
no longer change solely because time or output language changed.

The cache inherits the existing Redis TTL and KB invalidation lifecycle.
Retrieval-config changes without a supplied config version can remain cached
until TTL expiry. Reasoning language remains best-effort, and the repeat guard
is advisory rather than a hard execution limit. No Open API, SSE, persisted
event, or storage schema changes.

## Testing

`backend/tests/test_agent_harness_p0_p3.py` covers query normalization,
canonical signatures, repeat reminders, context pruning, exact formal-KB cache
hits, cache-key separation, and session-source cache exclusion.

`backend/benchmarks/agent_harness_ab.py` reports deterministic A/B metrics. The
current fixture result is 6 to 3 queries after deduplication, a first repeat
reminder after 3 calls, 20,000 to 5,159 characters for one pruned historical
tool result, and 2 to 1 retrievals for two identical cached searches.
