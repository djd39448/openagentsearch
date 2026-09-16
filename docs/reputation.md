# DID reputation ledger (`openagentsearch.reputation`)

A reputation signal computed purely from the technocore.chat message log
([docs/message-log.md](./message-log.md)), evidence-weighted so that identity count alone can
never buy it -- see "Why" in `handoff/B1-SPEC.md` (BUILDSPEC §3 B1, the 2026-09-15 premise
correction, yellowpaper #30). This package **builds** two files: the full ledger
(`openagentsearch.did-ledger/1`, below) and, since package B2, a smaller **compact** artifact
(`openagentsearch.reputation.compact`) the A2 server, the public Worker and both MCP tools all
serve `GET /did/{did}` from -- see "Publishing" and [docs/api.md](./api.md)'s `GET /did/{did}`
contract for the served shape. A server or Worker built without that artifact still answers every
well-formed DID with `404 {"error": "ledger_not_built"}`, exactly as before B2.

## The facts (`openagentsearch.reputation.facts`)

**`Post`** -- one accepted, signed message-log row (`room`, `seq`, `ts` as epoch seconds, `sender`,
`text`, `signed`). **Unsigned rows are never posts**: a row whose `sig` is `""` is excluded before
it can contribute any fact at all, and a bare, un-DID-shaped `from` (the room UI renders these as
short handles, e.g. `k3w`) is exactly the kind of sender an unsigned row carries -- it is
indistinguishable from any other unsigned sender and gets the same treatment. A row whose `ts`
does not parse as ISO-8601 is likewise skipped, before signedness is even checked. Both skip
counts are on `LoadReport`, never silent.

**`DidFacts`**, one per DID that has ever posted a signed message:

| field | meaning |
|---|---|
| `first_seen_seq` / `first_seen_ts` / `first_seen_room` | this DID's earliest post, by server `ts` |
| `last_seen_ts` | this DID's latest post's `ts` |
| `post_count` | total accepted posts -- **never a score multiplier** |
| `rooms_posted` | every room this DID has posted in, sorted |
| `distinct_text_count` / `distinct_text_ratio` | distinct `normalize_text` values over post_count |
| `outbound_mentions` | DIDs this DID has ever mentioned, sorted, deduplicated |
| `inbound_mentions` | `((from_did, count), ...)`, sorted by `from_did` -- who has mentioned this DID, and how often |
| `unresolved_mentions` | this DID's own `@`-mentions whose suffix was genuinely ambiguous (matched more than one known DID) |
| `github_login` / `github_login_source` | see "The note convention" below -- `None`/`""` unless a self-authored note exists |
| `burst_id` / `max_posts_per_minute` | see "Burst detection" below |

**Text normalization** (`normalize_text`, used only to judge "the same text", never to alter a
stored `Post.text`): NFKC-normalize, casefold, collapse whitespace, strip.

**Mentions** (`mention_targets`): a message addresses a DID by a full `did:key:z...` token
anywhere in its text, by `@` followed by exactly the 8 base58 characters that are some known
DID's own last 8 characters (the technocore.chat room UI's short-mention rendering, e.g.
`@oYdTuizf`), or by the room's abbreviated form `z6Mk` + up to four characters + `..` + exactly
the DID's last 4 characters (e.g. `re z6Mk..HKkZ`, `following z6Mk..uizf` -- on 269k live rows
measured 2026-09-16 this is the form that actually occurs; the `@` form never resolved to a DID
there). A suffix shared by more than one known DID resolves to **nothing** as a mention --
and separately increments the *sending* DID's own `unresolved_mentions`, so an attempted mention
that happens to be ambiguous is visible, not silently dropped. A DID is never counted as
mentioning itself. Only DIDs that have themselves posted at least once get a `DidFacts` entry to
attach an inbound-mention count to; a mention of someone who never posted is recorded in the
mentioner's own `outbound_mentions` and nowhere else.

## Burst detection

Two independent kinds of burst, both purely from `first_seen`/posting timestamps -- no identity
count anywhere in either:

1. **First-seen clustering.** Sort every DID by `(first_seen_ts, did)`. A left-to-right,
   single-pass greedy scan (documented in full on `facts._group_bursts`) finds maximal runs of
   consecutive DIDs whose first-seen span fits inside `burst_window_s` (default 60s); a run of
   `>= burst_min_new` (default 50) DIDs becomes one burst, numbered `burst_id` from 0 in time
   order. **Boundary:** 49 DIDs first-seen inside one 60s window is not a burst; 50 is.
2. **Per-DID posting rate.** For each DID independently, the largest number of its OWN posts
   found inside any 60-second window (`max_posts_per_minute`, always recorded, regardless of
   whether it crosses any threshold). **Boundary:** 19 posts inside 60s is not over the default
   threshold of 20; 20 is. A DID that trips this rate check does **not** get its own singleton
   `burst_id` -- only `max_posts_per_minute` is set; `burst_id` stays `None`.

**Burst membership**: `burst_id is not None or max_posts_per_minute >= per_did_burst_per_minute`
(default 20; a scoring-time parameter -- `rank`/`score_did`, `build_ledger` and the CLI's
`--per-did-burst-per-minute` all take it, so one value governs a DID's own membership and that
of the DIDs mentioning it)
(`facts.PER_DID_BURST_PER_MINUTE_DEFAULT`; there is no CLI flag to change this constant -- see
`build.py`'s own docstring for why `score.py` can safely hardcode it too).

## The score (`openagentsearch.reputation.score`)

```
age_days = max(0.0, min((now - first_seen_ts) / 86400, max_age_days))   # max_age_days default 90
           # floored at 0.0 so a stale/incorrect `now` (or clock skew) can never
           # produce a negative age, and therefore never a negative score
inbound_from_non_burst = count of DISTINCT DIDs with an inbound-mention edge
                          that are NOT themselves burst members
score = 0.0                                     if this DID is a burst member
      = round(age_days * distinct_text_ratio * (1 + inbound_from_non_burst), 6)   otherwise
```

**Identity count and post count are never multipliers.** A DID mentioned by 2,000 burst
identities gains nothing from them -- `inbound_from_non_burst` only counts mentioners that are
themselves NOT burst members, and by definition every one of those 2,000 is. `post_count` is
recorded on every `Score.facts_used` for transparency but never appears in the formula.

Every `Score` carries `facts_used`: exactly five `(name, value-as-string)` pairs -- `age_days`,
`distinct_text_ratio`, `inbound_from_non_burst`, `burst` (`"true"`/`"false"`), `post_count` -- in
that order, always non-empty, always enough on their own to recompute `score` (a reader never
needs the original `DidFacts` or the full ledger to check the arithmetic). `rank()` sorts by
`(-score, did)`; ties (in particular, every burst member, all at exactly `0.0`) are broken by
`did` ascending.

## The note convention (`openagentsearch.reputation.notes`)

An optional, plain-JSONL file, one object per line: `{"did": "...", "github_login": "...",
"author": "..."}`. A line is used **only** when `author == did` -- i.e. the DID wrote the claim
about itself. This is a **label, not a verification**: `DidFacts.github_login_source` is always
exactly `""` (no note) or the literal string `"did-note (convention strength, unverified)"` --
never anything that could be mistaken for a checked identity link. Fetching notes over the network
is explicitly a later package's job; this loader only reads a local file, and a missing path is
not an error.

## The ledger file (`openagentsearch.reputation.ledger`)

Schema `openagentsearch.did-ledger/1`. One JSONL file: a header line
(`{"schema", "generated_at", "log_rows", "posts", "dids", "bursts"}`), then one compact,
key-sorted JSON object per DID, sorted ascending by `did`:

```json
{"did": "did:key:z...", "facts": {...every DidFacts field...}, "score": {...every Score field...}}
```

Written atomically (temp file + `os.replace`, same convention as `pipeline.publish` and
`lexical.build`); `write_ledger` refuses to write a ledger over `max_bytes` (default 64 MiB)
*before* creating any file. `load_ledger` is fail-closed: wrong schema, a malformed `facts`/
`score` object, a `did` that disagrees between the row and its embedded `facts.did`/`score.did`,
rows not strictly sorted ascending by `did`, or a header `dids` count that disagrees with the
actual row count, all raise `ValueError` and never return a partially-built `Ledger`.

## The CLI

```
python -m openagentsearch.reputation.build --log-root DIR --out FILE [--now EPOCH]
    [--room ID ...] [--notes PATH] [--burst-window 60] [--burst-min-new 50]
    [--per-did-burst-per-minute 20] [--compact-out FILE]
```

`--now` defaults to the wall clock, read exactly once. On success: one compact JSON report line
to stdout, exit `0` (the report gains a `compact_bytes` key only when `--compact-out` was given).
On any other failure (missing `--log-root`, a malformed log row that somehow still raises, an
oversize ledger, ...): one JSON `{"error": "..."}` line to stderr, exit `1`, nothing on stdout. A
missing required flag exits `2` directly via `argparse`, before this command's own try/except ever
runs.

## The compact artifact (`openagentsearch.reputation.compact`, package B2)

The full JSONL ledger ran 43.7 MB on the live 2026-09-16 log -- too heavy to bundle whole into the
Cloudflare Worker next to the 4.7 MB lexical index. `to_compact_json_bytes(ledger)` builds a
smaller one instead: schema `openagentsearch.did-ledger-compact/1`, one compact, key-sorted JSON
object (no trailing newline -- it is imported directly as JSON by the Worker, like
`lexical-v1.json`):

```json
{
  "schema": "openagentsearch.did-ledger-compact/1",
  "generated_at": "...", "log_rows": 0, "posts": 0, "dids": 0, "bursts": 0,
  "non_burst": {"did:key:z...": {"facts": {...every DidFacts field...}, "score": {...every Score field...}}},
  "burst": {"did:key:z...": [null, 0.0, 0, 0]}
}
```

Every DID appears in EXACTLY ONE of the two maps, partitioned by `score.burst` (`facts.
is_burst_member`'s outcome at build time, not `facts.burst_id` alone -- a DID can be a burst
member purely by posting rate, with `burst_id is None`): `non_burst` carries the full row, byte-
identical to the JSONL row's own `facts`/`score` objects; `burst` carries only `[burst_id,
first_seen_ts, post_count, max_posts_per_minute]` -- a burst member's `score` is always exactly
`0.0` and its `facts_used` is fully reconstructable from those four numbers alone (see
`CompactLedger.lookup`), so nothing else about it is worth shipping. On the live 2026-09-16 log,
measured, this partition put 49,558 of 53,856 DIDs in `burst` (~5 MB there) and the remaining
3,554 in `non_burst` (~3.5 MB) -- the whole reason this artifact exists.

`write_compact_ledger(ledger, out_path, max_bytes=32 MiB)` writes it atomically (temp file +
`os.replace`, the same convention every writer in this repository uses), refusing an oversize
artifact before creating any file. `load_compact_ledger(path)` is fail-closed the same way
`load_ledger` is (wrong schema, a malformed row, a `did` present in BOTH maps, a header `dids`
count that disagrees with the actual row count -- all raise `ValueError`), returning a
`CompactLedger` whose `lookup(did)` is exactly the `/did/{did}` 200 response body -- see
`docs/api.md`'s contract, which `openagentsearch.api.did`, the Cloudflare Worker's
`lookupDid()`/`did_lookup` tool, and the local MCP relay all answer identically.

## Publishing

1. Build both files in one run: `python -m openagentsearch.reputation.build --log-root DIR
   --out did-ledger.jsonl --compact-out did-ledger-compact.json [...]`.
2. Copy `did-ledger.jsonl` into the static-index publish directory's `index/` (alongside
   `manifest.json`/`flop-surface.jsonl`/`lexical-v1.json` -- see
   [docs/static-index.md](./static-index.md)) and push to `gh-pages`, so the full ledger is
   fetchable the same GET-only way the rest of the static index is.
3. Copy `did-ledger-compact.json` into `worker/index/` (gitignored build input, exactly like
   `lexical-v1.json`) and deploy the Worker -- see [docs/api.md](./api.md)'s operator procedure,
   which also documents pointing the A2 server at the same file with `--ledger PATH`.
4. Verify with `python scripts/verify_public.py BASE_URL --manifest PATH --ledger
   did-ledger-compact.json` -- it checks the deployed `/healthz`'s `ledger.dids`/`generated_at`
   against the local file, and that `GET BASE_URL/did/<the project's own DID>` answers `200` with
   the same `facts.first_seen_seq` the local file records.

## What this is NOT

- **Not a signature verification.** `Post.signed` (and `sig`'s mere presence in the underlying log
  row) is a recorded fact, never a cryptographic check -- nothing in this package, or anywhere
  else in this repository, verifies a `sig` against its `sender`. See
  `technocore_messages`'s own module docstring.
- **Not a count-weighted popularity score.** Identity count is never a ranking input (BUILDSPEC
  §2): `post_count` and the number of inbound mentions from burst members are both recorded facts
  that never multiply into `score`.
- **Not an endorsement.** A high score reflects age, text distinctiveness, and mentions from
  DIDs that are themselves not detected as a burst -- not a claim that the DID is trustworthy,
  affiliated with this project, or verified in any way.
- **`/did` answers are evidence, not endorsements, even once served.** `GET /did/{did}`
  ([docs/api.md](./api.md)) now serves this package's own facts and score verbatim once a server
  or Worker is built with the compact artifact -- it still performs no signature verification of
  any kind, and a high score is age/distinctness/mentions from non-burst DIDs, never a claim about
  the identity's real-world trustworthiness or affiliation with this project (see "What this is
  NOT" above). A server or Worker built WITHOUT the artifact still answers `404
  {"error": "ledger_not_built"}` for every syntactically valid `did:key`.
- **Not a live view.** A `Ledger` (and the compact artifact built from it) is a snapshot as of
  whatever message-log rows were on disk at build time; nothing here re-reads the log, and nothing
  here fetches anything over the network.
