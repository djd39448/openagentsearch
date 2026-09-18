#!/usr/bin/env python3
"""OpenAgentSearch -- technocore.chat message-log poller (bin/message_log.py).

Polls a bounded set of PUBLIC rooms for new messages and appends them to the on-disk log built by
`openagentsearch.sources.technocore_messages.MessageLog`. Pure standard library plus the injected
fetcher (`openagentsearch.pipeline.ingest.urllib_fetch` by default).

WHY A SEPARATE LOG FROM THE ROOM CRAWLER
-----------------------------------------
`bin/crawl.py` keeps a room *directory* (`rooms.jsonl`): counts, timestamps, a classification
hint -- never message text. The reputation ledger needs per-message facts (`seq`, server `ts`,
`from` DID, `text`, `sig`, `nonce`), so this poller reads the same rooms but persists the messages
themselves, forward-only from the day it starts (see the `technocore_messages` module docstring
for the tail-truncation caveat: history older than the log's start is unrecoverable).

PRIVATE ROOMS
-------------
Room ids beginning with "p-" are private and are NEVER requested here -- not via `--room`
(refused with exit code 2 before any network access), and not via the room-directory top-N
selection (`select_rooms` excludes them unconditionally).

RATE LIMIT / POLITENESS
------------------------
One room is polled at a time, `--interval` seconds apart, against a single host. No redirect is
ever followed (`urllib_fetch` never follows one). A non-200 response or a transport failure for
one room is recorded and never aborts the sweep.

KILL SWITCH
-----------
Create a file named STOP inside `--root` to make a running `--loop` invocation exit at the next
check (the same convention `bin/crawl.py` uses).

ROOMS FROM THE LIVENESS MAP (package LM2)
-----------------------------------------
`--rooms-from-liveness PATH` adds every room whose class in a `liveness-v1.json` (or the compact
variant) built by `openagentsearch.liveness.build` is one of `--include-classes` (default
`live,mixed,quiet`) to the explicit room list -- after `--room` entries, minus `--exclude` entries,
deduplicated, sorted. A map that is missing, unreadable, oversized, or malformed NEVER aborts the
sweep: the explicit `--room` list still runs and the sweep's report line names the problem under
`liveness.error` (the map contributes nothing that day -- fail closed for the map, never for the
poller). A `farm`/`flood`/`unknown` room in the map is simply not selected; its log file on disk is
untouched. Room ids read from the map are data: a malformed id or a `p-*` id is skipped and counted
(`liveness.skipped`), never raised on.

Usage
-----
    python bin/message_log.py --root DIR --rooms-jsonl PATH --once
    python bin/message_log.py --root DIR --rooms-jsonl PATH --room some-room --top 0 --once
    python bin/message_log.py --root DIR --rooms-jsonl PATH --loop --sleep 300 --max-runtime 3600
    python bin/message_log.py --root DIR --rooms-jsonl PATH --room builders --top 0 \
        --rooms-from-liveness liveness-v1.json --include-classes live,mixed,quiet --once
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# Allow running this script directly (``python bin/message_log.py ...``) without the caller
# having set PYTHONPATH: fall back to the repository's own ``src`` directory.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from openagentsearch.pipeline.ingest import Fetcher, FetchResponse, urllib_fetch  # noqa: E402
from openagentsearch.sources.technocore_messages import (  # noqa: E402
    _ROOM_ID_RE,
    MessageLog,
    parse_room_page,
    select_rooms,
)

DEFAULT_BASE_URL = "https://technocore.chat"
USER_AGENT = "OpenAgentSearch-crawler/1.0"
DEFAULT_TIMEOUT_S = 45.0
DEFAULT_RETRIES = 2
DEFAULT_RETRY_BACKOFF_S = 5.0
MAX_BYTES = 5_000_000
SECONDS_PER_DAY = 86400.0

# Package LM2: the liveness map (`openagentsearch.liveness.build`) as a room source. The schemas
# and the class vocabulary are repeated here as literals rather than imported, so this script
# keeps working with only `sources.technocore_messages` on the path (it never imports the
# liveness package -- the map is a plain JSON file to it). A map exceeding LIVENESS_MAX_BYTES
# is treated as unreadable, never loaded.
LIVENESS_SCHEMAS = ("openagentsearch.liveness/1", "openagentsearch.liveness-compact/1")
ROOM_CLASS_VOCABULARY = ("live", "mixed", "quiet", "farm", "flood", "unknown")
DEFAULT_INCLUDE_CLASSES = ("live", "mixed", "quiet")
LIVENESS_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class LivenessRooms:
    """What `rooms_from_liveness()` read: `rooms` is the sorted, deduplicated tuple of room ids
    whose class was one of the requested classes (empty when `error` is set); `skipped` counts
    map entries dropped as data problems (a malformed room id, a `p-*` id, a class outside the
    vocabulary); `error` is `None` on a successful read, else one short reason (the map is then
    treated as contributing nothing); `generated_at` is the map's own value when readable."""

    rooms: tuple[str, ...]
    skipped: int
    error: str | None
    generated_at: str | None


def rooms_from_liveness(path: Path, include_classes: Sequence[str]) -> LivenessRooms:
    """Read the liveness map at `path` and return the rooms whose `class` is in
    `include_classes`. Never raises: a missing, oversized, undecodable, non-object, wrong-schema
    or shapeless file comes back as `LivenessRooms((), 0, "<reason>", None)` so the caller can
    carry on with its explicit rooms and report the reason.

    NOT guaranteed: this does not validate the map beyond what it needs (`schema`, `rooms` being an
    object of objects with a string `class`) -- `openagentsearch.liveness.build.load_liveness` is
    the fail-closed loader; this is a tolerant reader of one field, by design, because the
    poller must never stop polling Dave's explicit rooms because the map had a bad day.
    """
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        return LivenessRooms((), 0, f"unreadable: {type(exc).__name__}: {exc}", None)
    if size > LIVENESS_MAX_BYTES:
        return LivenessRooms((), 0, f"oversize: {size} bytes over {LIVENESS_MAX_BYTES}", None)
    try:
        obj = json.loads(file_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return LivenessRooms((), 0, f"unreadable: {type(exc).__name__}: {exc}", None)
    if not isinstance(obj, dict):
        return LivenessRooms((), 0, "malformed: top level is not an object", None)
    schema = obj.get("schema")
    if schema not in LIVENESS_SCHEMAS:
        return LivenessRooms((), 0, f"malformed: unexpected schema {schema!r}", None)
    generated_at = obj.get("generated_at")
    generated_at = generated_at if isinstance(generated_at, str) else None
    rooms_obj = obj.get("rooms")
    if not isinstance(rooms_obj, dict):
        return LivenessRooms((), 0, "malformed: rooms is not an object", generated_at)
    wanted = set(include_classes)
    selected: set[str] = set()
    skipped = 0
    for room_id, entry in rooms_obj.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("class"), str):
            skipped += 1
            continue
        room_class = entry["class"]
        if room_class not in ROOM_CLASS_VOCABULARY:
            skipped += 1
            continue
        if room_class not in wanted:
            continue
        if not isinstance(room_id, str) or not _ROOM_ID_RE.match(room_id) or room_id.startswith("p-"):
            skipped += 1
            continue
        selected.add(room_id)
    return LivenessRooms(tuple(sorted(selected)), skipped, None, generated_at)


def parse_include_classes(raw: str) -> tuple[str, ...]:
    """`--include-classes` (a comma-separated list) as a tuple, validated against
    `ROOM_CLASS_VOCABULARY`; raises `ValueError` naming the first bad item (the CLI turns that
    into exit 2, like a bad `--room`)."""
    items = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not items:
        raise ValueError("--include-classes must name at least one class")
    for item in items:
        if item not in ROOM_CLASS_VOCABULARY:
            raise ValueError(
                f"unknown room class in --include-classes: {item!r} "
                f"(known: {', '.join(ROOM_CLASS_VOCABULARY)})"
            )
    return items


@dataclass(frozen=True)
class SweepReport:
    """What one `run_sweep()` call did, across every room it was given.

    `retries` is the total number of *retry* attempts made across every room in this sweep (a
    room whose first request succeeds contributes 0; a room that succeeds on its second attempt
    contributes 1; a room that exhausts `retries` attempts before giving up contributes exactly
    `retries`) -- it does not count each room's own first attempt.
    """

    rooms: int
    new: int
    duplicates: int
    gaps: int
    retries: int
    errors: tuple[tuple[str, str], ...]
    seconds: float


def _fetch_json(url: str, timeout_s: float, max_bytes: int, user_agent: str) -> FetchResponse:
    """`urllib_fetch` with `Accept: application/json`: the room page is a JSON endpoint, and
    `urllib_fetch`'s default `text/html` accept header is the wrong thing to ask it for (the
    GitHub tree API refuses that header outright; technocore.chat's behaviour under it was not
    verified and is not relied on)."""
    return urllib_fetch(url, timeout_s, max_bytes, user_agent, accept="application/json")


def _room_url(base_url: str, room: str, limit: int, since: int) -> str:
    url = f"{base_url}/r/{room}?format=json&limit={limit}"
    if since >= 0:
        url += f"&since={since}"
    return url


def _fetch_with_retries(
    fetch: Fetcher,
    url: str,
    *,
    timeout_s: float,
    max_bytes: int,
    user_agent: str,
    retries: int,
    retry_backoff_s: float,
    sleep: Callable[[float], None],
) -> tuple[FetchResponse | None, str | None, int]:
    """GET `url` via `fetch`, retrying after a transport failure (`fetch` raised) or a response
    whose status is 5xx or 429; any other non-200 status is returned at once, never retried. Up
    to `retries` additional attempts are made after the first (so at most `retries + 1` requests
    total), all against the identical `url`. Before attempt `k` (`k` = 1..`retries`) sleeps
    `retry_backoff_s * k` via the injected `sleep`.

    Returns `(response, error, retries_used)`: on success `response` is the `FetchResponse` and
    `error` is `None`; on total failure `response` is `None` and `error` names the last failure
    and the attempt count (e.g. `"http 503 after 3 attempts"` or `"TimeoutError: timed out after
    3 attempts"`) -- except a non-retryable non-200 status, whose `error` is just `"http NNN"`
    (one attempt, never phrased with an attempt count). `retries_used` is the number of retry
    attempts actually made (0 when the first attempt already decided the outcome).

    A negative `retries` is treated as `0` (a single attempt, no retries) rather than making the
    attempt loop empty -- callers (in particular the `--retries` CLI flag, an unbounded `int`)
    must not be able to skip the request entirely and hit the "unreachable" branch below. A
    negative `retry_backoff_s` is treated as `0.0` (retry without waiting) for the same reason:
    `time.sleep` refuses a negative argument, and a bad flag value must not abort a sweep.
    """
    retries = max(retries, 0)
    retry_backoff_s = max(retry_backoff_s, 0.0)
    for attempt_index in range(retries + 1):
        if attempt_index > 0:
            sleep(retry_backoff_s * attempt_index)
        attempts_so_far = attempt_index + 1
        try:
            response = fetch(url, timeout_s, max_bytes, user_agent)
        except Exception as exc:  # transport failure: always retryable
            if attempt_index == retries:
                error = f"{type(exc).__name__}: {exc}"
                return None, f"{error} after {attempts_so_far} attempts", attempt_index
            continue
        if response.status == 200:
            return response, None, attempt_index
        if response.status == 429 or response.status >= 500:
            if attempt_index == retries:
                error = f"http {response.status}"
                return None, f"{error} after {attempts_so_far} attempts", attempt_index
            continue
        # any other non-200 (4xx, 3xx): recorded at once, never retried
        return None, f"http {response.status}", attempt_index
    raise AssertionError("unreachable: the loop above always returns")


def run_sweep(
    rooms: Sequence[str],
    *,
    log: MessageLog,
    fetch: Fetcher,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    interval_s: float = 1.0,
    limit: int = 200,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    retries: int = DEFAULT_RETRIES,
    retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
    base_url: str = DEFAULT_BASE_URL,
) -> SweepReport:
    """One sweep over `rooms`, in order, `interval_s` seconds apart against one host: GET each
    room's page since its logged `last_seq` (retrying per `_fetch_with_retries` -- see that
    docstring for exactly which failures are retried and how the backoff is timed), parse it,
    and append what is new. A `p-*` room (which should never reach this function -- `select_rooms`
    and the CLI both refuse it earlier) is never requested; a non-200 response (after any
    retries), a transport failure (after any retries), or a parse failure is recorded per room in
    `SweepReport.errors` and never aborts the sweep. This is the testable core: `main()` only
    parses arguments and wires `urllib_fetch` (with `Accept: application/json`) as `fetch`.
    """
    start = clock()
    total_new = 0
    total_duplicates = 0
    total_gaps = 0
    total_retries = 0
    errors: dict[str, str] = {}

    for index, room in enumerate(rooms):
        if index > 0 and interval_s > 0:
            sleep(interval_s)
        if room.startswith("p-"):
            errors[room] = "refused: private room ids are never requested"
            continue

        since = log.last_seq(room)
        url = _room_url(base_url, room, limit, since)
        response, error, retries_used = _fetch_with_retries(
            fetch,
            url,
            timeout_s=timeout_s,
            max_bytes=MAX_BYTES,
            user_agent=USER_AGENT,
            retries=retries,
            retry_backoff_s=retry_backoff_s,
            sleep=sleep,
        )
        total_retries += retries_used
        if error is not None:
            errors[room] = error
            continue
        assert response is not None  # _fetch_with_retries: exactly one of response/error is set

        try:
            page = parse_room_page(response.body, room=room, observed_at=clock())
        except ValueError as exc:
            errors[room] = f"parse error: {exc}"
            continue

        report = log.append(page)
        total_new += report.new
        total_duplicates += report.duplicates
        if report.gap is not None:
            total_gaps += 1

    return SweepReport(
        rooms=len(rooms),
        new=total_new,
        duplicates=total_duplicates,
        gaps=total_gaps,
        retries=total_retries,
        errors=tuple(sorted(errors.items())),
        seconds=clock() - start,
    )


def _report_to_json(
    report: SweepReport, liveness: LivenessRooms | None = None
) -> dict[str, object]:
    """The report line. `liveness` (package LM2) adds ONE trailing key, `liveness`, only when
    `--rooms-from-liveness` was given, so a report produced without the flag is byte-identical
    to what it was before LM2."""
    out: dict[str, object] = {
        "rooms": report.rooms,
        "new": report.new,
        "duplicates": report.duplicates,
        "gaps": report.gaps,
        "retries": report.retries,
        "errors": dict(report.errors),
        "seconds": report.seconds,
    }
    if liveness is not None:
        out["liveness"] = {
            "rooms": len(liveness.rooms),
            "skipped": liveness.skipped,
            "error": liveness.error,
            "generated_at": liveness.generated_at,
        }
    return out


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bin/message_log.py")
    parser.add_argument("--root", required=True)
    parser.add_argument("--rooms-jsonl", required=True)
    parser.add_argument("--room", action="append", default=[], dest="rooms")
    parser.add_argument("--exclude", action="append", default=[], dest="exclude")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--active-within-days", type=float, default=7.0)
    parser.add_argument("--interval", type=float, default=1.0)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    parser.add_argument("--sleep", type=float, default=300.0)
    parser.add_argument("--max-runtime", type=float, default=3600.0)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF_S)
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="override for tests only (default: the live host)",
    )
    parser.add_argument(
        "--rooms-from-liveness",
        default=None,
        dest="rooms_from_liveness",
        help=(
            "a liveness-v1.json (openagentsearch.liveness.build); its rooms whose class is in "
            "--include-classes are polled in addition to --room, minus --exclude (package LM2)"
        ),
    )
    parser.add_argument(
        "--include-classes",
        default=",".join(DEFAULT_INCLUDE_CLASSES),
        dest="include_classes",
        help=(
            "comma-separated room classes taken from --rooms-from-liveness "
            f"(default: {','.join(DEFAULT_INCLUDE_CLASSES)}; known: {','.join(ROOM_CLASS_VOCABULARY)})"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, build the room set, run one sweep (`--once`, the default) or repeat
    sweeps `--sleep` seconds apart until `--max-runtime` elapses or a `STOP` file appears in
    `--root` (`--loop`), and print one compact JSON report line per sweep to stdout. Returns 0 on
    a normal stop. An invalid or private `--room`, or a malformed or contradictory `--exclude`
    (an id also given as `--room`), is refused before any network access with exit code 2 and a
    JSON `{"error": "..."}` line on stderr; any other unexpected exception exits 1 with the same
    error-line shape. Argument-parsing failures (a missing required flag, `--once`/`--loop` both
    given) exit 2 directly via argparse and never reach this function's return statement.
    """
    args = _build_parser().parse_args(argv)
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    active_within_s = args.active_within_days * SECONDS_PER_DAY
    clock = time.time
    sleep = time.sleep

    liveness: LivenessRooms | None = None
    try:
        explicit: list[str] = list(args.rooms)
        if args.rooms_from_liveness is not None:
            include_classes = parse_include_classes(args.include_classes)
            liveness = rooms_from_liveness(Path(args.rooms_from_liveness), include_classes)
            excluded = set(args.exclude)
            for room_id in liveness.rooms:
                if room_id not in excluded and room_id not in explicit:
                    explicit.append(room_id)
        rooms = select_rooms(
            Path(args.rooms_jsonl),
            explicit=tuple(explicit),
            top=args.top,
            active_within_s=active_within_s,
            now=clock(),
            exclude=tuple(args.exclude),
        )
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, separators=(",", ":")), file=sys.stderr, flush=True)
        return 2
    except Exception as exc:
        print(
            json.dumps({"error": f"{type(exc).__name__}: {exc}"}, separators=(",", ":")),
            file=sys.stderr, flush=True,
        )
        return 1

    try:
        log = MessageLog(root)
        fetch: Fetcher = _fetch_json

        if args.loop:
            loop_start = clock()
            while True:
                report = run_sweep(
                    rooms, log=log, fetch=fetch, clock=clock, sleep=sleep,
                    interval_s=args.interval, limit=args.limit, timeout_s=args.timeout,
                    retries=args.retries, retry_backoff_s=args.retry_backoff,
                    base_url=args.base_url,
                )
                print(
                    json.dumps(_report_to_json(report, liveness), separators=(",", ":")),
                    flush=True,
                )
                if (root / "STOP").exists():
                    break
                if clock() - loop_start >= args.max_runtime:
                    break
                sleep(args.sleep)
            return 0

        report = run_sweep(
            rooms, log=log, fetch=fetch, clock=clock, sleep=sleep,
            interval_s=args.interval, limit=args.limit, timeout_s=args.timeout,
            retries=args.retries, retry_backoff_s=args.retry_backoff,
            base_url=args.base_url,
        )
        print(json.dumps(_report_to_json(report, liveness), separators=(",", ":")), flush=True)
        return 0
    except Exception as exc:
        print(
            json.dumps({"error": f"{type(exc).__name__}: {exc}"}, separators=(",", ":")),
            file=sys.stderr, flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
