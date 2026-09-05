# Changelog

Notable repository changes are recorded here. A version entry does not imply that a git tag,
package publication, or hosted release exists.

## Unreleased

### Added

No unreleased additions recorded yet.

### Changed

No unreleased changes recorded yet.

### Fixed

No unreleased fixes recorded yet.

## 0.1.0 - Development baseline

0.1.0 is the current repository/package version. No git tag or published release is asserted by
this changelog.

Implemented capabilities, by roadmap phase:

- Package and test foundation: installable `openagentsearch` package, `__version__`, pytest suite,
  PowerShell gate script with lint/format/type/test commands.
- Fetch policies, raw storage and budget: allowlist configuration, robots handling, polite
  rate-limited fetch, raw store with provenance records, page-budget STOP gate.
- Extraction and dedupe: HTML to visible text with title and language, extracted-record store,
  content-hash dedupe that fails loudly on damaged records.
- Chunk, embed, vector and cosine: deterministic overlapping chunker, local Ollama embedding
  client, SQLite vector store with corruption checks, deterministic cosine search that rejects
  non-finite values.
- HTTP API: standard-library server with `/healthz`, `/search` and `/doc/{sha256}`, bounded input
  validation, documented agent API contract and a fuzz sweep.
- MCP search wrapper: minimal stdio JSON-RPC subset exposing one `search` tool over `/search`.
- Synthetic eval and benchmark: frozen synthetic question set, recall@k, reproducible offline
  latency/recall benchmark.
- Contribution sandbox and gate: restricted Python subprocess runner and a fail-closed gate that
  records sandbox results without granting mergeability.
- Post-roadmap: offline indexing pipeline (`index_document` / `index_documents`), an integration
  test proving an indexed document is searchable and fetchable through the API, and storage
  corruption hardening.
