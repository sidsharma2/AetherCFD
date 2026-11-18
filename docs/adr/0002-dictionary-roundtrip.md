# ADR 0002 — Lossless custom dictionary parser; foamDictionary validates, never writes

Status: accepted (2026-10-07)

## Context
GUI forms must edit OpenFOAM dictionaries without destroying comments, alignment, one-line packing,
`#include`/`#includeFunc`/`#calc`/`#codeStream`, `$macro` inheritance or regex keys, and must keep
working while the raw editor holds half-typed text. `foamDictionary -set` rewrites the whole file and
drops comments (documented in cfd_agents `case/instrumentation.py`). foamlib edits in place but has no
spans/trivia for editor sync, no error recovery, does not support `#codeStream`, and pulls numpy.

## Decision
`aethercfd.foamdict`: lossless tokenizer (`"".join(tokens) == source` for any input, binary payloads
skipped by their declared length) → CST with spans and comment trivia, error-recovering → semantic
view (keyword paths, `$var` scoping, include graph, regex-key matching, value provenance
`literal | inherited | include | computed`) → minimal `TextEdit` list for set/insert/remove that keeps
the file's own style. Non-literal values are read-only in forms ("edit at source").
`foamDictionary -expand` (via the backend, in the case's fork) is the authoritative *validator* only.
foamlib is a dev-only oracle on the real-dictionary corpus.

## Consequences
We own a parser and must keep it syntax-only; evaluation is always delegated to OpenFOAM.
Byte-exact round-trip over every dictionary in a local case corpus (`AETHERCFD_CORPUS`) is a release gate.
