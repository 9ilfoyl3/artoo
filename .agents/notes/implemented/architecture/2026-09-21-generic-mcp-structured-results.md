# Agent Note: Generic MCP structured result channel

Status: implemented

## Problem

Artoo previously flattened every outbound MCP `tools/call` result into
`ToolResult.output`. Model-facing `content[].text` was delivered to the LLM, but
the standard MCP `structuredContent` value and the `outputSchema` declared by
`tools/list` were discarded. Tool-specific frontends therefore could not render
safe structured cards, and there was no path for live and historical
`tool_result` events to expose the same UI data.

Returning the raw tool text through SSE was not an acceptable fix. External MCP
output is untrusted, may contain prompt injection or oversized data, and was
deliberately excluded from SSE and `agent_steps` persistence.

## Decision

Artoo provides a generic MCP structured result channel without interpreting
business fields.

- `MCPRemoteClient.call_tool` returns a structured `MCPCallResult` containing
  model text, optional `structuredContent`, and the error flag.
- `tools/list` preserves a validated MCP `outputSchema` on each
  `MCPToolWrapper`. The schema is used only for local validation and is never
  injected into the model's function definitions.
- Remote `structuredContent` must use the generic envelope
  `{"kind": "<globally-namespaced-id>", "data": <JSON>}`. `kind` is an opaque
  renderer key such as
  `io.law-agent-lite.legal-provision-search.v1`.
- The envelope may include optional generic `feedback`:
  `{status, code?, message?, recoverable?, next_actions?}`. Status is one of
  `accepted`, `rejected`, `needs_evidence`, `retryable_error`, or `completed`.
  Artoo validates shape and bounds but does not interpret business meaning.
- Artoo validates JSON compatibility, `outputSchema`, a 64 KiB byte limit,
  maximum depth eight, 100 array items, 16 KiB strings, object-key limits, and
  forbidden prototype-pollution keys. Invalid structured data is dropped while
  the model still receives `content[].text`.
- Valid structured data flows through `ToolResult.structured_content`,
  `tool_result.structured_content` SSE events, and the same event stored in
  `agent_steps`. Raw tool text remains excluded from both SSE and persistence.
- Structured feedback is also rendered as a bounded tool observation for the
  model, so recoverable validation feedback can drive the next Agent step.
- Recoverable feedback is bounded per turn. Only `recoverable=true` with status
  `rejected`, `needs_evidence`, or `retryable_error` receives a retry budget;
  the limit is two retries per tool and stable feedback code. Once exhausted,
  the model observes an explicit `budget_exhausted` instruction and must change
  approach or answer with the available evidence. `recoverable=false` is
  rendered as `do_not_retry`.
- The built-in frontend keeps a `kind` to renderer registry. Unknown or invalid
  kinds fall back to a safe plain-JSON card. Tool-specific renderers live in the
  consuming application, not in Artoo core.
- Structured content is not automatically merged into `references`. Citation
  identity remains a separate generic contract.

## Alternatives considered

**Expose raw MCP `content[].text` through `tool_result`.** Rejected. It would
make untrusted third-party text a public and persisted API payload, increase
history storage, and require every client to sanitize prompt-injection and
rendering risks.

**Add Artoo-side parsing for known tools such as
`search_legal_provisions`.** Rejected. It couples the core platform to one
business domain and requires a new backend branch for every renderer.

**Carry a JSON envelope only inside `content[].text`.** Rejected as the primary
contract. It cannot be validated independently from the model projection and
can break when prompts or text shaping change. It remains only a possible
legacy compatibility fallback.

**Automatically convert structured content into `references`.** Rejected for
this change. Structured UI data does not define document/chunk authorization or
citation identity, and silently treating arbitrary MCP data as evidence would
weaken provenance.

**Allow recoverable feedback to retry until the global Agent iteration limit.**
Rejected because a deterministic validation error can otherwise consume the
entire turn. Per-tool/status feedback budgets preserve recovery without turning
the main loop into an unbounded retry driver.

## Consequences

MCP servers that want rich host rendering must opt in by declaring
`outputSchema` and returning `structuredContent`; older servers continue to
work with text-only output. The SSE and `agent_steps` contracts gain an
additive optional `structured_content` field, so existing clients can ignore
it.

Artoo now depends on `jsonschema` for generic remote schema validation. Invalid
or oversized schemas and results are logged without content and degrade to the
existing text-only behavior. The frontend registry must treat structured data
as untrusted, render plain text rather than HTML, and provide a fallback for
unknown `kind` values.

The SSE and persisted `agent_steps` schema gains the additive optional
`structured_content` object. MCP servers that want to return feedback must add
the optional `feedback` object inside that envelope. Older clients can ignore
the field, but consumers that need recovery behavior must handle the documented
status and next-action values. Recoverable feedback can consume at most two
additional calls for the same tool and stable feedback code in one turn; the
global `max_iterations` limit still applies.

## Testing

Backend tests cover output schema preservation, successful structured result
transfer, schema mismatch and size rejection, registry preservation during text
truncation, engine emission, typed SSE serialization, and bounded structured
feedback validation and retry budgets. Frontend tests cover the envelope guard,
renderer registration, summary selection, invalid keys, and unknown-kind
fallback. The frontend production build validates the React type/import
integration.
