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
- Artoo validates JSON compatibility, `outputSchema`, a 64 KiB byte limit,
  maximum depth eight, 100 array items, 16 KiB strings, object-key limits, and
  forbidden prototype-pollution keys. Invalid structured data is dropped while
  the model still receives `content[].text`.
- Valid structured data flows through `ToolResult.structured_content`,
  `tool_result.structured_content` SSE events, and the same event stored in
  `agent_steps`. Raw tool text remains excluded from both SSE and persistence.
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

## Testing

Backend tests cover output schema preservation, successful structured result
transfer, schema mismatch and size rejection, registry preservation during text
truncation, engine emission, and typed SSE serialization. Frontend tests cover
the envelope guard, renderer registration, summary selection, invalid keys, and
unknown-kind fallback. The frontend production build validates the React
type/import integration.
