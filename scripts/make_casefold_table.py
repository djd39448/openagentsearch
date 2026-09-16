"""Generates `worker/src/casefold-table.json`: the table that closes the gap between JavaScript's
`String.prototype.toLowerCase()` and Python's `str.casefold()` for the one JavaScript Worker
(package C2b) needs to reproduce `openagentsearch.lexical.tokenize.tokenize()` exactly.

For every Unicode scalar value (code point 0..0x10FFFF, surrogates excluded -- they are never
valid scalar values) whose `casefold()` differs from its `lower()`, the table records the
casefolded form. The JavaScript tokenizer applies `toLowerCase()` first (which already matches
Python's `lower()` for the vast majority of characters) and then, per resulting character, looks
this table up and substitutes the mapped string when present -- see `worker/src/search.js` and
`docs/api.md` for the exact pipeline and its documented limits (Node's ICU and CPython's
`unicodedata` can disagree for characters newer than the older of the two Unicode versions; the
shared fixture `tests/fixtures/lexical/tokenizer-vectors.json` is the actual contract).

Usage: python scripts/make_casefold_table.py [--check]

Without `--check`, (re)writes `worker/src/casefold-table.json`. With `--check`, generates the
table in memory and compares it byte-for-byte against the committed file, exiting 1 (with a one-
line diff summary) on any mismatch and 0 when they agree -- this is what
`tests/test_casefold_table.py` calls.
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "worker" / "src" / "casefold-table.json"

_SURROGATE_LOW = 0xD800
_SURROGATE_HIGH = 0xDFFF
_MAX_CODEPOINT = 0x10FFFF


def build_table() -> dict[str, object]:
    """Every code point in the Unicode scalar-value range whose `casefold()` differs from its
    `lower()`, mapped to its `casefold()` result. Deterministic: depends only on the running
    interpreter's `unicodedata` tables, never on iteration order (the `map` keys are sorted at
    serialization time)."""
    mapping: dict[str, str] = {}
    for codepoint in range(_MAX_CODEPOINT + 1):
        if _SURROGATE_LOW <= codepoint <= _SURROGATE_HIGH:
            continue
        ch = chr(codepoint)
        folded = ch.casefold()
        if folded != ch.lower():
            mapping[ch] = folded
    return {"unicode": unicodedata.unidata_version, "map": mapping}


def to_bytes(table: dict[str, object]) -> bytes:
    """`table` -> the exact bytes committed to disk: compact, `ensure_ascii=True`, keys sorted,
    a single trailing LF (never CRLF, regardless of platform)."""
    text = json.dumps(table, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("ascii")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare a fresh generation against the committed file instead of writing it",
    )
    args = parser.parse_args(argv)

    fresh = to_bytes(build_table())

    if args.check:
        if not OUTPUT_PATH.exists():
            print(f"missing: {OUTPUT_PATH}", file=sys.stderr)
            return 1
        committed = OUTPUT_PATH.read_bytes()
        if committed != fresh:
            print(
                f"stale: {OUTPUT_PATH} does not match a fresh generation "
                f"({len(committed)} bytes committed, {len(fresh)} bytes fresh)",
                file=sys.stderr,
            )
            return 1
        print(json.dumps({"ok": True, "bytes": len(fresh)}))
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(fresh)
    print(json.dumps({"written": str(OUTPUT_PATH), "bytes": len(fresh)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
