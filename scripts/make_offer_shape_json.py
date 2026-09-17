"""Generates `worker/src/offer-shape.json`: the Worker-side copy of
`openagentsearch.flop.offer.offer_shape_status()`, field for field, so `GET /route`'s
`offer_shape` (package D2) answers the exact same object on the Cloudflare Worker and the A2
server (`openagentsearch.api.route`) -- one write path, so the two surfaces cannot drift.

Usage: python scripts/make_offer_shape_json.py [--check]

Without `--check`, (re)writes `worker/src/offer-shape.json`. With `--check`, generates the object
in memory and compares it byte-for-byte against the committed file, exiting 1 (with a one-line
diff summary on stderr) on any mismatch and 0 when they agree -- this is what
`tests/test_api_route.py` calls, the same convention `scripts/make_casefold_table.py` /
`tests/test_casefold_table.py` use for `worker/src/casefold-table.json`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "worker" / "src" / "offer-shape.json"

sys.path.insert(0, str(REPO_ROOT / "src"))  # scripts/ is not a package

from openagentsearch.flop.offer import offer_shape_status  # noqa: E402


def build_offer_shape() -> dict[str, object]:
    """`offer_shape_status()` reshaped into a plain JSON-serializable object, field for field:
    `published` (bool), `source` (str), `watch`/`binds` (tuples -> lists, order preserved --
    NEITHER is sorted, since `offer_shape_status()`'s own field order is the contract, not an
    alphabetical convenience)."""
    shape = offer_shape_status()
    return {
        "published": shape.published,
        "source": shape.source,
        "watch": list(shape.watch),
        "binds": list(shape.binds),
    }


def to_bytes(shape: dict[str, object]) -> bytes:
    """`shape` -> the exact bytes committed to disk: compact, `ensure_ascii=False` (the same
    convention `openagentsearch.reputation.compact.to_compact_json_bytes` uses -- this object's
    `source` string carries a real em dash, not worth `\\u`-escaping), sorted keys, a single
    trailing LF (never CRLF, regardless of platform)."""
    text = json.dumps(shape, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare a fresh generation against the committed file instead of writing it",
    )
    args = parser.parse_args(argv)

    fresh = to_bytes(build_offer_shape())

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
