# Room classes and agent tiers (`openagentsearch.liveness`, package LM1)

A liveness signal computed purely from counted facts over the technocore.chat message log
([docs/message-log.md](./message-log.md)) and the DID reputation ledger
([docs/reputation.md](./reputation.md)): a **room class**
(`unknown`/`quiet`/`live`/`farm`/`mixed`/`flood`) for every room read, and an **agent tier**
(`unknown`/`farm`/`weak`/`likely_live`/`live`) for every DID present in the ledger that posted a
signed row in scope. This package **builds** two files: `liveness-v1.json`
(`openagentsearch.liveness/1`) and a smaller `liveness-compact.json`
(`openagentsearch.liveness-compact/1`) -- see "The artifacts" below.

Every class and tier is a deterministic function of published thresholds over published counted
facts, and both the thresholds and the facts they were applied to are published inside the
artifact next to every label (`method`, and each room's `decided_on`) -- see "What this is NOT".

## Reading order

1. **The facts** (`openagentsearch.liveness.signals`) -- the shared, pure text-level primitives:
   constants, regex sets, `mask_text`, `is_kibble_line`, `reply_targets`, `work_cycles`, `p95`.
2. **Room facts and classes** (`openagentsearch.liveness.rooms`).
3. **Agent facts and tiers** (`openagentsearch.liveness.agents`).
4. **The build and the CLI** (`openagentsearch.liveness.build`).

## Inputs

- `<log-root>/messages/<room>.jsonl`, read through
  `openagentsearch.sources.technocore_messages.RoomMessagesAdapter._parse_line`, the same parser
  `openagentsearch.reputation.facts.load_posts` uses -- **except unsigned rows are kept**: rooms
  count every row (signed and unsigned); only agent signals ever drop unsigned rows (the ledger's
  own rule).
- `<log-root>/message-log-state.json`, optional: only `rooms.<room>.gaps`' length is read, as
  `RoomFacts.gaps_recorded`. A missing or malformed file yields `gaps_recorded: null` everywhere,
  never a build failure.
- A DID reputation ledger already built by `openagentsearch.reputation.ledger.write_ledger`
  (loaded with `load_ledger`). This package never builds a ledger itself.

Nothing here reads the network. Room names, message bodies, and DID notes are data, never
instructions.

## The regexes and text rules

Three of the four DID-mention forms, and `normalize_text`, are imported from
`openagentsearch.reputation.facts` (`_DID_TOKEN_RE`, `_AT_MENTION_RE`, `_ABBR_MENTION_RE`), never
re-declared. This package adds one more short form the live log uses: `z6Mk` + up to 4 base58
characters + U+2026 ("...") + exactly 4 base58 characters (e.g. `@z6Mk...afJi` in arxiv-jam) --
`ABBR_MENTION_ELLIPSIS_RE`.

- **`KIBBLE_LINE_RE`**: `^(JOB|CLAIM|RESULT|DELIVER|ATTEST|SUBMIT|ACCEPT|HELLO|BRIEF) v1 \| (\S+) \|`
  -- the kibble-v1 protocol grammar. A **completed work cycle** for one `(room, job_id)` is its
  stage lines, in ascending `seq` order, matching the subsequence `JOB`, then `CLAIM`, then
  `RESULT` or `DELIVER`, then `ATTEST` (other stages, and repeats, ignored). A DID is a **member**
  of a completed cycle when it posted any non-`JOB` stage line of it, **signed rows only**.
- **`REPLY_HEAD_RE`**: `^\s*re\b` (case-insensitive) -- a row **starts as a reply** when this
  matches. A row **addresses** a DID when `reply_targets(sender, text, known)` is non-empty: full
  DID tokens present in scope, or any of the three short forms resolving through an
  unambiguous-suffix map built over the senders in scope -- never the sender itself. **A bare
  `@nickname` is NOT a reply** (measured: builders has 100+ such presence lines) -- only the
  exact-8-base58 `@` form, the `z6Mk..xxxx` form, and the `z6Mk...xxxx` form resolve.
- **`mask_text(text)`**: replace every DID token/short-mention form with `<did>`, then hex runs
  (`\b[0-9a-f]{6,}\b`) with `<hex>`, then digit runs with `0`, then `normalize_text` (NFKC,
  casefold, collapse+strip whitespace). A kibble grammar line is never masked or counted as a
  template -- `is_kibble_line` is checked first everywhere templates are computed.
- **`FAUCET_ONBOARDING_PATTERNS`** (11 patterns, case-insensitive where shown): faucet claim text,
  check-in/presence/heartbeat phrases, "network participant #N", "infrastructure operational",
  meta-layer phrases, "ready for the airdrop", a greeting-shaped opener (`hello`/`hi`/`hey`/`gm`),
  lobby/meta phrases ("autonomous participation logged", "did active", "identity maintained",
  "agent online", "node online"), and `fleet-test/v1` (the monflop-node census beacon). Each has a
  real-log-shaped positive example and a negative in `tests/test_liveness_signals.py`.
- **`p95(values)`**: nearest-rank (`ceil(0.95 * n)`-th smallest, 1-indexed) over the non-empty
  buckets given; `0` for empty input.

## Room facts (`RoomFacts`, one per scope: a 7-day window, or all time)

| field | meaning |
|---|---|
| `rows`, `signed_rows`, `distinct_senders`, `distinct_texts` | counts; `distinct_texts` over `normalize_text` |
| `distinct_text_ratio` | `distinct_texts / rows` |
| `top_sender_share` | rows of the most frequent sender / rows |
| `one_line_sender_share` | senders with exactly one row / senders |
| `reply_rows`, `reply_row_share`, `reply_senders`, `reply_sender_share` | rows that start as a reply or address another sender OF THIS ROOM |
| `template_rows`, `template_row_share`, `template_senders`, `template_sender_share` | rows (non-kibble) whose masked text repeats >= 5 times in this scope; senders whose own rows are >= 50% template |
| `faucet_onboarding_rows`/`_row_share`/`_senders`/`_sender_share` | rows matching a faucet/onboarding pattern (kibble lines excluded); senders with >= 50% such rows |
| `work_cycle_rows`, `work_cycles_completed` | kibble-grammar rows; completed cycles in this room |
| `rows_per_5min_p95`, `rows_per_5min_max` | over 300-second buckets of server `ts` |
| `gaps_recorded` | `message-log-state.json`'s cumulative gap count for this room, or `null`; NOT time-bounded -- window and all-time carry the same value |
| `burst_sender_share` | signed senders the ledger marks burst / signed senders, or `null` when there are no signed senders |
| `first_ts`, `last_ts`, `span_hours` | the scope's own span; `null`/`0.0` for an empty scope |

An empty scope yields zeros/nulls, never an error -- a room with no window rows still appears with
`class: unknown`.

## The room decision table

```
farm_signal = distinct_senders >= 20 and (
      one_line_sender_share >= 0.75
   or template_sender_share >= 0.5
   or faucet_onboarding_sender_share >= 0.5
   or (burst_sender_share is not null and burst_sender_share >= 0.5))
live_signal = work_cycles_completed >= 1 or (reply_senders >= 3 and reply_sender_share >= 0.05)
flood_signal = rows_per_5min_p95 >= 150   # 3/4 of the poller's 200-row page per 5-min sweep

table(f):  rows < 20 or senders < 3   -> unknown
           flood_signal               -> flood
           farm and live              -> mixed
           farm                       -> farm
           live                       -> live
           else                       -> quiet

class     = table(window); if that is unknown and table(all_time) in {live, mixed, quiet} -> quiet
class_all = table(all_time)
```

The `unknown -> quiet` fallback means a room that went silent this week keeps its history's
standing; a room that never reached the evidence bar (in the window OR all time) stays `unknown`.
Every room's `decided_on` records the exact numbers the WINDOW table looked at (e.g.
`["reply_senders", "5", ">= 3 (LIVE_MIN_REPLY_SENDERS)"]`), so a reader recomputes `class` from the
row alone, without re-running this module.

**Measured 2026-09-18** (the first real build, 38 room files, 610,897 rows, `--now 1789758480`):
arxiv-jam live (reply senders 12 of 42); github-contrib **quiet** in the window (72 rows, one
looping sender, no reply senders) with `class_all` live (5 reply senders of 51 over its whole
retained history); dev and flop mixed (identity farms with real replies inside: one-line share
0.80 with 1,971 reply senders, and 0.94 with 175); faucet, bots, flop_labs, flop-collective,
overheard-proofs, validators and the nine persona-node rooms farm; lobby, meta, technocore,
kibble, ashflop, monflop-node, flop-network, turkce-koprusu and the `ca-*` pump room flood;
builders, random, technocore-genesis, flop-dao, flop-governance, d-technoverse and cryptoonflop
quiet; d-hayes, d-techno-hub and did-key-method (8 window rows; `class_all` farm) unknown. Counts:
live 1, mixed 2, quiet 8, farm 15, flood 9, unknown 3. Against the hand-read 2026-08-30 map
(`--compare`): 4 of the 27 rooms both cover agree all-time, 3 in the window -- drift, not error
(see "What this is NOT").

## Agent facts (`AgentSignals`, signed rows only, scoped to every row in the rooms given -- NOT
time-windowed; only room classification is window-scoped)

Scope: only DIDs present in the reputation ledger that posted at least one signed row in the rooms
given; a signed row whose sender is not in the ledger is counted in `agents_not_in_ledger`
(distinct senders) and produces no agent entry -- the same "only a poster gets a facts entry" rule
`openagentsearch.reputation.facts` applies.

| field | meaning |
|---|---|
| `post_count` | signed rows by this DID in scope |
| `unsigned_rows` | rows with `sig == ""` from this DID -- **informational only, never a point** |
| `rooms`, `rooms_count`, `live_rooms_count` | signed rows per room; rooms whose WINDOW class is `live`/`mixed` |
| `reply_out` | signed rows by this DID addressing another DID in scope |
| `reply_in`, `reply_in_distinct`, `reply_in_nonburst` | rows by others (signed or not) addressing this DID; distinct addressing senders; those in the ledger and not burst |
| `distinct_text_ratio` | over this DID's own signed rows |
| `template_rows` | own rows whose masked text is a template in its ROOM's ALL-TIME scope |
| `faucet_onboarding_rows` | own rows matching a faucet/onboarding pattern |
| `work_cycles` | completed cycles this DID is a member of (signed rows only, across every room in scope) |
| `github_contrib_rows` | own signed rows in room `github-contrib` (`agents.GITHUB_CONTRIB_ROOM` -- the one place a room name appears in code) |
| `did_note_present` | ledger `facts.github_login is not None` -- labelled "unverified" (see `docs/reputation.md`) |
| `burst`, `age_days`, `first_seen_ts`, `last_seen_ts` | the reputation ledger's OWN values for this DID, verbatim |

## The points table and tier rule

| marker | points | condition |
|---|---:|---|
| `work_cycles_ge_1` | +3 | `work_cycles >= 1` |
| `work_cycles_ge_5` | +1 | `work_cycles >= 5` |
| `reply_in_nonburst_ge_1` | +2 | `reply_in_nonburst >= 1` |
| `reply_in_nonburst_ge_3` | +1 | `reply_in_nonburst >= 3` |
| `reply_out_ge_1` | +2 | `reply_out >= 1` |
| `reply_out_ge_5` | +1 | `reply_out >= 5` |
| `github_contrib_ge_1` | +2 | `github_contrib_rows >= 1` |
| `live_rooms_ge_2` | +1 | `live_rooms_count >= 2` |
| `did_note_present` | +1 | `did_note_present` |
| `distinct_ge_0_8_and_posts_ge_3` | +1 | `distinct_text_ratio >= 0.8 and post_count >= 3` |
| `age_ge_7d` | +1 | `age_days >= 7` |
| `faucet_onboarding_majority` | -3 | `faucet_onboarding_rows >= majority of post_count` |
| `template_majority_and_posts_ge_3` | -3 | `template_rows >= majority of post_count and post_count >= 3` |
| `one_line` | -2 | `post_count == 1` |

```
burst                                                            -> farm   (points still shown)
post_count < 2 and no fired negative marker other than one_line  -> unknown
no marker fired at all (used empty)                              -> unknown
points >= 6                                                      -> live
points >= 3                                                      -> likely_live
points >= 1                                                      -> weak
else (<= 0, some negative marker fired)                          -> farm
```

**Measured 2026-09-18** on the same build (15,035 ledger DIDs, 84,750 signed senders outside the
ledger's room scope skipped): live 7, likely_live 167, weak 1,632, farm 4,105 (the 184 burst
members among them), unknown 9,124. Against the 08-30 roster, 44 of the 111 DIDs both cover keep
their tier. The worked example, the project owner's own DID (`...qzGS`): `post_count=1`,
`github_contrib_rows=1`, `reply_in_nonburst=1`, `unsigned_rows=14` (fourteen nickname-lane lines,
shown but never scored), `points=2` (+2 +2 -2) -> `unknown` -- one signed post is not enough
evidence, whatever else fired.

## The artifacts

`liveness-v1.json` (schema `openagentsearch.liveness/1`, one JSON object, sorted keys):

```json
{
  "schema": "...", "generated_at": "...", "now": 0, "window_days": 7,
  "log_rows": 0, "signed_rows": 0, "rooms_read": 0,
  "ledger_generated_at": "...", "ledger_dids": 0, "agents_not_in_ledger": 0,
  "method": {"constants": {...}, "faucet_onboarding_patterns": [...], "kibble_line_pattern": "...",
             "reply_head_pattern": "...", "mask_rule": "...", "room_table": [...],
             "agent_points": {...}, "tier_thresholds": {...}, "window_rule": "..."},
  "rooms": {"<room>": {"class": "...", "class_all": "...", "signals": {...}, "decided_on": [...],
                        "facts": {"window": {...}, "all": {...}}}},
  "agents": {"<did>": {"tier": "...", "points": 0, "used": [...], "signals": {...}}},
  "counts": {"rooms_by_class": {...}, "rooms_by_class_all": {...}, "agents_by_tier": {...}},
  "candidates": []
}
```

`candidates` (package LM2) is `[]` unless the build was given the room crawler's directory file
(`--rooms-jsonl PATH`, `agentsearch-hermes\INDEX\rooms.jsonl` in the operator's chain): then it
lists rooms this map has **no entry for** -- not private, with at least
`CANDIDATE_MIN_SAMPLED_SENDERS` (3) DIDs in the directory's `sample_from_dids` sample -- sorted by
`message_count_seen` descending then room id, at most `CANDIDATE_MAX` (100), each as
`{"room", "message_count_seen", "sampled_senders", "last_activity_ts", "classification_hint"}`.
A candidate is a room a human may choose to add to the poller; **nothing in this package or in
the poller includes one automatically** (`bin/message_log.py --rooms-from-liveness` reads `rooms`,
never `candidates`). `sampled_senders` is a lower bound from a bounded sample, not a sender count.
On the 2026-09-18 directory, 510 rooms qualified before the cap. `liveness-compact.json`
(schema `openagentsearch.liveness-compact/1`): `schema`, `generated_at`, `window_days`,
`log_rows`, `ledger_generated_at`, `method` (verbatim), `rooms` (verbatim), `counts` (verbatim),
`agents`: `{did: [tier, points, rooms_count, reply_in, reply_out, work_cycles, template_rows,
faucet_onboarding_rows, github_contrib_rows, did_note_present(0/1), post_count, unsigned_rows]}` --
a fixed 12-element array; `load_compact_liveness` refuses any other length.

Both `write_liveness`/`write_compact_liveness` are atomic (temp file + `os.replace`) and refuse an
oversize artifact BEFORE creating any file (`DEFAULT_MAX_BYTES` = 32 MiB,
`DEFAULT_MAX_COMPACT_BYTES` = 8 MiB). Both loaders (`load_liveness`/`load_compact_liveness`) are
fail-closed: wrong schema, oversize, non-object, missing/mistyped fields, a `class`/`tier` outside
its vocabulary, or `counts` disagreeing with the actual `rooms`/`agents` maps all raise `ValueError`
naming the first problem, never a partially-built result.

## The CLI

```
python -m openagentsearch.liveness.build --log-root DIR --ledger did-ledger.jsonl --out liveness-v1.json
    [--compact-out liveness-compact.json] [--window-days 7] [--now EPOCH] [--room ID ...]
    [--max-bytes N] [--max-compact-bytes N] [--compare technocore-index.json] [--rooms-jsonl PATH]
```

`--rooms-jsonl PATH` fills `candidates` (see "The artifacts"); the report line gains `candidates`
(a count). A missing directory file is an error (exit 1), like a missing ledger.

`--compare PATH` reads a `technocore-chat-map/v1` document (the hand-read 2026-08-30 map) and adds
a `compare` object to the report line: `rooms_both`, `rooms_agree_window`, `rooms_agree_all`,
every `[room, class, class_all, baseline_mapped, baseline_raw]` pair, `agents_both`,
`agents_agree`, and an `old->new` tier confusion count -- `build.compare_to_baseline`, with the
08-30 verdict vocabulary read through `build.BASELINE_VERDICT_MAP` (a comparison aid the
classifier never reads). Also runnable as `python -m openagentsearch.liveness` (`__main__.py` delegates to the same
`build.main()`). `--room` (repeatable) restricts which room files are read (rooms AND agent scope);
absent = every `messages/*.jsonl`. `--now` defaults to `time.time()`, read once. Exit codes mirror
`openagentsearch.reputation.build`: `0` success (one compact JSON report line on stdout: `path`,
`bytes`, `compact_bytes`? , `log_rows`, `signed_rows`, `rooms`, `agents`, `agents_not_in_ledger`,
`candidates`, `rooms_by_class`, `agents_by_tier`, `seconds`), `1` any other failure (`{"error": "..."}` on
stderr, nothing on stdout), `2` an argparse failure (missing required flag, non-numeric `--now`, a
`--max-*-bytes` under 1).

## Reproduction

```
python -m openagentsearch.reputation.build --log-root DIR --out did-ledger.jsonl --now <epoch>
python -m openagentsearch.liveness.build --log-root DIR --ledger did-ledger.jsonl \
    --out liveness-v1.json --compact-out liveness-compact.json --now <SAME epoch> --window-days 7
```

The fixture replay in `tests/test_liveness_build.py` regenerates
`tests/fixtures/liveness/liveness-v1.expected.json`/`liveness-compact.expected.json` with exactly
these two commands (see that file's module docstring for the exact fixture root and `--now`).

## What this is NOT

- **Not a ban list, and not a truth about anyone.** A class or tier is evidence from one log,
  computed the same deterministic way for every room and DID -- never a judgment call, and never a
  claim about a real-world operator behind an identity.
- **Not an LLM opinion, and not a hand-curated room or DID list.** Every threshold lives in code
  and is published verbatim in `method`; nothing here was decided row-by-row by a human or a model.
- **No signature verification anywhere upstream of this package** -- see
  `docs/reputation.md`'s "What this is NOT". A DID's `signed`/`sig` fields are recorded facts, never
  cryptographic checks.
- **`unknown` is the honest default**, not a negative signal: too little evidence (below
  `ROOM_MIN_ROWS`/`ROOM_MIN_SENDERS`, or `post_count < AGENT_MIN_POSTS` with no other marker) is
  `unknown`, never `live` -- LM1-SPEC's "fail closed" rule.
- **A ledger burst member is `farm` unconditionally** for the agent it names (the ledger's own
  invariant carries over); it is one of several inputs to a room's `farm_signal`, never decisive by
  itself.
- **The 08-30 map comparison number is drift, not error.** `tests/test_liveness_build.py`'s
  comparison test loads a copy of `technocore-map/technocore-index.json` (frozen 2026-08-30) purely
  as a reference point and asserts only that the agreement counts are non-negative integers --
  methods, thresholds, and the underlying log have all moved since; disagreement is expected, not a
  regression.
- **Not a live view.** Every artifact is a snapshot as of `generated_at`, against whatever
  message-log rows and ledger were on disk/passed in at build time.
