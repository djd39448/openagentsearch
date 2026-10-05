"""The published form of the full reputation ledger: `did-ledger/` (package LS, 2026-10-05).

The single `did-ledger.jsonl` grew with every identity ever seen -- 47.7 MB on 2026-10-05 and
about 3 MB a day (some 4,000 new DIDs a day) -- so it was days from its 64 MiB guard and from
GitHub's 50 MB file warning, and gzip alone would only have moved the wall by months. The
published copy is therefore split into gzip shards keyed by when each DID was FIRST seen:

    did-ledger/index.json          the ledger header, the whole file's size and sha256, one entry
                                   per shard (rows, raw and gzip sizes and sha256s)
    did-ledger/<YYYY>-W<ww>.jsonl.gz   rows whose facts.first_seen_ts falls in that ISO week (UTC)
    did-ledger/<YYYY-MM-DD>.jsonl.gz   ...or that UTC day, for a week too big for one shard

Each shard holds the ledger's own row lines, byte for byte and in ledger order, so its size is
bounded by how many DIDs appear in one week (one day for a split week), not by history: a shard
keeps changing while its DIDs keep posting, but no file grows without bound. `join_shards`
rebuilds the exact `did-ledger.jsonl` bytes and checks them against `index.json`'s sha256, so a
reproducer downloads the directory, joins it and compares hashes exactly as before.

gzip output is deterministic on one machine (`mtime=0`, no file name) but may differ across zlib
builds, so reproduction claims rest on `raw_sha256` and `ledger_sha256`; `gz_sha256` is a
download-integrity check only.

CLI: `python -m openagentsearch.reputation.shards split --ledger FILE --out-dir DIR
[--max-shard-bytes N]`, `join --dir DIR --out FILE`, `verify --dir DIR`. Exit 0 ok, 1 failure
(one JSON error line on stderr), 2 bad arguments.
"""

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

SCHEMA_SHARDS = "openagentsearch.did-ledger-shards/1"
INDEX_NAME = "index.json"
DEFAULT_MAX_SHARD_BYTES = 32 * 1024 * 1024


class ShardSizeError(ValueError):
    """A single day's shard is over the per-shard limit even after its week was split."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _gzip(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=9, mtime=0)


def _week_key(ts: float) -> str:
    year, week, _ = dt.datetime.fromtimestamp(ts, dt.UTC).isocalendar()
    return f"{year}-W{week:02d}"


def _day_key(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d")


def _dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _rows(ledger_bytes: bytes) -> tuple[dict[str, Any], list[tuple[str, float, bytes]]]:
    """(header, [(did, first_seen_ts, line_bytes_with_newline)]) from canonical ledger bytes."""
    if not ledger_bytes.endswith(b"\n"):
        raise ValueError("ledger bytes must end with a newline (ledger.to_jsonl_bytes output)")
    lines = ledger_bytes[:-1].split(b"\n")
    header = json.loads(lines[0])
    if not isinstance(header, dict) or header.get("schema") != "openagentsearch.did-ledger/1":
        raise ValueError("first line is not an openagentsearch.did-ledger/1 header")
    rows = []
    for raw in lines[1:]:
        obj = json.loads(raw)
        rows.append((obj["did"], float(obj["facts"]["first_seen_ts"]), raw + b"\n"))
    if len(rows) != header.get("dids"):
        raise ValueError(f"header dids={header.get('dids')} does not match {len(rows)} rows")
    return header, rows


def split_ledger(ledger_bytes: bytes, *, max_shard_bytes: int = DEFAULT_MAX_SHARD_BYTES
                 ) -> dict[str, bytes]:
    """`{file name: bytes}` for the whole `did-ledger/` directory, `index.json` included.
    Deterministic for given input bytes (and zlib build). Rows keep ledger order inside a shard.

    Raises:
        ShardSizeError: one UTC day's DIDs gzip to more than `max_shard_bytes`.
    """
    header, rows = _rows(ledger_bytes)
    groups: dict[str, list[tuple[str, float, bytes]]] = {}
    for row in rows:
        groups.setdefault(_week_key(row[1]), []).append(row)
    files: dict[str, bytes] = {}
    entries: list[dict[str, Any]] = []

    def emit(key: str, members: list[tuple[str, float, bytes]], split_from: str | None) -> bool:
        raw = b"".join(line for _did, _ts, line in members)
        gz = _gzip(raw)
        if len(gz) > max_shard_bytes:
            return False
        name = f"{key}.jsonl.gz"
        files[name] = gz
        entry: dict[str, Any] = {"name": name, "key": key, "rows": len(members),
                                 "raw_bytes": len(raw), "raw_sha256": _sha256(raw),
                                 "gz_bytes": len(gz), "gz_sha256": _sha256(gz)}
        if split_from is not None:
            entry["split_from"] = split_from
        entries.append(entry)
        return True

    for week in sorted(groups):
        if emit(week, groups[week], None):
            continue
        days: dict[str, list[tuple[str, float, bytes]]] = {}
        for row in groups[week]:
            days.setdefault(_day_key(row[1]), []).append(row)
        for day in sorted(days):
            if not emit(day, days[day], week):
                raise ShardSizeError(
                    f"DIDs first seen on {day} gzip to more than {max_shard_bytes} bytes; raise "
                    f"--max-shard-bytes or shard finer")
    index = {
        "schema": SCHEMA_SHARDS,
        "header": header,
        "ledger_bytes": len(ledger_bytes),
        "ledger_sha256": _sha256(ledger_bytes),
        "shard_key": "facts.first_seen_ts, ISO week (UTC); a week over max_shard_bytes is split by UTC day",
        "max_shard_bytes": max_shard_bytes,
        "shards": sorted(entries, key=lambda e: e["key"]),
    }
    files[INDEX_NAME] = _dumps(index) + b"\n"
    return files


def join_shards(directory: Path) -> bytes:
    """The exact `did-ledger.jsonl` bytes rebuilt from a `did-ledger/` directory, verified against
    every raw shard hash and the whole-file hash in `index.json`.

    Raises:
        ValueError: anything missing, extra, tampered or inconsistent.
    """
    directory = Path(directory)
    index = json.loads((directory / INDEX_NAME).read_bytes())
    if index.get("schema") != SCHEMA_SHARDS:
        raise ValueError(f"{INDEX_NAME} schema must be {SCHEMA_SHARDS!r}")
    listed = {e["name"] for e in index["shards"]}
    on_disk = {p.name for p in directory.iterdir() if p.name != INDEX_NAME}
    if on_disk != listed:
        raise ValueError(f"shard files on disk differ from {INDEX_NAME}: "
                         f"missing {sorted(listed - on_disk)}, extra {sorted(on_disk - listed)}")
    lines: list[tuple[str, bytes]] = []
    for entry in index["shards"]:
        gz = (directory / entry["name"]).read_bytes()
        raw = gzip.decompress(gz)
        if _sha256(raw) != entry["raw_sha256"] or len(raw) != entry["raw_bytes"]:
            raise ValueError(f"{entry['name']}: content does not match {INDEX_NAME}")
        shard_lines = raw.split(b"\n")[:-1] if raw else []
        if len(shard_lines) != entry["rows"]:
            raise ValueError(f"{entry['name']}: {len(shard_lines)} rows, {INDEX_NAME} says {entry['rows']}")
        for line in shard_lines:
            lines.append((json.loads(line)["did"], line))
    lines.sort(key=lambda x: x[0])
    data = _dumps(index["header"]) + b"\n" + b"".join(line + b"\n" for _did, line in lines)
    if _sha256(data) != index["ledger_sha256"] or len(data) != index["ledger_bytes"]:
        raise ValueError("joined ledger does not match ledger_sha256 in index.json")
    return data


def write_shards(ledger_bytes: bytes, out_dir: Path, *,
                 max_shard_bytes: int = DEFAULT_MAX_SHARD_BYTES) -> dict[str, Any]:
    """Split `ledger_bytes` and replace `out_dir` with the result: everything is written to a
    fresh sibling directory first and checked with `join_shards`, then swapped in, so a reader
    never sees a half-written set and a removed shard name does not linger. Returns `index.json`'s
    content.

    Raises:
        ShardSizeError: see `split_ledger`. Raised BEFORE anything is written.
    """
    files = split_ledger(ledger_bytes, max_shard_bytes=max_shard_bytes)
    out_dir = Path(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=str(out_dir.parent), prefix=f".{out_dir.name}."))
    try:
        for name, data in files.items():
            (staging / name).write_bytes(data)
        if join_shards(staging) != ledger_bytes:
            raise ValueError("shards written to the staging directory do not join back to the ledger")
        old = None
        if out_dir.exists():
            old = out_dir.with_name(f".{out_dir.name}.old")
            if old.exists():
                shutil.rmtree(old)
            os.replace(out_dir, old)
        os.replace(staging, out_dir)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    result: dict[str, Any] = json.loads(files[INDEX_NAME])
    return result


def summary(index: dict[str, Any]) -> dict[str, Any]:
    """A short report of an `index.json` for CLI output."""
    shards = index["shards"]
    return {"shards": len(shards), "dids": index["header"]["dids"],
            "ledger_bytes": index["ledger_bytes"], "ledger_sha256": index["ledger_sha256"],
            "gz_bytes": sum(e["gz_bytes"] for e in shards),
            "largest_shard_gz_bytes": max((e["gz_bytes"] for e in shards), default=0)}


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {parsed}")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openagentsearch.reputation.shards")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("split", help="did-ledger.jsonl -> did-ledger/ directory")
    sp.add_argument("--ledger", required=True)
    sp.add_argument("--out-dir", required=True)
    sp.add_argument("--max-shard-bytes", type=_positive_int, default=DEFAULT_MAX_SHARD_BYTES)
    jp = sub.add_parser("join", help="did-ledger/ directory -> the exact did-ledger.jsonl")
    jp.add_argument("--dir", required=True)
    jp.add_argument("--out", required=True)
    vp = sub.add_parser("verify", help="check a did-ledger/ directory joins to its recorded hash")
    vp.add_argument("--dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "split":
            index = write_shards(Path(args.ledger).read_bytes(), Path(args.out_dir),
                                 max_shard_bytes=args.max_shard_bytes)
            report = summary(index)
        elif args.cmd == "join":
            data = join_shards(Path(args.dir))
            Path(args.out).write_bytes(data)
            report = {"path": args.out, "bytes": len(data), "ledger_sha256": _sha256(data)}
        else:
            join_shards(Path(args.dir))
            report = summary(json.loads((Path(args.dir) / INDEX_NAME).read_bytes()))
            report["verified"] = True
    except Exception as exc:  # noqa: BLE001 - the CLI's contract: any failure is one JSON error line, exit 1
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1
    print(json.dumps(report, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the subprocess test
    raise SystemExit(main())
