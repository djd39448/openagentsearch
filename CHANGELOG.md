# Changelog

Notable repository changes are recorded here. A version entry does not imply that a git tag,
package publication, or hosted release exists.

## Unreleased

### Added

- `openagentsearch.pipeline.ingest.LiveIngester`: live ingestion of one URL at a time behind the
  allowlist, robots.txt (fetched once per host, fail-closed when unavailable), the page budget and
  the host rate limiter, through `RawStore` provenance, extraction with dedupe, and atomic
  `index_document()`. Redirects are never followed, bodies are size-bounded, and every refusal
  is a typed `IngestReport` outcome. There is still no crawl loop, no link following and no
  production run: it ingests the URLs it is handed.
- `VectorStore.add_many()` writes a batch of chunk rows in one SQLite transaction (all rows
  or none), and `VectorStore.existing_chunk_ids()` reports which chunk ids are already stored.

### Changed

- `index_document()` is now atomic per document: every chunk is embedded before the first
  write, and the rows land in a single transaction, so an embedder or store failure leaves no
  half-indexed document. A document whose chunk ids already exist is refused before any
  embedding call instead of failing on the first duplicate insert. `index_documents()` is
  unchanged in shape: sequential, stops at the first failure, keeps earlier documents.

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
