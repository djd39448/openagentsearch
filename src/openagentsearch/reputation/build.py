"""CLI: `python -m openagentsearch.reputation.build --log-root DIR --out FILE [--now EPOCH]
[--room ID ...] [--notes PATH] [--burst-window 60] [--burst-min-new 50] [--compact-out FILE]`

Builds a `Ledger` from `--log-root`'s message log (`openagentsearch.sources.technocore_messages`
on-disk layout) and writes it to `--out` (`ledger.to_jsonl_bytes`, atomic). When `--compact-out`
is given, the SAME `Ledger` from this same run is also written as the compact Worker artifact
(`openagentsearch.reputation.compact.write_compact_ledger`) -- one build, two files, never two
separate reads of the log. On success, prints one compact JSON report line to stdout and returns
0 (the report gains a `compact_bytes` key only when `--compact-out` was given). On any failure,
prints one JSON `{"error": "..."}` line to stderr and returns 1; nothing is printed to stdout in
that case.
Argument-parsing failures (a missing required flag, a non-numeric `--now`, ...) exit the process
directly with status 2 via argparse's own behaviour -- the same three-way exit-code convention
`openagentsearch.pipeline.lexical`'s CLI uses (0 / 1 / 2), not `pipeline.publish`'s (0 / 2 with no
distinct argparse code).

`--now` defaults to `time.time()`, read once, right after argument parsing -- the ONLY wall-clock
read in this whole command; everything downstream (`build_ledger`, every `Score.score_did`) takes
that same value as `now` and never reads a clock again, so two invocations a second apart differ
ONLY in `--now`'s value, never in anything computed from it twice.
"""

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from openagentsearch.reputation.compact import write_compact_ledger
from openagentsearch.reputation.ledger import build_ledger, write_ledger


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openagentsearch.reputation.build")
    parser.add_argument("--log-root", required=True, help="message-log root (holds messages/)")
    parser.add_argument("--out", required=True, help="ledger JSONL file to write")
    parser.add_argument("--now", type=float, default=None, help="epoch seconds; default: now")
    parser.add_argument("--room", action="append", default=None, dest="rooms", help="repeatable")
    parser.add_argument("--notes", default=None, help="optional did-* notes JSONL file")
    parser.add_argument("--burst-window", type=float, default=60.0, dest="burst_window_s")
    parser.add_argument("--burst-min-new", type=int, default=50, dest="burst_min_new")
    parser.add_argument(
        "--per-did-burst-per-minute", type=int, default=20, dest="per_did_burst_per_minute"
    )
    parser.add_argument(
        "--compact-out", default=None, dest="compact_out",
        help="optional: also write the compact Worker artifact (did-ledger-compact.json) here",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, build the ledger, write it, and print the result. See the module
    docstring for exit codes."""
    args = _build_parser().parse_args(argv)
    log_root = Path(args.log_root)
    out_path = Path(args.out)
    now = time.time() if args.now is None else args.now

    try:
        if not log_root.is_dir():
            raise NotADirectoryError(f"--log-root not found: {log_root}")
        notes_path = Path(args.notes) if args.notes is not None else None
        ledger, report = build_ledger(
            log_root,
            now=now,
            rooms=tuple(args.rooms) if args.rooms is not None else None,
            notes_path=notes_path,
            burst_window_s=args.burst_window_s,
            burst_min_new=args.burst_min_new,
            per_did_burst_per_minute=args.per_did_burst_per_minute,
        )
        written_bytes = write_ledger(ledger, out_path)
        compact_bytes: int | None = None
        if args.compact_out is not None:
            compact_bytes = write_compact_ledger(ledger, Path(args.compact_out))
    except Exception as exc:
        print(
            json.dumps({"error": f"{type(exc).__name__}: {exc}"}, separators=(",", ":")),
            file=sys.stderr,
            flush=True,
        )
        return 1

    result: dict[str, object] = {
        "path": str(out_path),
        "bytes": written_bytes,
        "log_rows": report.log_rows,
        "posts": report.posts,
        "skipped_malformed": report.skipped_malformed,
        "skipped_unsigned": report.skipped_unsigned,
        "dids": report.dids,
        "bursts": report.bursts,
        "notes_lines": report.notes_lines,
        "notes_used": report.notes_used,
        "seconds": report.seconds,
    }
    if compact_bytes is not None:
        result["compact_bytes"] = compact_bytes
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the subprocess test
    raise SystemExit(main())
