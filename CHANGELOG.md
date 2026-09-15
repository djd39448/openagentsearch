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
- `openagentsearch.index.manifest`: a new module with the index manifest itself — `ManifestEntry`,
  `ManifestCounts`, `ManifestCorruptionError`, `STATUSES`, and the pure `ensure_manifest_table` /
  `write_manifest_entry` / `read_manifest_counts` / `read_manifest_entry` / `read_manifest_entries`
  functions that operate on a caller-supplied `sqlite3.Connection`.
- `VectorStore.add_many(..., manifest=...)` upserts a manifest row in the same transaction as the
  vector rows it writes; `VectorStore.record_manifest()`, `.manifest_counts()`, `.manifest_entry()`
  and `.manifest_entries()` read and write the manifest table directly. `VectorStore` now creates
  the manifest table (if missing) whenever it opens.
- `make_healthz_route(store)` (`openagentsearch.api.healthz`): an opt-in `/healthz` route that
  reports `{"status": "ok", "index": {"indexed": N, "failed": N, "superseded": N, "refused": N}}`.
  The default `/healthz` registered by `create_server()` is unchanged and stays byte-identical to
  `{"status":"ok"}`.

### Changed

- `index_document()` is now atomic per document: every chunk is embedded before the first
  write, and the rows land in a single transaction, so an embedder or store failure leaves no
  half-indexed document. A document whose chunk ids already exist is refused before any
  embedding call instead of failing on the first duplicate insert. `index_documents()` is
  unchanged in shape: sequential, stops at the first failure, keeps earlier documents.
- `index_document()` / `index_documents()` / `LiveIngester` now take a `source_kind: str = "html"`
  parameter, recorded on the manifest row. `index_document()` also takes an optional
  `indexed_at: float | None = None` (default: `time.time()` at write time). `IndexReport` gains
  `source_kind: str` and `indexed_at: float` (appended at the end of the dataclass, after
  `chunk_ids`). A failure inside `index_document()` (from extraction through the final write) is
  now recorded as a `failed` manifest row before the original exception is re-raised unchanged;
  the "already indexed" refusal is not a failure and still writes nothing.

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
