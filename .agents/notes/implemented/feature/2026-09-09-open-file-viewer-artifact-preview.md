# Agent Note: open-file-viewer artifact preview engine

Status: implemented

## Problem

The Artifact panel maintained one bespoke previewer per file type (PDF iframe,
image tag, plain text, Markdown, CSV) and explicitly did not support Office
documents, email, or archives. Knowledge-base documents and session attachments
in those formats could not be previewed, and each new format required new
panel-side components plus new data-flow branches (text content vs objectURL).

## Decision

The preview capability inside the Artifact panel is unified on
`@open-file-viewer/react` 0.1.44 with `@open-file-viewer/core` 0.1.44 and
`pdfjs-dist` ^4.10.38:

- `OpenFileViewerPreview` follows the official React example: module-level
  plugin registration (image / video / audio / pdf / epub / xps / office / ofd
  / archive / email / text), the pdf.js worker loaded via Vite `?url`,
  `locale: "zh-CN"`, and the built-in toolbar.
- `openFileViewerTheme.css` (scoped to `.artoo-ofv`) remaps the viewer's
  `--ofv-*` palette onto Artoo's theme variables, so colors follow the app
  theme: the search highlight becomes a translucent theme color instead of the
  opaque default that covered glyphs, the PDF backdrop, the Office panel
  backdrop, and the toolbar search input all share the muted surface (so Word
  views match the PDF backdrop instead of the accent color), the download
  fallback card is flattened to a light-bordered card, and failure copy is
  simplified via `pdfPreviewFailedTitle` / `pdfDownload`. The viewer base
  theme stays `light` as a deterministic base; dark surfaces come from Artoo's
  variables.
- Word-like documents (`doc`, `docx`, `docm`, `dot`, `rtf`, `odt`) default to
  `fit: "width"` so pages fill the panel width; other formats keep the viewer
  defaults.
- The preview component is lazy-loaded with `React.lazy`, so the engine chunk
  and worker download only when a file is first previewed.
- `ArtifactPanel` keeps its outer responsibilities (slide-in shell,
  authenticated raw fetch, objectURL lifecycle with revoke, download, close)
  and now always passes the blob objectURL plus the original file name to the
  viewer. The per-type text-extraction branch and the five per-type previewer
  components are removed.
- `PREVIEWABLE_TYPES` expands to the registered preview surface: PDF, common
  images, text/Markdown/CSV, Word/Excel/PowerPoint including legacy and
  OpenDocument formats, RTF, EML/MSG, ZIP, and EPUB. Media, 3D, CAD, and GIS
  plugins stay registered in the viewer but remain gated out of the preview
  button surface.

## Alternatives considered

**Add per-format libraries (mammoth, SheetJS, and friends) to the existing
per-type previewer registry.** Rejected because it multiplies panel-side code
and data-flow branches per format and still lacks a unified container, toolbar,
state model, and fallback path.

**Convert everything to PDF server-side (for example LibreOffice).** Rejected
as the primary path because it adds an operational service and an upload hop
for every preview. The viewer's optional `officePlugin({ convert })` hook keeps
this available as a future enhancement for high-fidelity Office rendering
without changing the integration.

**Keep the custom previewers and add only an Office viewer.** Rejected because
the goal was a single container contract; two preview stacks would keep
inconsistent states, toolbars, and fallback behavior across file types.

## Consequences

Previewable types grow from 7 to 30 without backend changes. The engine ships
as one lazy chunk (about 624 KB, 203 KB gzip in the current build) plus an
on-demand pdf worker, so the initial bundle is unchanged. DOCX/PPTX rendering
is HTML-fidelity rather than pixel-perfect; complex documents may eventually
need the server-side convert hook. Vite externalizes `buffer` for a transitive
dependency of the MSG parser, so `.msg` preview may degrade while `.eml`
(postal-mime) is unaffected. The library is 0.1.x: `@open-file-viewer/react`
pins the exact core version, so both packages must be bumped in lockstep.
`OpenFileViewerPreview` is the single integration point for tuning plugins or
viewer options.

## Testing

`npm test` in `frontend/` passes (34 tests, including new `artifactStore`
previewable-type coverage) and `npm run build` (tsc + vite build) succeeds.
