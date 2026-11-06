# Agent Note: List-documents tool-result files

Status: implemented

## Problem

`list_documents` returned the authoritative catalog under `ToolResult.data["documents"]`, so the model could correctly report that a knowledge base contained 13 documents. The Chat UI did not receive those files, however: the engine's `tool_result` SSE payload only reads `ToolResult.data["files"]`, and `list_documents` never populated that key. The model therefore described 13 documents while the rendered "文档清单" tool row received `files: []`.

## Decision

`list_documents` now derives a lightweight `files` list from each returned catalog page. Every item follows the shared UI contract `{id, filename, source}`, including both formal `document` files and `session-file` entries. The richer metadata remains under `documents`; pagination, summary status, and the model-facing XML output are unchanged.

The shared contract stays at the tool boundary. The Agent engine remains generic, and the React client continues to render `tool_result.files` without a `list_documents` special case.

## Alternatives considered

**Teach the frontend to read `documents` directly.** Rejected because the UI already shares one file-reference renderer across retrieval, reading, and listing tools. A tool-specific payload shape would spread catalog details into presentation code and add another persisted-event format.

**Put the full catalog objects in `files`.** Rejected because `files` is the lightweight preview contract consumed by the tool row. Duplicating chunk counts, summary state, and pagination metadata would enlarge every persisted `tool_result` without improving rendering.

**Map `documents` to `files` in the Agent engine.** Rejected because the engine should not know the internal result shape of each built-in tool. The tool owns the conversion from its rich domain result to the shared event contract.

## Consequences

Document-list tool rows now show the same page of filenames that the model received and expose previewable formal and session files. The model-visible tool output and document authorization are unchanged.

The `files` list is page-scoped, matching the `documents` page. A later page is only rendered after the model calls `list_documents` again, which preserves the existing pagination behavior.

## Testing

`backend/tests/test_generic_kb_agent_data_plane.py::test_document_catalog_lists_only_authorized_completed_documents` verifies that an authorized catalog result exposes the shared `{id, filename, source}` file contract. The session-file catalog test continues to cover both supported sources.
