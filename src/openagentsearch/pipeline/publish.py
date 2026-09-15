"""Static index export: turn one VectorStore's index manifest into two plain files an operator can
publish (for example to a `gh-pages` branch) so any agent with an HTTP GET can read them, with no
API server and no query language required.

`build_static_index()` writes exactly two files under `out/index/` (plus a plain-text README):

- `manifest.json` -- every manifest row, every status (`indexed` / `failed` / `superseded` /
  `refused`), sorted by `(source_url, doc_sha256)`. This is the full, honest bookkeeping: nothing
  is filtered out and nothing is invented.
- `flop-surface.jsonl` -- one line per `indexed` row only, sorted by
  `(source_kind, source_url, doc_sha256)`, carrying a title, an optional `#section` fragment, and a
  short lexical abstract cut from the extracted text.

Both files are produced from a single, already-open `VectorStore` and the same `root` directory
`ExtractStore` writes extracted records under; nothing here fetches a URL, embeds a vector, or
mutates the store. Writes are atomic (temp file + `os.replace`) and are performed only after every
read that could fail (in particular, reading the manifest) has already succeeded -- so a corrupt
store never leaves a partial or stale-but-modified file behind.

What this module does NOT guarantee:

- The export is a snapshot of the manifest at the moment it was read, not a live view and not a
  promise of freshness -- nothing here records or checks how old the underlying crawl is.
- An "abstract" is a lexical cut of the extracted text (collapsed whitespace, truncated at a word
  boundary), never a summary; no language model or heuristic ranking touches it.
- `flop-surface.jsonl` excludes every row whose manifest status is not `indexed` -- a `superseded`
  document's earlier text is visible only in `manifest.json`, never in the surface file.
- Neither file is signed or checksummed beyond `manifest.json`'s own `db_sha256` field (the hash
  of the source SQLite file, which lets a reader confirm which database an export came from, not
  that the export is untampered).
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from openagentsearch.index.manifest import STATUSES, ManifestCounts, ManifestEntry
from openagentsearch.vector.store import VectorStore

SCHEMA_ID = "openagentsearch.static-index/1"

_README_TEXT = """\
OpenAgentSearch static index export
schema: {schema}

manifest.json - every document the pipeline has attempted to index, one row per
  doc_sha256, with its status (indexed / failed / superseded / refused) and reason.
flop-surface.jsonl - one JSON object per line, only for rows whose status is "indexed";
  "abstract" is a lexical cut of the extracted text, never a summary.

Both are GET-only static artifacts: plain files an operator regenerates and publishes
on their own schedule. No freshness is guaranteed and neither file is signed.

"superseded" documents stay in manifest.json but are excluded from
flop-surface.jsonl -- only the current indexed document per source_url appears there.

Nothing here is a ranking, a recommendation, or an endorsement of any listed page.
"""


@dataclass(frozen=True)
class PublishReport:
    """What one `build_static_index()` call wrote. `counts` is the same `ManifestCounts` the
    manifest's own `"counts"` field is built from -- `.as_dict()` for the JSON-shaped view."""

    documents: int
    surface_lines: int
    missing_extracted: int
    manifest_path: str
    surface_path: str
    db_sha256: str
    counts: ManifestCounts


def _counts_from_entries(entries: Sequence[ManifestEntry]) -> ManifestCounts:
    """Pure re-derivation of `ManifestCounts` from an already-read list of entries, so this
    module never issues a second manifest query (and a second chance to observe a different,
    inconsistent answer) after the one read that can raise `ManifestCorruptionError`."""
    tally = {status: 0 for status in STATUSES}
    for entry in entries:
        tally[entry.status] += 1
    return ManifestCounts(**tally)


def _kind_counts_from_entries(entries: Sequence[ManifestEntry]) -> dict[str, ManifestCounts]:
    """Per-`source_kind` `ManifestCounts`, sorted by `source_kind` -- the same reshaping
    `read_manifest_kind_counts()` does, computed here from `entries` instead of a second query."""
    per_kind: dict[str, dict[str, int]] = {}
    for entry in entries:
        tally = per_kind.setdefault(entry.source_kind, {status: 0 for status in STATUSES})
        tally[entry.status] += 1
    return {kind: ManifestCounts(**tally) for kind, tally in sorted(per_kind.items())}


def _iso8601_utc(timestamp: float) -> str:
    """`timestamp` (Unix epoch seconds) as an ISO-8601 string in UTC, `Z`-suffixed, always
    at whole-second precision so every export has the same shape."""
    stamp = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")
    return stamp.replace("+00:00", "Z")


def _make_abstract(text: str, max_chars: int) -> str:
    """Collapse `text`'s whitespace to single spaces and cut it to at most `max_chars`
    characters, never inside a word: the character immediately after the returned string in the
    collapsed text is always a space (or the returned string is the whole collapsed text).

    NOT guaranteed: a single "word" longer than `max_chars` (no space anywhere in the first
    `max_chars` characters) is still cut mid-word as a last resort -- this is a byte-preserving
    truncation, not a summarisation, and it never raises.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_chars:
        return collapsed
    if collapsed[max_chars] == " ":
        return collapsed[:max_chars]
    truncated = collapsed[:max_chars]
    cut = truncated.rfind(" ")
    return truncated[:cut] if cut > 0 else truncated


def _read_extracted(root: Path, doc_sha256: str) -> tuple[str, str, bool]:
    """Best-effort read of `root/extracted/<doc_sha256>.json` (the file `ExtractStore`/
    `index_source_document()` write, keyed by the manifest's own `doc_sha256`).

    Returns `(title, text, found)`. A missing file, a file that is not valid UTF-8, a file that
    fails to parse as JSON, or a parsed record missing a string `title`/`text` is never fatal to
    the export -- it is reported as `found=False` so the caller renders `""` for both and counts
    it toward `PublishReport.missing_extracted` instead of failing the whole export for one bad
    record.
    """
    path = root / "extracted" / f"{doc_sha256}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        title = data["title"]
        text = data["text"]
    except (OSError, ValueError, KeyError, TypeError):
        # ValueError covers both json.JSONDecodeError and UnicodeDecodeError (raised by
        # read_text() on a file that is not valid UTF-8) -- neither is an OSError subclass.
        return "", "", False
    if not isinstance(title, str) or not isinstance(text, str):
        return "", "", False
    return title, text, True


def _write_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically: a temp file in the same directory, then `os.replace()`.
    A reader never observes a partial file, and on any failure the temp file is removed rather
    than left behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def build_static_index(
    *,
    store: VectorStore,
    root: Path,
    out: Path,
    abstract_chars: int = 300,
    generated_at: float | None = None,
    base_url: str | None = None,
) -> PublishReport:
    """Read `store`'s index manifest and write `out/index/manifest.json`,
    `out/index/flop-surface.jsonl` and `out/index/README.txt`, atomically.

    Order of operations, exactly: read every manifest row (`store.manifest_entries()` -- the one
    call that can raise `ManifestCorruptionError`) -> derive counts and per-kind counts from that
    same list (no second query) -> hash the store's SQLite file -> build both documents in memory
    -> write all three files. Nothing under `out/` is touched until the manifest read has already
    succeeded, so a corrupt store leaves `out/` exactly as it was found (see `_write_atomic`).

    `generated_at` is the manifest timestamp to record (`None`, the default, means "now", read
    once via `time.time()`); passing the same `generated_at` across two calls against the same
    store is what makes the two runs byte-identical. `base_url` is accepted for the operator's own
    bookkeeping and, when given, is noted in `README.txt` as where the files are *intended* to be
    published -- it is never embedded in `manifest.json` or `flop-surface.jsonl`, and this
    function makes no claim that the files are actually reachable there.

    `abstract_chars` bounds every `flop-surface.jsonl` abstract (see `_make_abstract`); it must be
    a positive integer.

    NOT guaranteed: this is a snapshot, not a live view -- a row written to `store` after this
    function reads the manifest is not reflected in the output. `root` is trusted to be the same
    root the crawl/indexing pipeline wrote `extracted/` under; a `root` that never held that data
    simply produces `missing_extracted == surface_lines` rather than failing.
    """
    bad_abstract_chars = (
        isinstance(abstract_chars, bool)
        or not isinstance(abstract_chars, int)
        or abstract_chars <= 0
    )
    if bad_abstract_chars:
        raise ValueError(f"abstract_chars must be a positive integer, got {abstract_chars!r}")
    root = Path(root)
    out = Path(out)
    if not root.is_dir():
        raise NotADirectoryError(f"root is not an existing directory: {root}")

    entries = sorted(store.manifest_entries(), key=lambda e: (e.source_url, e.doc_sha256))
    counts = _counts_from_entries(entries)
    kind_counts = _kind_counts_from_entries(entries)
    db_sha256 = hashlib.sha256(store.path.read_bytes()).hexdigest()
    when = time.time() if generated_at is None else generated_at

    manifest_obj: dict[str, object] = {
        "schema": SCHEMA_ID,
        "generated_at": _iso8601_utc(when),
        "db_sha256": db_sha256,
        "counts": counts.as_dict(),
        "kinds": {kind: kc.as_dict() for kind, kc in kind_counts.items()},
        "documents": [
            {
                "doc_sha256": entry.doc_sha256,
                "source_url": entry.source_url,
                "status": entry.status,
                "reason": entry.reason,
                "indexed_at": entry.indexed_at,
                "chunk_count": entry.chunk_count,
                "extracted_sha256": entry.extracted_sha256,
                "source_kind": entry.source_kind,
            }
            for entry in entries
        ],
    }
    manifest_bytes = (
        json.dumps(manifest_obj, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")

    indexed = sorted(
        (entry for entry in entries if entry.status == "indexed"),
        key=lambda e: (e.source_kind, e.source_url, e.doc_sha256),
    )
    missing_extracted = 0
    surface_lines: list[str] = []
    for entry in indexed:
        title, text, found = _read_extracted(root, entry.doc_sha256)
        if not found:
            missing_extracted += 1
        fragment = urlsplit(entry.source_url).fragment
        line_obj = {
            "doc_sha256": entry.doc_sha256,
            "url": entry.source_url,
            "kind": entry.source_kind,
            "title": title,
            "section": fragment if fragment else None,
            "abstract": _make_abstract(text, abstract_chars) if found else "",
            "chunk_count": entry.chunk_count,
            "indexed_at": entry.indexed_at,
        }
        surface_lines.append(json.dumps(line_obj, ensure_ascii=False, separators=(",", ":")))
    surface_bytes = "".join(line + "\n" for line in surface_lines).encode("utf-8")

    index_dir = out / "index"
    manifest_path = index_dir / "manifest.json"
    surface_path = index_dir / "flop-surface.jsonl"
    readme_path = index_dir / "README.txt"

    readme_text = _README_TEXT.format(schema=SCHEMA_ID)
    if base_url:
        readme_text += (
            "\nOnce published, these are intended to be served at:\n"
            f"  {base_url}/index/manifest.json\n"
            f"  {base_url}/index/flop-surface.jsonl\n"
        )

    _write_atomic(manifest_path, manifest_bytes)
    _write_atomic(surface_path, surface_bytes)
    _write_atomic(readme_path, readme_text.encode("utf-8"))

    return PublishReport(
        documents=len(entries),
        surface_lines=len(surface_lines),
        missing_extracted=missing_extracted,
        manifest_path=str(manifest_path),
        surface_path=str(surface_path),
        db_sha256=db_sha256,
        counts=counts,
    )


def _report_dict(report: PublishReport) -> dict[str, object]:
    """JSON-shaped view of a `PublishReport`, for the CLI's one-line stdout summary."""
    return {
        "documents": report.documents,
        "surface_lines": report.surface_lines,
        "missing_extracted": report.missing_extracted,
        "manifest_path": report.manifest_path,
        "surface_path": report.surface_path,
        "db_sha256": report.db_sha256,
        "counts": report.counts.as_dict(),
    }


def _positive_int(value: str) -> int:
    """argparse `type=`: a positive integer; raises `ArgumentTypeError` otherwise."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {number}")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openagentsearch.pipeline.publish")
    parser.add_argument("--db", required=True, help="SQLite file backing the VectorStore")
    parser.add_argument("--root", required=True, help="root directory holding extracted/ and raw/")
    parser.add_argument("--out", required=True, help="directory to write index/ under")
    parser.add_argument("--abstract-chars", type=_positive_int, default=300)
    parser.add_argument(
        "--base-url",
        default=None,
        help="noted in README.txt as where the export is intended to be published; not fetched",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, build the static export, and print the result.

    On success, prints one compact JSON line (a `PublishReport`, see `_report_dict`) to stdout and
    returns 0. On any failure -- a `--db` path that does not exist, a `--root` that is not an
    existing directory, a corrupt manifest (`ManifestCorruptionError`), or anything else raised
    while building the export -- prints one compact JSON `{"error": "..."}` line to stderr and
    returns 2; nothing is printed to stdout in that case. Argument-parsing failures (a missing
    required flag, a non-positive `--abstract-chars`, ...) exit the process directly with status 2
    via argparse's own behaviour and never reach this function's return statement.
    """
    args = _build_parser().parse_args(argv)
    db_path = Path(args.db)
    root_path = Path(args.root)
    out_path = Path(args.out)

    try:
        if not db_path.is_file():
            raise FileNotFoundError(f"--db not found: {db_path}")
        if not root_path.is_dir():
            raise NotADirectoryError(f"--root not found: {root_path}")
        # dimension is irrelevant here: build_static_index() only reads the manifest table, never
        # the vector rows, so no real embedding dimension is needed to open the store read-only.
        store = VectorStore(db_path, dimension=1)
        try:
            report = build_static_index(
                store=store,
                root=root_path,
                out=out_path,
                abstract_chars=args.abstract_chars,
                base_url=args.base_url,
            )
        finally:
            store.close()
    except Exception as exc:
        print(
            json.dumps({"error": f"{type(exc).__name__}: {exc}"}, separators=(",", ":")),
            file=sys.stderr,
            flush=True,
        )
        return 2

    print(json.dumps(_report_dict(report), ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the subprocess test
    raise SystemExit(main())
