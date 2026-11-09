# Agent Note: Material tokenizer runtime safety

Status: implemented

## Problem

The [material token estimation API](../feature/2026-10-19-material-token-estimation-api.md)
depends on `tiktoken` loading `cl100k_base` at runtime. The backend image installed
the Python package but did not include the encoding vocabulary, so an offline or
network-isolated deployment fell back to `len(text) // 4`. That ratio is reasonable
for English but underestimates Chinese material by several times.

Two secondary failures made the degradation unreliable. Literal strings such as
`<|endoftext|>` in document content caused `Encoding.encode()` to reject the whole
document, even though those strings are ordinary material text. The resulting
character fallback was reported as `fallback=false` because only initial encoder
load failure was tracked. In addition, process-wide caching kept a fallback
estimator after a transient startup/load failure until the backend restarted.

## Decision

The backend image now downloads `cl100k_base` during the build, copies its vocabulary
into the runtime image, and sets `TIKTOKEN_CACHE_DIR=/opt/tiktoken`. Runtime token
counting therefore does not depend on outbound network access.

Document text is encoded with `disallowed_special=()`, treating special-token-looking
strings as normal content. `TokenEstimator` reports whether each string used the
character fallback, and the materials API aggregates that per-document status into
the response's `fallback` field.

The materials route creates a lightweight estimator per request rather than caching
the wrapper process-wide. `tiktoken.get_encoding()` still caches the loaded BPE
object, so successful requests reuse the vocabulary while a transient load failure
can recover on a later request.

The emergency fallback now counts ASCII at roughly four characters per token and
non-ASCII at roughly one token per character. It remains an approximation, but it no
longer turns Chinese text into a four-times-smaller estimate.

## Alternatives considered

**Keep downloading the vocabulary on first request.** Rejected because offline and
intranet deployments would continue returning the same incorrect Chinese estimate,
with only the `fallback` flag signalling the problem.

**Treat literal special-token strings as tiktoken special tokens.** Rejected because
the document content is user material, not protocol control data. `cl100k_base`'s
single `<|endoftext|>` special token would also undercount the literal text compared
with tokenizing its visible characters.

**Keep process-wide caching of the estimator wrapper.** Rejected because an initial
network failure would be sticky. Retrying the wrapper preserves recovery while the
underlying tiktoken registry continues to cache successful encoder construction.

**Continue using `len(text) // 4` for every fallback.** Rejected because the endpoint
is used for Chinese knowledge-base material, where that heuristic can understate a
180k-character document as roughly 45k tokens rather than approximately 100k or more.

## Consequences

The runtime image grows by one vocabulary file, approximately 1.6 MiB uncompressed
and less than 1 MiB compressed. Offline deployments can now perform deterministic
`cl100k_base` counting without first reaching `openaipublic.blob.core.windows.net`.

The response's `fallback` field now describes the actual work performed for the
request, including character fallback after an unexpected encoding error. The API
still estimates `cl100k_base`, not the tokenizer of an arbitrary downstream model;
model-specific counts remain an explicit limitation of the current contract.

## Testing

`backend/tests/test_token_estimator.py` covers literal special-token content, CJK and
ASCII fallback counts, and the empty-string status. `backend/tests/test_material_token_estimate_api.py`
covers aggregate per-document fallback reporting in addition to the existing scope,
ordering, parent-chunk, authorization, and not-found behavior.

The backend image is built with an empty runtime network and verified to load the
copied vocabulary, return `fallback=false`, count `<|endoftext|>` as seven tokens, and
count a 320-character Chinese sample as 300 tokens without network access.
