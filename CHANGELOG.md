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
- `python -m openagentsearch.api.server` (`openagentsearch.api.cli`): a server CLI that wires a
  `VectorStore`, an embedder, and the `/healthz` / `/search` / `/doc/{sha256}` routes into a real
  `ThreadingHTTPServer` process. Flags: `--db PATH` (required), `--embedder {ollama,keyword}`
  (required), `--host` (default `127.0.0.1`), `--port` (default `8080`; `0` for an ephemeral
  port), `--root DIR` (optional; mounts `/doc/` when given), `--dimension` (default `256` for
  `keyword`, `768` for `ollama`), `--ollama-url` and `--ollama-model`. Prints one compact JSON
  startup line to stdout (`{"listening": ..., "db": ..., "embedder": ..., "doc_route": ...}`) and
  stops cleanly on SIGINT/SIGTERM (SIGBREAK/CTRL_BREAK_EVENT on Windows), printing
  `{"stopped": true}`. A construction failure (for example a `--db` parent directory that does
  not exist) prints a one-line JSON error to stderr and exits 1; argument errors exit 2. A
  non-loopback `--host` prints a warning to stderr before listening rather than being refused.
  `--embedder ollama` is untested in CI: there is no Ollama server in the test environment, so
  only construction (never a live request) is exercised for it.
- `KeywordEmbedder` (`openagentsearch.embed.keyword`): a deterministic, standard-library,
  hashed-keyword text embedder (SHA-256 token hashing into fixed buckets, L2-normalized) for
  tests and demos. It is lexical only, subject to hash collisions, and not comparable to
  `OllamaEmbedClient` vectors. `make_search_route()`'s route now treats an all-zero or empty
  query embedding (for example a tokenless query under `KeywordEmbedder`) as a valid query with
  no matches (`200` with `results: []`) instead of letting `cosine_search()`'s
  "all zeros" `ValueError` propagate.
- `openagentsearch.sources`: four FLOP source adapters sharing one `SourceDoc` document shape and
  one `AdapterStats` accounting shape (`openagentsearch.sources.base`), plus the pure helpers
  `split_markdown_sections()` and `slugify_heading()`:
  - `RoomDirectoryAdapter` (`sources.technocore_rooms`) reads technocore.chat's `rooms.jsonl` room
    *directory* line by line (bounded per-line length, a computed-once file SHA-256 in
    provenance) and renders one deterministic English document per room meeting `min_messages`;
    it never sees message text.
  - `GitHubRepoDocsAdapter` (`sources.github_docs`) reads a fixed list of paths at one pinned
    commit through an injected fetcher, splitting markdown files into per-section documents
    (`split_markdown_sections`) and leaving `.txt`/`.json` files unsplit; `markdown_paths_from_tree`
    turns a GitHub git-tree JSON payload into a sorted list of markdown paths and refuses a
    `truncated` tree.
  - `GitHubIssuesAdapter` (`sources.github_issues`) is a pure reader over already-loaded
    `gh api .../issues` (+ per-issue comments) JSON, skipping pull requests and oversized issues;
    `load_issues_with_gh()` is the runtime loader that shells out to `gh` through an injected
    `runner` (`subprocess.run([...], shell=False, timeout=...)` by default), and
    `_parse_concatenated_json_arrays()` parses `gh api --paginate`'s back-to-back JSON-array pages.
  - `SitePagesAdapter` (`sources.flop_site`) fetches an explicit, operator-supplied list of URLs
    behind a strict HTTPS host allowlist through an injected fetcher; it does not handle
    robots.txt itself (that is a future crawl loop's job, not this adapter's).
- `openagentsearch.pipeline.index.index_source_document()` / `index_source_documents()`: index a
  `SourceDoc` from any adapter above the same way `index_document()` indexes raw HTML (atomic per
  document, "already indexed" refused before any embedding call), content-type aware (`html` runs
  through `extract()`; `text` is indexed as-is with `lang="und"`). When called with `root=`, the
  raw bytes and an extracted-document JSON are also written (kept even on a later failure, written
  only after the "already indexed" check) so `/doc/{sha256}` can serve the document.
  `index_source_documents()` is sequential like `index_documents()` but, unlike it, a failure does
  NOT stop the batch; it returns a `SourceIndexReport` (`indexed`, `already_indexed`, `failed`,
  `reports`, `failures`).
- `read_manifest_kind_counts(conn)` / `VectorStore.manifest_kind_counts()` (`KindCounts`, sorted by
  `source_kind`): manifest status counts broken out per `source_kind`. `make_healthz_route`'s
  `/healthz` now also reports `"kinds": {source_kind: {indexed, failed, superseded, refused}}`
  after `"index"`.
- `openagentsearch.pipeline.publish.build_static_index()` and its CLI
  (`python -m openagentsearch.pipeline.publish --db PATH --root DIR --out DIR
  [--abstract-chars 300] [--base-url URL]`): a static index export. Reads a `VectorStore`'s
  manifest and writes, atomically (temp file + `os.replace`), exactly two files under
  `out/index/` plus a `README.txt`: `manifest.json` (schema
  `openagentsearch.static-index/1` — every manifest row, every status, sorted by
  `(source_url, doc_sha256)`, with `counts` and per-`source_kind` `kinds` breakdowns) and
  `flop-surface.jsonl` (one compact JSON line per `indexed` document only, sorted by
  `(source_kind, source_url, doc_sha256)`, each carrying a title and abstract read from
  `root/extracted/<doc_sha256>.json` — `""` for either, counted in `PublishReport.missing_extracted`,
  when that record is absent or unreadable — plus the URL's `#section` fragment when present). A
  document read that fails (`ManifestCorruptionError`) is raised before any file under `out/` is
  touched, so a corrupt store leaves a pre-existing export untouched and leaves no temp file
  behind. The CLI prints one compact JSON line and exits 0 on success; a missing `--db` or `--root`
  (or any other build failure) prints one JSON error line to stderr and exits 2. See
  [docs/static-index.md](./docs/static-index.md).
- `openagentsearch.flop.chain`: a finality-gated seam for chain-derived reputation signals
  (FailedAcks, calibration snapshots, fraud verdicts) ahead of a public FLOP RPC existing —
  `ChainFact` (validated `height`/`kind`/`key`/sorted-unique `payload`/`observed_at`),
  `FinalizedHead`, the `ChainSource` / `FactSink` protocols, `NullChainSource` (always height 0,
  never yields a fact — the stub used until a real RPC exists), `StaticChainSource` (a
  fixed-fixture source for tests, validated to non-descending height order on construction), and
  `ingest_finalized_facts()`: pure orchestration that reads `finalized_head()` exactly once per
  run and hands every fact at or below that height to a `FactSink` in order, while every fact
  above it is counted in `ChainIngestReport.deferred_above_finality` and never reaches the sink —
  a moving finalized head during iteration can never widen the window one run ingests against.
  `ListFactSink` is the in-memory, duplicate-rejecting sink used by its tests. No RPC, no
  persistence, and no reorg handling below finality yet — see the module docstring.
- `config/flop.yaml` + `openagentsearch.pipeline.crawlconfig`: the bounded crawl loop's config file
  and its loader. `load_crawl_config(path) -> CrawlConfig` validates a YAML file into frozen
  `HostRule` (allowlisted host, per-host page budget, optional URL path prefixes),
  `GitHubDocsSource`, `GitHubIssuesSource`, and the top-level `CrawlConfig` (hosts, seeds, and the
  four FLOP source descriptors from package A3) -- every dataclass validates itself in
  `__post_init__` and every rejection is a `ValueError` naming the field. A host that looks like an
  IP literal is accepted only as `"127.0.0.1"` (the crawl loop's own test-server host); every seed
  must be `https://` on an allowlisted host, with the same `127.0.0.1` exception for tests.
  `allowlist_entries(config)` reshapes `config.hosts` into `AllowlistEntry` rows for `LiveIngester`;
  `config_sha256(config)` is the deterministic fingerprint (hosts/seeds/sources only, never a CLI
  override) `--resume` checks against.
- `openagentsearch.extract.links`: `extract_links(html, base_url, *, max_links=200) -> list[str]`,
  a pure `html.parser`-based `<a href>` collector -- resolves each href against `base_url`,
  lowercases scheme and host, drops the fragment, skips `mailto:`/`javascript:` and any non-
  `http(s)` scheme, and dedupes by the normalized URL in first-seen order. `url_allowed(url, rules:
  Mapping[str, HostRule]) -> bool` is the matching pure allow-check: `http`/`https`, host present
  in `rules`, and (when that host declares `path_prefixes`) the path starts with one of them.
- `openagentsearch.pipeline.crawl` (`python -m openagentsearch.pipeline.crawl`): the bounded,
  resumable crawl loop. `run_crawl(config, *, root, store, embedder, chunk_size, overlap, resume=
  False, checkpoint_every=25, min_interval_s=1.0, fetcher=None, clock=time.time, sleep=None,
  skip_sources=False, rooms_jsonl=None, gh_runner=None, max_pages_override=None) -> CrawlReport`
  first runs the four source adapters (unless `skip_sources`) through `index_source_documents()`,
  recording an `AdapterStats` per adapter and never treating a source failure as fatal, then does a
  breadth-first crawl over `LiveIngester.ingest()` starting from `config.seeds`, following only
  links `extract_links()` finds and `url_allowed()` accepts. `CrawlState` (frontier, visited,
  per-host budget progress, and a `config_sha256` fingerprint) is checkpointed atomically
  (write-to-temp + `os.replace`) to `<root>/crawl-state.json` every `checkpoint_every` attempted
  pages and once more on exit, success or exception (an exception from ingesting -- for example an
  embedder failure -- still checkpoints before propagating unchanged). `--resume` requires that
  file, refuses one saved under a different config (`config_sha256` mismatch, NOT affected by
  `--max-pages-per-host`), and reconstructs each host's remaining budget from what was already
  counted against it, removing a stale `STOP-<host>` marker when the (possibly raised) budget has
  room again. The CLI (`--allowlist`/`--config`, `--root`, `--db`, `--embedder` required;
  `--max-pages-per-host`, `--resume`, `--checkpoint-every`, `--min-interval`, `--skip-sources`,
  `--rooms-jsonl`, `--gh-runner-disabled`, `--seed` repeatable, `--dimension`) prints one compact
  JSON `CrawlReport` line to stdout and writes it to `<root>/crawl-report.json`, exiting `0` on a
  normal stop, `2` for a bad `--resume` precondition (JSON error line to stderr), `1` for any other
  failure (same error-line shape).
- `openagentsearch.flop.wire`: pure, fail-closed decoders for the public FLOP v1 wire objects
  (Appendix F) — `scale` (a bounded `Reader` plus `Compact<u32>`/fixed-integer/`bool`/`H256`
  primitives), `hashes` (corpus-verified `blake2_256`/`sha256` preimage builders for `channel_id`,
  `task_hash`, `decode_policy_hash`, `report_data`, leaf hashes V0-V3, and Merkle node/root
  recomputation), `objects` (`DataRef`, `DecodePolicy`, `ValidatorAttestation`, `VerifiedTurn` with
  its V0-V3 self-consistency rule, `ReceiptV1`/`LegacyReceipt`, and the FCC4 DA transcript
  container), `verify` (a signature seam that reports `not_verified` unless a real sr25519
  verifier is injected — none exists in the Python standard library), and `settle`
  (`verified_work_from_turns`: pure settlement arithmetic — Merkle membership, duplicate-turn and
  u128 `g_n`-overflow checks, per R11.2a). Verified end to end against the vendored public
  `tests/fixtures/flop/wire-format-v1.json` corpus (CC BY 4.0, commit `cb3cbf97a346f`, sha256
  pinned and re-checked on every test run). See [docs/flop-wire.md](./docs/flop-wire.md) for the
  reason vocabulary, the hash table, and the one documented corpus discrepancy
  (`wrong_path_orientation`'s mutation is provably undetectable by any correct Merkle-walk
  implementation for that specific vector — see the doc for why).

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
- `make_healthz_route`'s `/healthz` response gains a `"kinds"` key after `"index"` (see Added,
  above); `"kinds"` is `{}` for a store with no manifest rows yet, never absent.

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
