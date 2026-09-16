# Static index export

`openagentsearch.pipeline.publish.build_static_index()` (CLI: `python -m
openagentsearch.pipeline.publish --db PATH --root DIR --out DIR`) turns one `VectorStore`'s index
manifest into three plain, GET-only files under `out/index/`:

- `manifest.json` -- every document the pipeline has ever attempted to index, one row per
  `doc_sha256`, whatever its status (`indexed` / `failed` / `superseded` / `refused`).
- `flop-surface.jsonl` -- one JSON object per line, only for rows whose status is `indexed`.
- `lexical-v1.json` -- a precomputed BM25 lexical index over the same `indexed` rows, so a reader
  with no server of its own (for example a Cloudflare Worker) can rank queries without
  embeddings. Unlike the two files above, this one can be legitimately absent even from an
  otherwise-successful publish -- see [`lexical-v1.json`](#lexical-v1json) below.

**Published.** The operator pushes these files to the `gh-pages` branch, from which GitHub Pages
serves them at the URLs below (landing page: https://djd39448.github.io/openagentsearch/). Each publish is a
snapshot: `generated_at` and `db_sha256` say which build you are reading (`manifest.json` and
`lexical-v1.json` each carry their own copy of both fields, always in agreement since both are
built from the same manifest read in the same `build_static_index()` call).

## `manifest.json`

Once published:

```
curl https://djd39448.github.io/openagentsearch/index/manifest.json
```

One JSON object, compact, `ensure_ascii=false`, trailing newline:

```json
{
  "schema": "openagentsearch.static-index/1",
  "generated_at": "2026-09-15T18:00:00Z",
  "db_sha256": "<sha256 of the source SQLite file>",
  "counts": {"indexed": 0, "failed": 0, "superseded": 0, "refused": 0},
  "kinds": {"<source_kind>": {"indexed": 0, "failed": 0, "superseded": 0, "refused": 0}},
  "documents": [
    {
      "doc_sha256": "<64 lowercase hex chars>",
      "source_url": "<the fetched URL, fragment included when the source records one>",
      "status": "indexed | failed | superseded | refused",
      "reason": "<empty for a clean indexed row; otherwise a short explanation>",
      "indexed_at": 0.0,
      "chunk_count": 0,
      "extracted_sha256": "<64 hex chars, or empty when extraction never completed>",
      "source_kind": "<adapter kind: room, github_doc, github_issue or site>"
    }
  ]
}
```

`documents` is sorted by `(source_url, doc_sha256)`. `counts` and each entry in `kinds` sum every
status, including `failed`, `superseded` and `refused` -- this file is the full bookkeeping, not a
success-only view.

## `flop-surface.jsonl`

Once published:

```
curl https://djd39448.github.io/openagentsearch/index/flop-surface.jsonl
```

One line per `indexed` document, sorted by `(source_kind, source_url, doc_sha256)`, each line a
compact JSON object:

```json
{"doc_sha256": "<64 hex chars>", "url": "<source URL>", "kind": "<source_kind>", "title": "<extracted title, or \"\" when unavailable>", "section": "<URL fragment, or null>", "abstract": "<lexical cut of the extracted text, up to abstract_chars>", "chunk_count": 0, "indexed_at": 0.0}
```

`abstract` is a whitespace-collapsed prefix of the extracted text cut at a word boundary -- it is
not a summary and no model touches it. A `superseded` document's earlier content never appears
here, only in `manifest.json`.

### Five-line Python example: fetch and filter by kind

```python
import json, urllib.request

BASE = "https://djd39448.github.io/openagentsearch/index/flop-surface.jsonl"
with urllib.request.urlopen(BASE) as response:
    rows = [json.loads(line) for line in response.read().decode("utf-8").splitlines()]
yellowpaper = [row for row in rows if row["kind"] == "github_doc"]
print(len(yellowpaper), "github_doc rows")
```

## `lexical-v1.json`

Once published:

```
curl https://djd39448.github.io/openagentsearch/index/lexical-v1.json
```

Also buildable standalone: `python -m openagentsearch.pipeline.lexical --db PATH --root DIR
--out DIR [--max-bytes N]` writes `OUT/index/lexical-v1.json` and prints one compact JSON report
line; `openagentsearch.lexical.build.build_lexical_index()` / `write_lexical_index()` are the
underlying library calls. **Lexical, not semantic**: this is a BM25 keyword index, the same family
of ranking a classic full-text search engine uses -- it has no notion of meaning, synonymy, or
paraphrase, only of which exact (casefolded) word-tokens a query and a document share.

One JSON object, compact, `ensure_ascii=false`, keys sorted:

```json
{
  "schema": "openagentsearch.lexical-index/1",
  "generated_at": "2026-09-15T18:00:00Z",
  "db_sha256": "<sha256 of the source SQLite file, matching manifest.json's>",
  "tokenizer": "unicode-word-casefold-v1",
  "bm25": {"k1": 1.2, "b": 0.75},
  "avgdl": 123.4,
  "docs": [
    {
      "sha": "<64 lowercase hex chars>", "url": "<source URL>", "title": "<extracted title>",
      "section": "<URL fragment, \"\" when absent>", "kind": "<source_kind>",
      "abstract": "<the same lexical cut flop-surface.jsonl's \"abstract\" carries>",
      "len": 87
    }
  ],
  "terms": {"<token>": [[412, 3], [977, 1]]},
  "counts": {"docs": 0, "terms": 0, "postings": 0, "dropped_terms": 0, "missing_extracted": 0}
}
```

`docs` is ordered exactly like `flop-surface.jsonl`'s rows (`(kind, url, sha)`), and a posting's
first element is an INDEX into `docs`, not a `sha` -- resolve it as `docs[doc_index]`. Each
`terms` entry is a token to its postings, `[doc_index, tf]` pairs sorted ascending by `doc_index`;
`terms`' own keys are sorted. Two builds of the same manifest/extracted data with the same
`generated_at` produce byte-identical files.

### Tokenizer (`unicode-word-casefold-v1`)

NFKC-normalize -> `casefold()` (not merely lowercase -- e.g. German "straße" folds to "strasse")
-> split into maximal runs of characters whose Unicode general category is `L` (letter) or `N`
(number), or that are `_` -- classified one character at a time via a Unicode category table,
deliberately NOT a `\w`/`\p{Word}` shorthand, so a non-Python reader (a JavaScript `\p{L}`/`\p{N}`
regex) can reproduce the exact same split -> drop any run shorter than 2 or longer than 40
characters -> a run containing `_` is emitted BOTH whole and as each of its `_`-separated parts
that independently passes the same length rule, so an identifier like
`min_force_open_escrow_for_failed_ack` is searchable as itself and as "failed ack". No stemming,
no stopword list, no language detection. `tokenize()` itself refuses input over 1,000,000
characters; `build_lexical_index()` never lets that happen, though -- a document's combined
`title + section + text` is truncated to that same limit (not refused) before tokenizing, so one
oversized indexed document can never abort an otherwise-good build. The tokenizer's behavior is
pinned by the shared fixture `tests/fixtures/lexical/tokenizer-vectors.json` (input -> tokens),
which both the Python and (package C2b) JavaScript test suites replay.

A term present in more than `max_df_ratio` of documents (default 50%) is dropped from `terms`
entirely and counted in `counts.dropped_terms` -- its absence is not evidence it never occurred.

### Ranking (`openagentsearch.lexical.search.search`)

Okapi BM25 over the query's distinct tokens (`query_terms()`, casefolded/NFKC-normalized like
document text, deduplicated in first-seen order, cut at 32 terms): for each query term present in
`terms`, `idf = ln(1 + (N - df + 0.5) / (df + 0.5))`, and each of its postings contributes
`idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len / avgdl))` to that document's score (`avgdl == 0`
is treated as a length ratio of `0`, never a division by zero). Scores are summed per document,
rounded to 6 decimal places, and a document whose rounded score is `0.0` is excluded. An optional
`kind` filter is applied BEFORE the top-`k` cut (so a filtered query can return fewer than `k`
hits even when `k` unfiltered hits exist), and results are ordered by `(-score, doc_index)` --
ties break deterministically by a document's position in `docs`, i.e. by `(kind, url, sha)`. `k`
must be an integer in `[1, 50]`; an empty or entirely tokenless query returns no hits. This exact
ranking (not an approximation of it) is the golden output `tests/fixtures/lexical/queries.json`
records -- 20 queries covering single terms, multi-term queries, an underscore identifier, `kind`
filtering, a term with no matches, a casefold-only query variant, and a `k`-truncated result set
-- and which the JavaScript Worker in package C2b must reproduce exactly.

### The 24 MiB size guard

`write_lexical_index()` serializes the whole index in memory, and if it exceeds `max_bytes`
(default 24 MiB -- see `handoff/C1-DESIGN.md` §2 for why: a Cloudflare Worker parses this file
once at isolate startup, outside the 10 ms per-request CPU budget, but startup itself still has to
finish) it raises `LexicalSizeError` BEFORE writing anything, not even a temp file.
`build_static_index()` (`pipeline.publish`) catches only this one error and reports it as
`PublishReport.lexical_error` (`""` on success) rather than failing the whole publish -- `.lexical_bytes`
is `0` in that case. `manifest.json` and `flop-surface.jsonl` are written first and are unaffected
either way; `lexical-v1.json` is written last.

**Served live, not just as a static file.** A Cloudflare Worker (package C2b) bundles
`lexical-v1.json` at deploy time and serves `GET /search` (the same BM25 ranking, over HTTP) plus a
remote MCP server at `/mcp` with no server of its own to run queries against. See
[docs/api.md](./api.md) for the routes, the MCP tools, and the operator deploy procedure.

## Not guaranteed

- Freshness: all three files are a snapshot as of `generated_at`, not a live feed.
- Signing: none of the files are signed; `db_sha256` only says which database an export came from.
- Ranking: `manifest.json` and `flop-surface.jsonl` carry no ranking, recommendation or
  endorsement of any kind. `lexical-v1.json` IS a ranking input, but a lexical (keyword-overlap)
  one -- it has no concept of relevance beyond shared tokens, and a query for a synonym or a
  paraphrase of a document's content will not find it.
- Completeness: `lexical-v1.json` may be missing from a given publish even when `manifest.json`
  and `flop-surface.jsonl` are present and current -- see the size guard above.
