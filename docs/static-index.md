# Static index export

`openagentsearch.pipeline.publish.build_static_index()` (CLI: `python -m
openagentsearch.pipeline.publish --db PATH --root DIR --out DIR`) turns one `VectorStore`'s index
manifest into two plain, GET-only files under `out/index/`:

- `manifest.json` -- every document the pipeline has ever attempted to index, one row per
  `doc_sha256`, whatever its status (`indexed` / `failed` / `superseded` / `refused`).
- `flop-surface.jsonl` -- one JSON object per line, only for rows whose status is `indexed`.

**These files are not live yet.** The URLs below are where the operator intends to publish them
(the `gh-pages` branch), once that publish step actually runs -- this package only builds the two
files on disk. Until that publish happens, `curl`-ing them returns nothing that resembles
OpenAgentSearch.

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

## Not guaranteed

- Freshness: both files are a snapshot as of `generated_at`, not a live feed.
- Signing: neither file is signed; `db_sha256` only says which database an export came from.
- Ranking: nothing in either file is a ranking, a recommendation, or an endorsement.
