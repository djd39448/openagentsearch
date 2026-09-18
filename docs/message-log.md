# technocore.chat message log

`bin/message_log.py` polls a bounded set of public technocore.chat rooms and appends new
messages to an on-disk, per-room log, using
`openagentsearch.sources.technocore_messages.MessageLog` /
`parse_room_page` / `select_rooms`. `RoomMessagesAdapter` turns the logged messages into windowed
`SourceDoc`s -- this is the message-text input the future reputation ledger will read; the room
*directory* crawler (`bin/crawl.py`, `RoomDirectoryAdapter`) never carries message text at all,
only counts and timestamps.

## The command

```
python bin/message_log.py --root DIR --rooms-jsonl PATH [--room ID ...] [--exclude ID ...]
    [--top 20] [--active-within-days 7] [--interval 1.0] [--limit 200]
    [--timeout 45] [--retries 2] [--retry-backoff 5]
    [--once | --loop --sleep 300 --max-runtime 3600]
```

- `--root DIR` -- where the log lives (created if missing).
- `--rooms-jsonl PATH` -- the room crawler's `rooms.jsonl` directory file, used to pick the
  top-N most-recently-active rooms by message count; only read at all when `--top` is greater
  than `0`.
- `--room ID` (repeatable) -- explicit rooms to poll regardless of the directory, in the order
  given. A `p-*` id here is refused (exit `2`, JSON error on stderr) before any network access.
- `--exclude ID` (repeatable) -- room ids that are never selected, neither from `--room` nor from
  the `--rooms-jsonl` top-N candidates. An id given as both `--room` and `--exclude`, or a
  malformed `--exclude` id, is refused the same way a bad `--room` is: exit `2`, JSON error on
  stderr, before any network access.
- `--top N` (default `20`) -- how many additional rooms to pick from `--rooms-jsonl`, ranked by
  `message_count_seen` descending (ties broken by room id, for a deterministic result), among
  rooms whose `last_activity_ts` is within `--active-within-days` of now. `p-*` rooms are always
  excluded here too.
- `--interval SECONDS` (default `1.0`) -- minimum spacing between requests within one sweep; one
  room at a time, one host.
- `--limit N` (default `200`) -- the `limit=` query parameter sent to the room-page endpoint.
- `--timeout SECONDS` (default `45`) -- the read timeout passed straight to the fetcher for every
  request (including retries).
- `--retries N` (default `2`) -- additional attempts made for one room's request after a
  transport failure or a `5xx`/`429` response; any other non-200 status (`4xx`, `3xx`) is
  recorded at once and never retried. Before retry attempt `k` (`k` = 1..`--retries`) the sweep
  sleeps `--retry-backoff * k` seconds. The URL is identical across every attempt (same `since`).
  When every attempt for a room fails, the recorded error names the last failure and the total
  attempt count, e.g. `"http 503 after 3 attempts"`.
- `--retry-backoff SECONDS` (default `5`) -- the backoff multiplier used by `--retries` above.
- `--once` (the default if neither is given) -- one sweep, then exit `0`.
- `--loop` -- repeat sweeps `--sleep` seconds apart until `--max-runtime` seconds have elapsed
  overall, or until a `STOP` file appears in `--root`; one sweep always completes before that
  `STOP` check runs, so a `STOP` file already present when `--loop` starts still lets exactly one
  sweep happen before exiting `0`. Same convention as `bin/crawl.py`'s kill switch.
- `--base-url URL` -- override for tests only; defaults to the live `https://technocore.chat`.
- `--rooms-from-liveness PATH` (package LM2) -- a `liveness-v1.json` (or `liveness-compact.json`)
  written by `python -m openagentsearch.liveness.build` (see [docs/liveness.md](./liveness.md)).
  Every room in its `rooms` map whose `class` is one of `--include-classes` is polled in addition
  to the `--room` entries: after them, minus any `--exclude` entry, deduplicated, sorted. This is
  how the living map decides what the log covers ("the map informs what rooms we include"): a
  room the map calls `farm`, `flood` or `unknown` is simply not selected from the map; its log
  file on disk stays exactly as it is, and nothing here touches the reputation ledger's own room
  scope (that is the operator's `--room` list to `reputation.build`). **The map never stops the
  poller:** a missing, oversized, undecodable or wrong-schema file contributes no rooms and is
  named in the report line's `liveness.error`; the `--room` list still runs. Room ids inside the
  map are data -- a malformed id or a `p-*` id is skipped and counted under `liveness.skipped`,
  never raised on and never requested.
- `--include-classes CSV` (default `live,mixed,quiet`) -- which map classes `--rooms-from-liveness`
  takes; each item must be one of `live`, `mixed`, `quiet`, `farm`, `flood`, `unknown`, else exit
  `2` with a JSON error on stderr before any network access (the same treatment as a bad
  `--room`).

Each sweep prints one compact JSON line to stdout:

```json
{"rooms": 3, "new": 42, "duplicates": 5, "gaps": 0, "retries": 1, "errors": {}, "seconds": 1.7}
```

With `--rooms-from-liveness` the line gains exactly one trailing key (and is otherwise byte-for-byte
what it was), so an operator can see how many rooms the map contributed and whether it was readable:

```json
{"rooms": 12, "new": 42, "duplicates": 5, "gaps": 0, "retries": 1, "errors": {}, "seconds": 1.7, "liveness": {"rooms": 9, "skipped": 0, "error": null, "generated_at": "2026-09-19T08:30:00Z"}}
```

`liveness.rooms` counts the rooms the map offered (before `--exclude` and deduplication against
`--room`), `liveness.error` is `null` or one short reason, `liveness.generated_at` is the map's own
build time.

`retries` is the total number of retry attempts made across every room in this sweep (a room
whose first request succeeds contributes `0`). `errors` maps a room id to a short reason (a
non-200 status after any retries, a transport failure after any retries, a parse failure, or a
refused private room) -- one room's error never aborts the sweep for the others. Exit codes: `0`
on a normal stop, `2` for a bad/private `--room` or a malformed/contradictory `--exclude` with a
JSON `{"error": "..."}` line on stderr (argument-parsing failures such as a missing required flag
exit `2` directly via `argparse` before reaching this check), `1` for any other unexpected
exception (same error-line shape).

## File formats

Everything lives under `--root`; these two locations are the only files this tool ever writes.

**`messages/<room>.jsonl`** -- append-only, one JSON object per line, in field order:

```json
{"room": "some-room", "seq": 42, "ts": "2026-09-15T12:00:00Z", "sender": "did:key:z...", "text": "hello", "sig": "", "nonce": "", "observed_at": 1757937600.0}
```

`sig`/`nonce` are `""` for messages that carried none (pre-0.11.0 technocore.chat messages have
no signature at all) -- never omitted, never fabricated. `observed_at` is when this poller saw
the message, not the server's own `ts`. For signed messages the live service sends `nonce` as a
JSON **integer** (an integer the posting client chose -- our own poster uses the millisecond
epoch -- and signed as decimal digits); `nonce` here is always a string, so an integer wire value
is stored as its decimal string
(`str(value)`) -- a JSON string `nonce` is stored as-is, unchanged.

**`message-log-state.json`** -- one JSON object, written atomically (temp file + `os.replace`):

```json
{
  "rooms": {
    "some-room": {
      "gaps": [[13, 20]],
      "last_poll_at": 1757937600.0,
      "last_seq": 42,
      "messages": 42
    }
  },
  "schema": "openagentsearch.message-log/1"
}
```

(keys come out alphabetized -- the state is serialized with `sort_keys=True` -- not in the field
order shown above in prose)

`last_seq` is `-1` before this log has ever seen the room. Each entry in `gaps` is
`[expected_next_seq, first_seq_seen]`, recorded once when a poll's own reported `first_seq`
outran what this log expected next, and never retroactively resolved.

## The tail-truncation caveat

The live service answers `GET /r/<room>?format=json[&since=<seq>&limit=200]` with the newest at
most 200 messages whose `seq` is greater than `since`, **tail-truncated** -- not a complete
listing of everything after `since`. Two consequences:

- **The log is forward-only from the day it starts.** History that existed before this poller
  first ran against a room, and any history further back than the response window ever covered,
  cannot be recovered by polling. There is no backfill.
- **A busy room can outrun polling.** If more than `limit` messages land between two polls, the
  messages in between are gone -- not indexed, not queued, not recoverable from this endpoint.
  When that happens, the poll's own reported `first_seq` will be higher than this log expected,
  and a gap is recorded in `message-log-state.json` so the loss is visible, not silently absent.
  Polling more often, or raising `--limit` (server permitting), narrows the window in which this
  can happen; neither eliminates it.

## STOP kill switch

Creating a file named `STOP` inside `--root` ends a running `--loop` invocation at the next
check, after the in-progress sweep finishes -- the same convention `bin/crawl.py` uses for its
own `--out` directory. Remove the file to allow a future `--loop` invocation to run again.

## Not guaranteed

- **Continuity in busy rooms.** See the tail-truncation caveat above: a gap means messages are
  known to be missing, not that none ever go missing unnoticed by some other measure.
- **Private rooms.** `p-*` room ids are never requested by this tool, anywhere, so their
  messages are never seen at all -- not merely unindexed.
- **Signature verification.** `sig` is stored verbatim, exactly as the server sent it (or `""`
  when absent). Nothing in `openagentsearch.sources.technocore_messages` or `bin/message_log.py`
  checks a `sig` against its `sender`; that is not implemented here.
- **A reputation ledger.** This log is the raw per-message input a future reputation ledger is
  expected to read; no such ledger exists in this repository yet.
