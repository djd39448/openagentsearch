"""CLI: `python -m openagentsearch.pipeline.lexical --db PATH --root DIR --out DIR [--max-bytes N]`

Builds a `LexicalIndex` over `--db`'s manifest and `--root/extracted/` (see
`openagentsearch.lexical.build.build_lexical_index`) and writes it to
`--out/index/lexical-v1.json`. On success, prints one compact JSON report line to stdout and
returns 0. On a size refusal (`LexicalSizeError`) or any other construction failure, prints one
JSON `{"error": "..."}` line to stderr and returns 1; nothing is printed to stdout in that case.
Argument-parsing failures (a missing required flag, a non-integer `--max-bytes`, ...) exit the
process directly with status 2 via argparse's own behaviour. The `VectorStore` is closed on every
path that reaches it.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from openagentsearch.lexical.build import build_lexical_index, write_lexical_index
from openagentsearch.vector.store import VectorStore

_DEFAULT_MAX_BYTES = 24 * 1024 * 1024


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openagentsearch.pipeline.lexical")
    parser.add_argument("--db", required=True, help="SQLite file backing the VectorStore")
    parser.add_argument("--root", required=True, help="root directory holding extracted/")
    parser.add_argument("--out", required=True, help="directory to write index/lexical-v1.json in")
    parser.add_argument("--max-bytes", type=int, default=_DEFAULT_MAX_BYTES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, build the lexical index, write it, and print the result. See the module
    docstring for exit codes."""
    args = _build_parser().parse_args(argv)
    db_path = Path(args.db)
    root_path = Path(args.root)
    out_path = Path(args.out) / "index" / "lexical-v1.json"

    try:
        if not db_path.is_file():
            raise FileNotFoundError(f"--db not found: {db_path}")
        if not root_path.is_dir():
            raise NotADirectoryError(f"--root not found: {root_path}")
        # dimension is irrelevant here: build_lexical_index() only reads the manifest table,
        # never the vector rows, so no real embedding dimension is needed to open the store.
        store = VectorStore(db_path, dimension=1)
        try:
            index, report = build_lexical_index(store=store, root=root_path)
            written_bytes = write_lexical_index(index, out_path, max_bytes=args.max_bytes)
        finally:
            store.close()
    except Exception as exc:
        print(
            json.dumps({"error": f"{type(exc).__name__}: {exc}"}, separators=(",", ":")),
            file=sys.stderr,
            flush=True,
        )
        return 1

    print(
        json.dumps(
            {
                "path": str(out_path),
                "bytes": written_bytes,
                "docs": report.docs,
                "terms": report.terms,
                "postings": report.postings,
                "dropped_terms": report.dropped_terms,
                "missing_extracted": report.missing_extracted,
                "seconds": report.seconds,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the subprocess test
    raise SystemExit(main())
