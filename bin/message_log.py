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

Usage
-----
    python bin/message_log.py --root DIR --rooms-jsonl PATH --once
    python bin/message_log.py --root DIR --rooms-jsonl PATH --room some-room --top 0 --once
    python bin/message_log.py --root DIR --rooms-jsonl PATH --loop --sleep 300 --max-runtime 3600
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
    MessageLog,
    parse_room_page,
    select_rooms,
)

DEFAULT_BASE_URL = "https://technocore.chat"
USER_AGENT = "OpenAgentSearch-crawler/1.0"
TIMEOUT_S = 10.0
MAX_BYTES = 5_000_000
SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class SweepReport:
    """What one `run_sweep()` call did, across every room it was given."""

    rooms: int
    new: int
    duplicates: int
    gaps: int
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


def run_sweep(
    rooms: Sequence[str],
    *,
    log: MessageLog,
    fetch: Fetcher,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    interval_s: float = 1.0,
    limit: int = 200,
    base_url: str = DEFAULT_BASE_URL,
) -> SweepReport:
    """One sweep over `rooms`, in order, `interval_s` seconds apart against one host: GET each
    room's page since its logged `last_seq`, parse it, and append what is new. A `p-*` room (which
    should never reach this function -- `select_rooms` and the CLI both refuse it earlier) is
    never requested; a non-200 response, a transport failure, or a parse failure is recorded per
    room in `SweepReport.errors` and never aborts the sweep. This is the testable core: `main()`
    only parses arguments and wires `urllib_fetch` (with `Accept: application/json`) as `fetch`.
    """
    start = clock()
    total_new = 0
    total_duplicates = 0
    total_gaps = 0
    errors: dict[str, str] = {}

    for index, room in enumerate(rooms):
        if index > 0 and interval_s > 0:
            sleep(interval_s)
        if room.startswith("p-"):
            errors[room] = "refused: private room ids are never requested"
            continue

        since = log.last_seq(room)
        url = _room_url(base_url, room, limit, since)
        try:
            response: FetchResponse = fetch(url, TIMEOUT_S, MAX_BYTES, USER_AGENT)
        except Exception as exc:  # transport failure: record, never fatal to the sweep
            errors[room] = f"{type(exc).__name__}: {exc}"
            continue
        if response.status != 200:
            errors[room] = f"http {response.status}"
            continue

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
        errors=tuple(sorted(errors.items())),
        seconds=clock() - start,
    )


def _report_to_json(report: SweepReport) -> dict[str, object]:
    return {
        "rooms": report.rooms,
        "new": report.new,
        "duplicates": report.duplicates,
        "gaps": report.gaps,
        "errors": dict(report.errors),
        "seconds": report.seconds,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bin/message_log.py")
    parser.add_argument("--root", required=True)
    parser.add_argument("--rooms-jsonl", required=True)
    parser.add_argument("--room", action="append", default=[], dest="rooms")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--active-within-days", type=float, default=7.0)
    parser.add_argument("--interval", type=float, default=1.0)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    parser.add_argument("--sleep", type=float, default=300.0)
    parser.add_argument("--max-runtime", type=float, default=3600.0)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="override for tests only (default: the live host)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, build the room set, run one sweep (`--once`, the default) or repeat
    sweeps `--sleep` seconds apart until `--max-runtime` elapses or a `STOP` file appears in
    `--root` (`--loop`), and print one compact JSON report line per sweep to stdout. Returns 0 on
    a normal stop. An invalid `--room` (bad shape, or a private `p-*` id) is refused before any
    network access with exit code 2 and a JSON `{"error": "..."}` line on stderr; any other
    unexpected exception exits 1 with the same error-line shape. Argument-parsing failures (a
    missing required flag, `--once`/`--loop` both given) exit 2 directly via argparse and never
    reach this function's return statement.
    """
    args = _build_parser().parse_args(argv)
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    active_within_s = args.active_within_days * SECONDS_PER_DAY
    clock = time.time
    sleep = time.sleep

    try:
        rooms = select_rooms(
            Path(args.rooms_jsonl),
            explicit=tuple(args.rooms),
            top=args.top,
            active_within_s=active_within_s,
            now=clock(),
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
                    interval_s=args.interval, limit=args.limit, base_url=args.base_url,
                )
                print(json.dumps(_report_to_json(report), separators=(",", ":")), flush=True)
                if (root / "STOP").exists():
                    break
                if clock() - loop_start >= args.max_runtime:
                    break
                sleep(args.sleep)
            return 0

        report = run_sweep(
            rooms, log=log, fetch=fetch, clock=clock, sleep=sleep,
            interval_s=args.interval, limit=args.limit, base_url=args.base_url,
        )
        print(json.dumps(_report_to_json(report), separators=(",", ":")), flush=True)
        return 0
    except Exception as exc:
        print(
            json.dumps({"error": f"{type(exc).__name__}: {exc}"}, separators=(",", ":")),
            file=sys.stderr, flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
