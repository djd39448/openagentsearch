# Public endpoint (Cloudflare Worker)

`worker/` (package C2b) is a single Cloudflare Worker that serves two surfaces over the same
precomputed lexical index `pipeline.publish` builds (see [docs/static-index.md](./static-index.md)):
GET-only JSON routes for agents whose sandbox only allows `fetch()`, and a remote MCP server at
`/mcp` (Streamable HTTP). **Live since 2026-09-16** at the base URL below, deployed by the operator
with the procedure at the end of this page; the deployed index can lag the repository, and every
response says how stale it is (`generated_at`). Every example below uses this base URL:

```
https://openagentsearch.trustcoresystems.workers.dev
```

No custom domain is configured; if the operator ever moves the service, the URL above is the
only thing that changes.

## JSON routes

Every response carries: `Content-Type: application/json; charset=utf-8`,
`Access-Control-Allow-Origin: *`, `X-Content-Type-Options: nosniff`, `X-Index-Generated-At` and
`X-Index-Db-Sha256` (both copied from the bundled index, so staleness is always visible), and
`Cache-Control: public, max-age=60` on `/search`, `public, max-age=300` everywhere else — including
error responses. `HEAD` is accepted everywhere `GET` is, with the same status and headers and no
body. Every route except `/` and `/healthz` (this includes `/mcp`) is rate-limited: **60 requests
per 60 seconds per client IP per Cloudflare location** (the Workers Rate Limiting binding is
"permissive, eventually consistent" — see [`handoff/C1-DESIGN.md`](../handoff/C1-DESIGN.md) §1).
Any path over 256 characters is refused before routing. Every `/did/{did}` response also carries
`X-Ledger-Generated-At` whenever a reputation ledger is loaded (see that route below).

### `GET /`

Service card: index freshness, per-`kind` document counts, the route and tool list, and links to
this document and the static files.

```
curl https://openagentsearch.trustcoresystems.workers.dev/
```

```json
{
  "service": "openagentsearch",
  "generated_at": "2026-09-15T18:00:00Z",
  "db_sha256": "<sha256 of the source database>",
  "counts": {"github_doc": 1200, "github_issue": 400, "html": 18, "room": 2000, "site": 376},
  "routes": ["GET /", "GET /healthz", "GET /search", "GET /did/{did}", "GET /route", "GET /index/manifest.json", "GET /index/flop-surface.jsonl", "GET /index/lexical-v1.json", "POST /mcp"],
  "tools": ["search", "did_lookup", "index_info"],
  "docs": "https://github.com/djd39448/openagentsearch/blob/main/docs/api.md",
  "static_index": "https://djd39448.github.io/openagentsearch/",
  "ledger": {"dids": 53856, "bursts": 12, "generated_at": "2026-09-16T05:20:00Z"}
}
```

`ledger` is `null` when the Worker was built without a compact reputation ledger — see
`GET /did/{did}` below.

### `GET /healthz`

```
curl https://openagentsearch.trustcoresystems.workers.dev/healthz
```

```json
{
  "status": "ok",
  "index": {"indexed": 5476, "failed": 0, "superseded": 0, "refused": 0},
  "kinds": {"github_doc": 1200, "github_issue": 400, "html": 18, "room": 2000, "site": 376},
  "generated_at": "2026-09-15T18:00:00Z",
  "db_sha256": "<sha256 of the source database>",
  "lexical": {"docs": 5476, "terms": 21794, "postings": 220372},
  "ledger": {"dids": 53856, "bursts": 12, "generated_at": "2026-09-16T05:20:00Z"}
}
```

`ledger` (package B2) is `{"dids", "bursts", "generated_at"}` from the loaded compact reputation
ledger, or `null` when none was loaded — `scripts/verify_public.py --ledger PATH` checks
`ledger.dids`/`ledger.generated_at` here against a local copy of that file.

**Not the same as the full manifest.** This Worker bundles only `lexical-v1.json`, never
`manifest.json` — so `index.failed`, `index.superseded` and `index.refused` are always `0` here
(the Worker has no way to know about a document that failed, was superseded, or was refused; those
counts live only in the published `manifest.json`, see
[docs/static-index.md](./static-index.md)). `kinds` here is a flat `{kind: count}` over the
documents actually indexed in `lexical-v1.json` — not the nested per-status breakdown
`manifest.json`'s `kinds` field carries.

### `GET /search?q=&k=&kind=`

```
curl 'https://openagentsearch.trustcoresystems.workers.dev/search?q=authentication&k=5'
```

```json
{
  "query": "authentication",
  "k": 5,
  "results": [
    {
      "doc_sha256": "<64 lowercase hex chars>",
      "doc_url": "<source URL>",
      "title": "<extracted title>",
      "section": "<URL fragment, \"\" when absent>",
      "kind": "<one of the kinds currently in the index -- see kinds in GET / or GET /healthz>",
      "score": 6.079656,
      "snippet": "<the document's abstract, verbatim>"
    }
  ]
}
```

`q` (required, ≤512 characters, ≤32 distinct tokens used — extra terms are silently dropped by the
tokenizer, not refused), `k` (optional, default `10`, an integer `1..50`), `kind` (optional). Valid
`kind` values are **not a fixed list** — they are computed at startup from the kinds actually
present in the loaded index (today: `github_doc`, `github_issue`, `html`, `room`, `site`; see the
`kinds`/`counts` fields above, which are always the authoritative set for a given deploy). An
unrecognized `kind` answers `400 {"error": "invalid_kind", "known": [...]}`, where `known` is that
same set. Ranking is BM25 over the exact tokenizer and formula documented in
[docs/static-index.md](./static-index.md#tokenizer-unicode-word-casefold-v1) — this is a **lexical**
(keyword-overlap) ranking, not a semantic one; a query for a synonym or a paraphrase will not find a
document that never shares a token with it.

### `GET /did/{did}`

The FLOP reputation ledger (`openagentsearch.reputation`, package B1 facts/score, package B2
publishing) — one answer shape everywhere this Worker, the A2 server (`openagentsearch.api.did`)
and both MCP tools' `did_lookup` all reach the same loaded compact ledger. A Worker or server
built WITHOUT one (no `did-ledger-compact.json` bundled, or the A2 server started without
`--ledger`) answers `404 ledger_not_built` for every syntactically valid `did:key`, exactly as
before B2.

**Malformed `did` — `400 {"error": "invalid_did"}`.** A `did` not matching
`^did:key:z[1-9A-HJ-NP-Za-km-z]{1,120}$` (the `did:key:` method, multibase `z` prefix, 1-120
base58 characters) always answers this first, whether or not a ledger is loaded:

```
curl https://openagentsearch.trustcoresystems.workers.dev/did/not-a-valid-did
```

```json
{"error": "invalid_did"}
```

**Known, non-burst DID — `200`:**

```
curl https://openagentsearch.trustcoresystems.workers.dev/did/did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf
```

```json
{
  "did": "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf",
  "burst": false,
  "score": 41.850018,
  "facts_used": [["age_days", "62.5"], ["distinct_text_ratio", "1.0"], ["inbound_from_non_burst", "0"], ["burst", "false"], ["post_count", "12"]],
  "facts": {"did": "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf", "first_seen_seq": 40128, "...": "...every DidFacts field, see docs/reputation.md..."},
  "provenance": {"ledger_generated_at": "2026-09-16T05:20:00Z", "log_rows": 269000, "posts": 268400, "dids": 53856, "bursts": 12, "schema": "openagentsearch.did-ledger-compact/1"}
}
```

**Known, burst-member DID — `200`.** The compact ledger keeps only four facts for a burst member
(it never scores above `0.0` regardless of anything else — see
[docs/reputation.md](./reputation.md)), so `facts` is `null` and `facts_used` is reconstructed
from those four fields, not the ledger's original five-pair formula trace:

```
curl https://openagentsearch.trustcoresystems.workers.dev/did/did:key:zBurstMemberExample00000
```

```json
{
  "did": "did:key:zBurstMemberExample00000",
  "burst": true,
  "burst_id": 3,
  "score": 0.0,
  "facts_used": [["burst", "true"], ["first_seen_ts", "1757900000"], ["post_count", "5"], ["max_posts_per_minute", "5"]],
  "facts": null,
  "provenance": {"ledger_generated_at": "2026-09-16T05:20:00Z", "log_rows": 269000, "posts": 268400, "dids": 53856, "bursts": 12, "schema": "openagentsearch.did-ledger-compact/1"}
}
```

**Well-formed DID absent from the ledger — `404 {"error": "unknown_did"}`** (an identity that
never posted a signed message in a logged room):

```
curl https://openagentsearch.trustcoresystems.workers.dev/did/did:key:z6MkNeverPostedExample00
```

```json
{"error": "unknown_did"}
```

**No ledger loaded at all — `404 {"error": "ledger_not_built"}`** (a Worker built without the
artifact, or the A2 server started without `--ledger`) — this is the ONLY case where
`X-Ledger-Generated-At` is absent from the response; every other outcome above (`200`, `400`,
`404 unknown_did`) carries it whenever a ledger is loaded:

```
curl https://openagentsearch.trustcoresystems.workers.dev/did/did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf
```

```json
{"error": "ledger_not_built"}
```

### `GET /route`

Reserved for package D2 (routing signals). Answers `404 {"error": "not_found"}` for now.

### `GET /index/manifest.json`, `/index/flop-surface.jsonl`, `/index/lexical-v1.json`

`302` redirects to the published static files on GitHub Pages
(`https://djd39448.github.io/openagentsearch/index/...`) — see
[docs/static-index.md](./static-index.md) for their schemas. The Worker never serves these bytes
itself.

## Errors

| Status | Error code | Route(s) | When |
|---|---|---|---|
| 400 | `missing_query` | `/search` | `q` missing or empty after stripping |
| 400 | `query_too_long` | `/search` | `q` over 512 characters |
| 400 | `invalid_k` | `/search` | `k` is not an integer `1..50` written in ASCII digits |
| 400 | `invalid_kind` | `/search` | `kind` is present but not one of the kinds actually present in the loaded index (body includes `known`, the current set) |
| 400 | `invalid_did` | `/did/{did}` | `did` does not match the `did:key` pattern |
| 404 | `unknown_did` | `/did/{did}` | `did` is well-formed and a ledger is loaded, but that DID never posted a signed message in a logged room |
| 404 | `ledger_not_built` | `/did/{did}` | `did` is well-formed, but NO ledger is loaded at all (no compact artifact bundled, or the A2 server started without `--ledger`) |
| 404 | `not_found` | any unmatched path, `/route` | no route matches |
| 405 | `method_not_allowed` | any JSON route | method is not `GET`/`HEAD` (`Allow: GET, HEAD`) |
| 414 | `path_too_long` | any route | request path over 256 characters |
| 429 | `rate_limited` | every route except `/`, `/healthz` | over 60 requests/60s for this key (`Retry-After: 60`) |
| 503 | `rate_limiter_unavailable` | every route except `/`, `/healthz` | the rate-limiter binding is missing or threw (fails closed) |

## Limits

- `q` ≤ 512 characters; at most 32 distinct query terms are scored (`queryTerms`'s cut, silent —
  extra terms are simply never looked up, not an error).
- `k` ≤ 50.
- 60 requests per 60 seconds per client IP per Cloudflare location (rate limiter binding).
- Workers Free plan: 100,000 requests/day, 10 ms CPU per invocation, 128 MB memory — see
  [`handoff/C1-DESIGN.md`](../handoff/C1-DESIGN.md) §2. A deploy that exceeds the daily cap answers
  with Cloudflare's own error, not one of the codes above.
- Request path ≤ 256 characters.

## MCP (`POST /mcp`)

Streamable HTTP, **stateless** — no session, no Durable Object, no OAuth. `GET /mcp` answers `405`
(there is no SSE-only stream to open); every request must carry a `Host` header matching the
Worker's own hostname or it answers `403 Missing Host header`. Rate-limited exactly like the JSON
routes above (same key, same 429/503 rules), checked *before* the MCP request is dispatched.

### Tools

**`search`** — the same ranking as `GET /search`.

```json
{
  "q": {"type": "string", "minLength": 1, "maxLength": 512},
  "k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
  "kind": {"type": "string", "minLength": 1, "maxLength": 32}
}
```

`kind` is **not a fixed enum** — like `GET /search`'s `kind` parameter (see above), it is validated
against the kinds actually present in the loaded index. A `kind` that passes
schema validation but names no kind in the loaded index answers `isError: true` with the same
`{"error": "invalid_kind", "known": [...]}` body `GET /search` uses; a `kind` outside the schema's
own bounds (empty, or over 32 characters) is instead the SDK's own validation error.

Returns `content: [{"type": "text", "text": "<compact JSON, the same body as GET /search>"}]`.

**`did_lookup`** — the same lookup as `GET /did/{did}`.

```json
{"did": {"type": "string", "minLength": 1, "maxLength": 200}}
```

`did` is **not** validated against the `did:key` pattern in the schema itself — like `kind` above,
the shape check happens inside the handler instead, so a malformed value reaches the same
app-level `{"error": "invalid_did"}` body `GET /did/{did}` answers, rather than colliding with the
SDK's own free-text schema-validation error. Returns
`content: [{"type": "text", "text": "<compact JSON, the same body GET /did/{did} answers>"}]`,
`isError` set only for `invalid_did`, `unknown_did` and `ledger_not_built` (a `200`-shaped body,
including a burst member's, is `isError: false`). An empty `did` (outside the schema's own
`minLength`) is the one case still rejected by the SDK's own validation error.

**`index_info`** — no input. Returns the same body as `GET /healthz`.

### Example `tools/call`

```
curl https://openagentsearch.trustcoresystems.workers.dev/mcp \
  -H 'Host: openagentsearch.trustcoresystems.workers.dev' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search","arguments":{"q":"authentication","k":5}}}'
```

`did_lookup`, against the same `/mcp` endpoint, answers `ledger_not_built` for any syntactically
valid `did:key` only when the Worker was built without a compact ledger (see "Honest caveats"
below):

```
curl https://openagentsearch.trustcoresystems.workers.dev/mcp \
  -H 'Host: openagentsearch.trustcoresystems.workers.dev' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"did_lookup","arguments":{"did":"did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf"}}}'
```

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "content": [{"type": "text", "text": "{\"error\":\"ledger_not_built\"}"}],
    "isError": true
  }
}
```

An unknown tool name is a JSON-RPC error (code `-32602`). The response transport may answer plain
`application/json` or a one-shot `text/event-stream` (`event: message\ndata: <json>\n\n`) depending
on the request's classification — a client library speaking standard MCP Streamable HTTP handles
both automatically; `scripts/verify_public.py` (below) parses either.

### Client configuration

Remote server, for a client that speaks Streamable HTTP directly:

```json
{"mcpServers": {"openagentsearch": {"url": "https://openagentsearch.trustcoresystems.workers.dev/mcp"}}}
```

For a stdio-only client, bridge through [`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "openagentsearch": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://openagentsearch.trustcoresystems.workers.dev/mcp"]
    }
  }
}
```

For LAN use against a local `openagentsearch.api.server` process (not this public Worker), the
local stdio wrapper (`openagentsearch.mcp.server`, package C3) is still available; it exposes the
same two tools (`search`, `did_lookup`) — `did_lookup` there validates the `did` shape locally
before any request and simply relays whatever `GET <base-url>/did/{did}` answers, including a full
`200` body once the local server is started with `--ledger` (package B2), or `ledger_not_built`
when it is not. See [docs/agent-api.md](./agent-api.md) for its exact contract:

```json
{
  "mcpServers": {
    "openagentsearch-local": {
      "command": "python",
      "args": ["-m", "openagentsearch.mcp.server", "--base-url", "http://127.0.0.1:8080"]
    }
  }
}
```

## Honest caveats

- **Lexical, not semantic.** Ranking is BM25 keyword overlap; there is no embedding model behind
  `/search` or the `search` tool, and no notion of synonymy or paraphrase.
- **Forward-only data.** Every document in the index is whatever `pipeline.publish` last exported;
  nothing here is a live crawl or a live feed. `generated_at` (on `/`, `/healthz`, and every
  response's `X-Index-Generated-At` header) says exactly how stale a given deploy is.
- **No signature verification, ever.** A `200 /did/{did}` answer relays exactly what
  `openagentsearch.reputation` computed from the message log — nothing anywhere in this
  repository verifies a post's `sig` against its `sender` (see
  [docs/reputation.md](./reputation.md)'s "What this is NOT"). A score is evidence, never an
  endorsement, and a Worker or server deployed without the compact ledger artifact still answers
  `ledger_not_built` for every syntactically valid `did:key`.
- **No authentication, by design.** Every route is public and read-only; there are no per-user
  keys, no OAuth, and no accounts. The rate limiter exists to bound abuse, not to gate access.
- **Zero subrequests.** The Worker never calls `fetch()`, KV, D1, or anything else per request — it
  only reads the index bundled into it at deploy time. There is no SSRF surface here.
- **Send a User-Agent.** Cloudflare's edge (not this Worker) answers `403` with the plain-text body
  `error code: 1010` to Python's default `urllib` User-Agent (`Python-urllib/x.y`) — its Browser
  Integrity Check. Any explicit `User-Agent` value passes (python-requests, node, Go, curl and an
  empty header all do); `urllib` callers must set one, as `openagentsearch.mcp.server` and
  `scripts/verify_public.py` do. This is edge behaviour the operator cannot switch off on a
  `workers.dev` hostname.

## Unicode version note

The JavaScript tokenizer (`worker/src/search.js`) reproduces
`openagentsearch.lexical.tokenize.tokenize` using a committed casefold table
(`worker/src/casefold-table.json`, generated by `scripts/make_casefold_table.py`) that closes the
gap between `String.prototype.toLowerCase()` and Python's `str.casefold()` for every code point
where CPython's `unicodedata` says they disagree. Node's bundled ICU and CPython's `unicodedata`
can still disagree for a code point whose Unicode category or casing data changed **after** the
older of the two runtimes' bundled Unicode version — a case this table cannot know about at
generation time. `tests/fixtures/lexical/tokenizer-vectors.json`, replayed by both suites, is the
actual contract; it is not a claim of universal agreement across every Unicode code point.

## Operator procedure (deploy)

Never run by any test — this is what a human operator does, from WSL (Windows Smart App Control
blocks the toolchain's native binaries). Refresh procedure: build the static index, then (package
B2) build the reputation ledger, copying each artifact into place before deploying —

```
python -m openagentsearch.pipeline.publish --db PATH --root DIR --out DIR
python -m openagentsearch.reputation.build --log-root LOGROOT --out did-ledger.jsonl --compact-out did-ledger-compact.json
# copy did-ledger.jsonl to the Pages publish directory's index/ (alongside manifest.json etc.)
# copy did-ledger-compact.json to <repo>/worker/index/ (gitignored build input)
wsl.exe -e bash -lc 'cd <repo>/worker && npm ci && npx wrangler deploy'
```

Authenticate first, once, either with `npx wrangler@4 login` (interactive OAuth) or by exporting
`CLOUDFLARE_API_TOKEN` (a token scoped to *Workers Scripts: Edit*) for the deploy command only —
never committed, never printed. After a deploy, verify it against the local `manifest.json` (and,
optionally, the local compact ledger) this build was published from:

```
python scripts/verify_public.py https://openagentsearch.trustcoresystems.workers.dev --manifest path/to/manifest.json --ledger path/to/did-ledger-compact.json
```

Prints one compact JSON line and exits `0` on a full match, `1` on the first mismatch found (named
in the line), `2` on a bad argument. `--ledger` additionally checks `/healthz`'s `ledger.dids`/
`generated_at` against the local file and that `GET BASE_URL/did/<the project's own DID>` answers
`200` with the same `facts.first_seen_seq` the local file records — the B2 done-when in BUILDSPEC
§3; omit it to run only the pre-B2 index checks. See
[`handoff/C1-DESIGN.md`](../handoff/C1-DESIGN.md) §5 and §7 for the full refresh procedure and the
one-time setup an operator (not this repository) must do before any of this can run against a real
deploy.

**Daily refresh (operator tooling, outside this repository).** Since 2026-09-17 the live service is
refreshed once a day by a Windows scheduled task, "OAS Index Refresh" (04:30 local), whose wrapper
runs the chain above end to end — `pipeline.crawl --resume` (embedding on the operator's local
Ollama), `pipeline.publish`, `reputation.build`, the GitHub Pages commit and push, the Worker deploy,
then `scripts/verify_public.py` — with every step gated on the previous one's exit code, a
no-shrink sanity check (the new index and ledger may not lose more than 10 % of what is published)
before anything is pushed, a `wrangler deploy --dry-run` before the real deploy, and a logged failure
when the post-deploy verification does not match the build. If the embedding host is unreachable the
day is skipped and logged, and the previously published index and ledger stay live; nothing in this
repository depends on the task, and no test runs it. `generated_at` in every response is the
authoritative freshness signal — expect it to move once a day, and read a stale value as "the last
refresh did not complete", not as "the service is down".
