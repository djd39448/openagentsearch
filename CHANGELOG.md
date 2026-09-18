# Changelog

Notable repository changes are recorded here. A version entry does not imply that a git tag,
package publication, or hosted release exists.

## Unreleased

### Added

- `scripts/freeze_message_log.py` (package SN): `freeze`/`verify`/`extract` for the technocore.chat
  message log that feeds `openagentsearch.reputation.build`. `freeze` writes a deterministic
  `NAME.tar.gz` (fixed `TarInfo` fields, `mtime=0` gzip, room bytes copied exactly as on disk,
  CRLF preserved) plus a `NAME.manifest.json` recording archive hashes and, per room, byte/line/
  row counts, seq coverage and gap intervals, sender and timestamp summaries. `verify` recomputes
  every recomputable field from the archive's own bytes and compares field by field; `extract`
  safely rebuilds a `--log-root`-shaped directory a reproducer can point `reputation.build` at.
  Stdlib only, standalone (no `src/` import needed to `verify`/`extract` a downloaded archive), no
  network. Answers yellowpaper #58's REPRODUCED bar: an immutable snapshot, compressed and
  decompressed sha256s, a capture/gap manifest, the exact invocation, and the expected output hash.
  See [docs/reputation.md](./docs/reputation.md#snapshots-reproducible-evidence).
- `openagentsearch.reputation.build` (package SN, deliverable 2) gains `--max-ledger-bytes`/
  `--max-compact-bytes` (default: `ledger.DEFAULT_MAX_BYTES`/`compact.DEFAULT_MAX_BYTES`, i.e.
  unchanged behaviour), passed straight through to `write_ledger`/`write_compact_ledger` as
  `max_bytes=`; a value under 1 is an `argparse` error, exit 2. Purpose: a one-off **evidence
  build** over a log larger than the published-artifact size guards -- those guards protect the
  Pages/Worker artifacts this command routinely publishes, and an evidence build that raises them
  is never itself published there. See [docs/reputation.md](./docs/reputation.md#the-cli).
- `integrations/flop-session-router/` (package RP1): `OpenAgentSearchCandidateProvider`, a
  `MinerCandidateProvider` for `retardio73-boop/flop-session-router` pinned to commit
  `dba6525554c4ea5965ef6dd23e93194736aa0ef3` (tag `v0.1.3-alpha`), mirroring their
  `ExplicitDiscoveryCandidateProvider` binding pattern: bound-DID-only, 200-only, unknown treated as
  absent (never an error), burst exclusion is exclusion not a score, and the only mutation is
  attaching `EvidenceProvenance` to a capability that has none, never overwriting existing operator
  provenance and never touching telemetry/price/assurance; fails closed (`throw` or `empty`, never
  a partial candidate list) on any ledger unavailability. Transport is https-only, no redirects, a
  5s timeout, a 512 KiB body cap, an explicit User-Agent, a 60s per-DID memo, and at most 4
  concurrent lookups. Ships with six `-text` fixtures (verbatim live `GET /did/{did}` and
  `GET /healthz` bodies captured 2026-09-18) and a `test/provider.test.ts` covering all of the above
  against an injected `fetch` stub; no network in tests, and nothing else in the repository changes.

## 0.3.0 - 2026-09-17

0.3.0 is the current repository/package version, tagged `v0.3.0` in git by the owner's
authorization. No package publication or hosted release exists; the public Worker at
https://openagentsearch.trustcoresystems.workers.dev is operator-run from this code and can lag
it. Everything below was added between 0.2.0 and this tag: the DID reputation ledger and its
published artifacts (B1, B2), the `SessionOffer` seam (D1), the observations-only `/route` (D2)
and the wire-corpus conformance report (D4a).

### Added

- `openagentsearch.reputation` (package B1): a DID reputation ledger computed purely from the
  technocore.chat message log. `facts.py` turns log rows into per-DID `DidFacts` (age, distinct-
  text ratio, mention edges via a full `did:key:z...` token, an unambiguous `@`-suffix or the
  room's abbreviated `z6Mk..xxxx` form, and two
  independent burst detectors -- a first-seen-timestamp cluster and a per-DID posting rate);
  `score.py` computes an evidence-weighted `Score` (`age_days x distinct_text_ratio x (1 +
  inbound_from_non_burst)`, rounded to 6 dp) where identity count and post count are never
  multipliers and every burst member scores exactly `0.0` regardless of size; every `Score`
  carries the exact facts it was computed from (`facts_used`), so it can be recomputed without
  the rest of the ledger. `notes.py` reads the optional, labeled `did-*` note convention
  (self-authored only, never fetched, never verified). `ledger.py` assembles the typed `Ledger`
  and its atomically-written, fail-closed-loadable JSONL file
  (`openagentsearch.did-ledger/1`). CLI: `python -m openagentsearch.reputation.build --log-root
  DIR --out FILE`. Standard library only, no network. `GET /did/{did}` still answers `404
  ledger_not_built` until a later package serves this file. See
  [docs/reputation.md](./docs/reputation.md).
- `openagentsearch.reputation.compact` (package B2): a compact Worker-sized artifact
  (`openagentsearch.did-ledger-compact/1`) built from the same `Ledger` -- full `facts`/`score`
  rows for non-burst DIDs, a four-field summary (`burst_id`, `first_seen_ts`, `post_count`,
  `max_posts_per_minute`) for burst members (49,558 of 53,856 DIDs on the live 2026-09-16 log),
  since a burst member always scores `0.0` regardless of anything else. `write_compact_ledger`/
  `load_compact_ledger` are atomic/fail-closed, the same convention as the full ledger and every
  other on-disk artifact in this repository; `CompactLedger.lookup(did)` returns the exact
  `/did/{did}` response body. `python -m openagentsearch.reputation.build` gains
  `--compact-out FILE` (writes both files from one build; report gains `compact_bytes`). `GET
  /did/{did}` now answers the same shape everywhere it is reached: `200` (full facts for a
  non-burst DID, `facts: null` + a synthesized `facts_used` for a burst member), `400
  invalid_did`, `404 unknown_did` (well-formed, absent from the ledger), or `404
  ledger_not_built` (no ledger loaded at all) -- on the A2 server (`openagentsearch.api.did`,
  new `--ledger PATH` flag; `/healthz` gains a `ledger` field), the public Worker
  (`worker/src/routes.js`'s `makeWorker(index, ledger = null)`, `/healthz` and `/` gain the same
  `ledger` field, `did_lookup`/`index_info` tools updated), and both MCP tools (remote and
  local). Every `/did/{did}` response carries `X-Ledger-Generated-At` whenever a ledger is
  loaded. `scripts/verify_public.py` gains `--ledger PATH`, checking a deployed `/healthz`'s
  `ledger.dids`/`generated_at` against a local compact artifact and that `GET
  BASE_URL/did/<our did>` answers `200` with the same `facts.first_seen_seq` -- the B2
  done-when in BUILDSPEC §3. No signature verification anywhere in this path; a score remains
  evidence, never an endorsement. See [docs/reputation.md](./docs/reputation.md) and
  [docs/api.md](./docs/api.md).
- `openagentsearch.flop.offer` (package D1): a fail-closed parsing seam for the FLOP
  `SessionOffer` opening object. The runtime `StandingOffer` / SDK `SessionOffer` (v1 spot, v2
  forward) is defined only in the private `flop-labs/flop-core` repository (sv,
  flop-labs/yellowpaper#26, 2026-09-14); the published yellow paper v0.5.0 does not contain its
  wire shape. `parse_session_offer(data: bytes | bytearray, *, max_bytes=65536) ->
  OfferParseResult` never inspects `data`'s content -- only its length and sha256 -- and always answers `status ==
  "OFFER_SHAPE_UNPUBLISHED"`, never "accepted," never "rejected as malformed": a well-formed
  future offer and random bytes get the same answer today. `offer_shape_status()` returns the
  constant `OfferShapeStatus` (`published=False`, `source`, `watch`, and `binds` -- twelve
  snake_case field names taken from sv's prose description of what the object binds, vocabulary
  from a comment, not a schema; replaced, not extended, once the shape is public). Pure, stdlib
  only, no network. This fixes the seam so a future D2 `/route` can state why it does not parse
  offers. See [docs/flop-wire.md](./docs/flop-wire.md).
- `GET /route?model_hash=&precision=&max_latency_ms=&k=` (package D2): routing signals,
  observations-only, on the A2 server (`openagentsearch.api.route.make_route_route`, wired in
  `openagentsearch.api.cli`) and the Cloudflare Worker (`worker/src/routes.js`'s `/route` branch
  plus a `route` MCP tool in `worker/src/index.js`) -- identical contract on both surfaces.
  Because no `SessionOffer` shape is public (package D1) and no published quote unit exists to
  rank across providers (`flop-labs/yellowpaper#26`), `candidates` is always `[]`
  (`candidates_reason`) and `ranking` is always `null` (`ranking_reason`) -- an invariant, not a
  fixture accident. `observations` are this service's own existing search (Worker BM25 / A2
  cosine, the SAME ranking `GET /search` uses) over `model_hash` (+ `" " + precision` when
  given), each hit `{url, kind, score, text}` joined to the reputation ledger by every `did:key:`
  token `extract_dids`/`extractDids` finds in the hit's text (at most 5, first-appearance order,
  de-duplicated) through the SAME lookup function `GET /did/{did}` itself uses, so
  `dids[].ledger` is byte-identical to that route's own body. `offer_shape` is
  `openagentsearch.flop.offer.offer_shape_status()` copied field for field; the Worker's copy is
  a generated, committed file (`worker/src/offer-shape.json`,
  `scripts/make_offer_shape_json.py`, the same generate-and-diff-check convention
  `scripts/make_casefold_table.py` uses) so both surfaces answer byte-identical bytes without
  either importing the other's code -- a Worker deployed without that file answers `500
  {"error": "offer_shape_missing"}` for every `/route` request rather than a wrong or fabricated
  shape. Validation (`model_hash` required 1-128 chars of `[A-Za-z0-9:_./-]`, `precision`
  optional 1-32 chars of the same alphabet and not checked against any vocabulary,
  `max_latency_ms` optional `1..600000` accepted and echoed but never used to filter anything,
  `k` optional `1..50` default `10` reusing `/search`'s rule) answers `400 {"error": "<code>",
  "field": "<param>"}` for the first problem found, in that order. Rate-limited and
  `HEAD`/`POST`-handled exactly like `/search`. See [docs/api.md](./docs/api.md).
- `scripts/make_wire_conformance_report.py` (package D4a): a committed, byte-deterministic
  conformance report (`tests/fixtures/flop/wire-format-v1-report.json`) reproducing every check
  `tests/test_flop_wire_corpus.py` performs against the public `wire-format-v1.json` corpus --
  positive `vectors`, all 14 `negative_cases`, and a `summary` -- so a third party can diff their
  own run against ours without re-deriving anything (`retardio73-boop/flop-conformance-lab`'s #58
  checklist: exact script, full output, pinned upstream commit, sha256s, regenerate command, all
  in one file). The three cases whose expected outcome is a signature verdict
  (`invalid_receipt_signature`, `invalid_validator_signature`, `legacy_receipt_current_channel`)
  are always `not_verifiable` (this package has no sr25519 implementation; the corpus test's own rejection
  proof uses an injected `StubVerifier`, a test device this report never treats as cryptographic
  evidence); `wrong_path_orientation` is reported `not_rejected` and mirrored into a top-level
  `deviations` array, per the known upstream corpus defect (`flop-labs/yellowpaper#44`). Stdlib
  only, no network, `sort_keys=True`/`indent=2`/LF-terminated output -- no timestamps, no absolute
  paths. `tests/test_wire_conformance_report.py` pins the committed file against a fresh subprocess
  run and checks the summary counts, the corpus sha256, and negative-case id parity. See
  [docs/flop-wire.md](./docs/flop-wire.md#conformance-report).

## 0.2.0 - 2026-09-16

0.2.0 was the repository/package version tagged `v0.2.0` in git by the owner's authorization
(superseded by 0.3.0 above). No package publication or hosted release exists for it. Everything
below was added, changed or fixed between 0.1.0 and that tag.

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
- `openagentsearch.sources.technocore_messages`: a technocore.chat message log, forward-only,
  bounded and resumable. `parse_room_page` strictly parses one `GET /r/<room>?format=json...`
  response into a `RoomPage` (messages sorted by `seq`; a single malformed message item is
  skipped and counted in `skipped_malformed`, never fatal; a payload over 5,000,000 bytes, a
  `room` field mismatch, or a non-list `messages` field raises `ValueError`). `MessageLog` appends
  only genuinely new messages (`seq` strictly greater than the room's high-water mark) to
  `messages/<room>.jsonl`, records a truncation gap (`[expected_next_seq, first_seq_seen]`) when
  a poll's own `first_seq` outran that mark, and persists per-room state atomically to
  `message-log-state.json` (`last_seq`, cumulative `messages`, every `gaps` entry,
  `last_poll_at`). `select_rooms` builds a deterministic room set from an explicit list (a
  private `p-*` id raises) plus the top-N rooms from a `rooms.jsonl` directory by
  `message_count_seen`, restricted to rooms active within a given window. `RoomMessagesAdapter`
  (`kind = "room_message"`) turns windows of up to `window` consecutive logged messages into one
  `SourceDoc` each. `bin/message_log.py` (`--root`, `--rooms-jsonl`, repeatable `--room`, `--top`,
  `--active-within-days`, `--interval`, `--limit`, `--once`/`--loop --sleep --max-runtime`,
  `--base-url` for tests) is the poller CLI: one compact JSON sweep report line per sweep to
  stdout, exit `0` on a normal stop, `2` for a bad/private `--room` (JSON error line to stderr),
  `1` for any other unexpected exception; a `STOP` file in `--root` ends a running `--loop` at the
  next check, the same convention `bin/crawl.py` uses. Never requests a `p-*` room; never follows
  a redirect (`urllib_fetch`); writes only the two files named above. `sig`/`nonce` are stored
  verbatim (possibly `""`, pre-0.11.0 messages carry none) and never cryptographically verified.
  See [docs/message-log.md](./docs/message-log.md).
- `bin/message_log.py` hardening: `--timeout SECONDS` (default `45`, replacing the old fixed
  10 s read timeout) and `--retries N` / `--retry-backoff SECONDS` (defaults `2` / `5`) retry a
  room's request after a transport failure or a `5xx`/`429` response (any other non-200 is still
  recorded at once, never retried), sleeping `retry_backoff_s * k` before retry attempt `k` and
  naming the last failure and attempt count when every attempt fails (e.g. `"http 503 after 3
  attempts"`). `run_sweep()` gains matching `timeout_s`/`retries`/`retry_backoff_s` parameters and
  `SweepReport`/the JSON report line gain a `"retries"` key (after `"gaps"`, before `"errors"`)
  counting total retry attempts across the sweep. `--exclude ROOM` (repeatable) /
  `select_rooms(..., exclude=...)` removes room ids from both the top-N candidates and `explicit`;
  an id given as both `--room`/`explicit` and `--exclude`/`exclude`, or a malformed `--exclude`
  id, is refused exactly like a bad `--room` (exit `2`, JSON error, before any network access).
- `openagentsearch.lexical`: a precomputed BM25 lexical index over the same manifest/extracted
  data `pipeline.publish` reads, built for a Cloudflare Worker (package C2b) that has no server of
  its own to embed queries against. `tokenize()`/`query_terms()` (`unicode-word-casefold-v1`):
  NFKC-normalize, casefold, split into maximal runs of characters whose Unicode general category
  is `L`/`N` or that are `_` (classified via `unicodedata.category`, never `\w`, so a JavaScript
  port can use `\p{L}`/`\p{N}` and agree exactly), drop runs shorter than 2 or longer than 40
  characters, and emit an underscore-containing run both whole and as its `_`-separated parts;
  bounded to 1,000,000 input characters. `build_lexical_index()` builds a `LexicalIndex` from a
  `VectorStore` + the same `root/extracted/` `pipeline.publish` reads (reusing its abstract cut,
  `#section` fragment and `(kind, url, sha)` ordering, so `lexical-v1.json` never drifts from
  `flop-surface.jsonl`), dropping any term present in over `max_df_ratio` (default 50%) of docs;
  two builds against the same input with the same `generated_at` are byte-identical.
  `write_lexical_index()` / `load_lexical_index()` write and fail-closed-read `lexical-v1.json`
  (schema `openagentsearch.lexical-index/1`), refusing (`LexicalSizeError`) a file over 24 MiB
  before writing anything. `search()` is the reference BM25 ranking (k1 1.2, b 0.75, ties broken
  by doc index, `kind` filtered before the top-`k` cut, scores rounded to 6 decimal places) a
  JavaScript port must reproduce exactly against the shared fixtures in `tests/fixtures/lexical/`
  (`tokenizer-vectors.json`, `fixture-index-v1.json` — 40 synthetic docs, regenerated by
  `scripts/make_lexical_fixture.py` — and `queries.json`, 20 golden queries).
  `python -m openagentsearch.pipeline.lexical --db PATH --root DIR --out DIR [--max-bytes N]`
  writes `OUT/index/lexical-v1.json` and prints one JSON report line; a size refusal or build
  failure prints a JSON error line to stderr and exits 1 (argparse errors exit 2, as elsewhere).
  `pipeline.publish.build_static_index()` now also writes `index/lexical-v1.json`, last, after
  `manifest.json` and `flop-surface.jsonl`; a `LexicalSizeError` there is reported (not raised) as
  `PublishReport.lexical_error` (`PublishReport` also gains `lexical_bytes`), leaving the other
  two files exactly as written. See [docs/static-index.md](./docs/static-index.md).
- `worker/` (package C2b): a Cloudflare Worker serving the precomputed lexical index over HTTP —
  GET-only JSON routes (`/`, `/healthz`, `/search`, `/did/{did}`, `/route` reserved, `/index/*`
  redirects to the published static files) plus a stateless remote MCP server at `/mcp`
  (`search`, `did_lookup`, `index_info`). `worker/src/search.js` is a pure-ES-module JavaScript
  port of `openagentsearch.lexical.tokenize`/`search`, verified to reproduce every case in
  `tests/fixtures/lexical/tokenizer-vectors.json` and `queries.json` exactly (BM25 term
  contributions summed in sorted term order, scores rounded to 6 decimal places); a committed,
  generated casefold table (`worker/src/casefold-table.json`, `scripts/make_casefold_table.py`,
  `tests/test_casefold_table.py`) closes the gap between `String.prototype.toLowerCase()` and
  Python's `str.casefold()`. The router (`worker/src/routes.js`) is dependency-free — no `agents`,
  no MCP SDK — so `worker/test/` runs under plain Node with nothing installed;
  `worker/src/index.js` composes it with the MCP transport (`worker/test-mcp/`, needs
  `node_modules`, WSL-only). Every route is rate-limited (except `/` and `/healthz`) via the
  Workers Rate Limiting binding, failing closed to `503` when it is missing or throws.
  `scripts/verify_public.py` (stdlib) checks a live deploy's `/healthz` counts and `/mcp` tool
  list against a local `manifest.json`; `tests/test_worker_js.py` runs the dependency-free JS
  suite as a subprocess (skipped when no `node` is on `PATH`), and `tests/test_verify_public.py`
  exercises the verify script against a loopback `http.server`. Nothing is deployed by this
  change — see [docs/api.md](./docs/api.md) (new) for every route, the MCP tool schemas, client
  configuration, and the operator deploy procedure.
- `openagentsearch.mcp.server` (package C3) gains a second tool, `did_lookup`, next to `search`:
  `DID_LOOKUP_TOOL`'s `inputSchema` requires one `did` string matching
  `^did:key:z[1-9A-HJ-NP-Za-km-z]{1,120}$` (the same shape `docs/api.md`'s `GET /did/{did}`
  documents). `MCPServer.did_lookup(did)` validates that shape locally first -- a `did` that does
  not match never causes a request and returns `(400, {"error": "invalid_did"})` synthesized
  locally -- then sends `GET <base-url>/did/<percent-encoded did>` with the same explicit
  User-Agent `search()` uses, through an opener that never follows a redirect (a 3xx answer
  surfaces as an ordinary `HTTPError`, exactly like `scripts/verify_public.py`'s opener), reading
  the response bounded at 1 MB and parsing it strictly as a JSON object. `tools/call` maps a `200`
  to `isError: false` with the parsed body as `content`/`structuredContent`; a `400`/`404` carrying
  a JSON `{"error": ...}` body (including the locally-synthesized `invalid_did` and the route's own
  `ledger_not_built` placeholder until the reputation ledger, package B2, is published) to the same
  shape with `isError: true`, so the agent sees the server's own reason; and a transport failure,
  a non-JSON body, or any other status to `isError: true` with a one-line reason instead of a
  JSON-RPC protocol error. `tools/list` now returns both tools, `search` first; unknown tool names
  keep the existing `-32602 Invalid params` path. This tool performs no signature or cryptographic
  verification of its own -- it only relays what the public route currently says. See
  [docs/agent-api.md](./docs/agent-api.md#mcp-stdio-local) and
  [docs/api.md](./docs/api.md#mcp-post-mcp) for the tool schemas and a `tools/call` example.

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

- `parse_room_page` (`openagentsearch.sources.technocore_messages`) no longer drops a signed
  message whose `nonce` is a JSON integer as malformed. Package ML's first live sweep against the
  `builders` room (`92eb605`) logged only 6 of 200 messages: the live service sends `nonce` as a
  JSON integer (`"nonce": 1789449982039`-style, paired with a string `sig`) for every signed
  message, and the parser required `nonce` to already be a string, so all 194 signed messages
  were skipped and only the 6 unsigned ones (carrying neither key) were written. An integer
  `nonce` now parses and is stored as its decimal string (`str(value)`) -- the on-disk field
  stays a string either way; a string `nonce` is unchanged, and `true`/`false`/a float `nonce`
  still counts as malformed exactly as before.

## 0.1.0 - Development baseline

0.1.0 was the development baseline. No git tag or published release is asserted by
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
