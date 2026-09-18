"""scripts/freeze_message_log.py -- freeze a technocore.chat message-log root
(`openagentsearch.sources.technocore_messages.MessageLog`'s on-disk layout, `<log-root>/messages/
<room>.jsonl`) into one immutable, third-party-verifiable snapshot archive, and verify/extract it
again. Stdlib only -- this module imports nothing outside the standard library and nothing from
`src/`, so a downloader can run `verify`/`extract` standalone with no repository checkout beyond
this one file.

Why (`handoff/SN-SPEC.md`): yellowpaper #58's reviewers (retardio73-boop/flop-conformance-lab)
retain an empirical lane as REPRODUCED only when a published `did-ledger.jsonl`/
`did-ledger-compact.json` (`openagentsearch.reputation.build`) ships alongside an immutable input
snapshot, a sha256 of both the compressed and decompressed bytes, a capture manifest (room, seq
range, missing-seq/gap manifest, observed time window), the exact invocation, and the expected
output hash. `reputation.build` is already deterministic given `--now`; this script freezes its
INPUT -- the message log itself -- which has never been packaged before.

Three subcommands:

  freeze  --log-root DIR --out-dir DIR --name NAME [--exclude ROOM]... [--exclude-reason TEXT]
          [--frozen-at ISO8601Z] [--room ID]...
      Writes `NAME.tar.gz` (a deterministic gzip of a deterministic tar: fixed `TarInfo` fields
      and `mtime=0` everywhere, one `messages/<room>.jsonl` member per included room in sorted
      room order, room bytes copied exactly as they sit on disk -- CRLF line endings and all,
      never normalised or re-serialised) and `NAME.manifest.json` (schema, per-room stats --
      byte/line/row counts, seq coverage and gaps, sender/timestamp summaries -- and totals) into
      `--out-dir`. `--room` (repeatable) is an alternative to `--exclude`: it names the exact
      include list rather than an exclude list; giving both is an error. Refuses to overwrite an
      existing output file (nothing is written in that case). Exit 0 and one JSON report line to
      stdout on success; exit 2 (nothing written) for a bad argument, an unreadable log root, or
      an existing output file.

  verify  --archive NAME.tar.gz --manifest NAME.manifest.json
      Recomputes every recomputable field from the archive's own bytes -- gzip sha256, tar
      sha256, every per-room stat, and the totals -- and compares each to the manifest field by
      field; the manifest is never trusted for anything independently checkable. One JSON report
      line to stdout: `{"ok": bool, "mismatches": [...], "rooms": N, "unchecked": [...]}`.
      `unchecked` always names `frozen_at`/`name`/`excluded`/`not_claimed`/`layout`/`row_schema`
      -- fields this command cannot recompute from archive bytes alone, so it never fails on
      them. Exit 0 when `ok`, 1 when not (a field mismatch, an unsafe tar member, a room present
      on only one side), 2 when the archive or manifest cannot even be read or parsed.

  extract  --archive NAME.tar.gz --into DIR
      Safely extracts `messages/<room>.jsonl` for every room member (the SAME tar-member safety
      checks `verify` applies) into `DIR/messages/`, so a reproducer can point
      `openagentsearch.reputation.build --log-root DIR` directly at the result. Refuses if
      `DIR/messages` already exists (nothing is written in that case). One JSON report line to
      stdout on success: `{"extracted": N, "into": DIR}`.

NOT guaranteed: this script proves the archive's bytes are exactly the bytes that went into it,
and recomputes statistics purely from those bytes -- it never verifies a message's `sig` against
its `sender` (nothing in this repository does), never claims a room's captured `seq` range is
complete (the poller keeps only the newest tail-truncation window per sweep; a room's history
before its own `seq_min` was never captured at all, and every manifest's own `not_claimed` field
says so verbatim), and never attributes any identity to any operator. `verify` re-derives
statistics from the tar member bytes it walks -- it does not and cannot check that those bytes
still match whatever now sits on the ORIGINAL operator's disk; that assurance is exactly what the
archive's own published sha256 is for. Memory: each room's on-disk file is read into memory
exactly once, its statistics are computed from those bytes, the same bytes are appended to the
in-memory tar, and the room's bytes are then dropped (see `cmd_freeze`) -- so the peak is one
room's file plus the whole uncompressed tar plus its gzip (a few hundred MB for a ~250 MB log),
never every room's file at once. Nothing here is streamed to disk before the final write.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import sys
import tarfile
import time
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeGuard

SCHEMA = "openagentsearch.message-log-snapshot/1"
ROW_SCHEMA = ["room", "seq", "ts", "sender", "text", "sig", "nonce", "observed_at"]
LAYOUT = (
    "tar members messages/<room>.jsonl, sorted by room; bytes exactly as written by "
    "openagentsearch.sources.technocore_messages.MessageLog (CRLF lines are preserved)"
)
NOT_CLAIMED = (
    "Capture completeness is not claimed: the poller keeps the newest window of each room per "
    "sweep, so absent seq numbers inside a room's range are recorded here as gaps, and a room's "
    "history before its seq_min was never captured. Nothing here attributes identities to "
    "operators."
)
# Fields `verify` cannot recompute from archive bytes alone -- always reported, never failed on.
UNCHECKED_FIELDS = ["frozen_at", "name", "excluded", "not_claimed", "layout", "row_schema"]

# Everything `Path.read_bytes` + `gzip.decompress` raise for an unreadable, non-gzip, corrupted or
# TRUNCATED archive (a half-finished download): `OSError` covers I/O and `gzip.BadGzipFile`, but
# a truncated stream raises `EOFError` and corrupted deflate data raises `zlib.error`, neither of
# which is an `OSError` -- all three must become the documented `{"error": ...}` line + exit 2.
_DECOMPRESS_ERRORS = (OSError, EOFError, zlib.error)

# A snapshot member is `messages/<room>.jsonl` where <room> obeys the SAME room-id rule the log
# writer enforces (`sources.technocore_messages._ROOM_ID_RE`, reimplemented here: stdlib only).
_MEMBER_RE = re.compile(r"^messages/([A-Za-z0-9._-]{1,128})\.jsonl$")
_FROZEN_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _is_plain_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _iso8601_utc(timestamp: float) -> str:
    """`timestamp` (Unix epoch seconds) as a `Z`-suffixed, whole-second ISO-8601 UTC string --
    the same convention `openagentsearch.reputation.ledger._iso8601_utc` uses, reimplemented here
    since this script imports nothing outside the standard library."""
    stamp = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")
    return stamp.replace("+00:00", "Z")


def _frozen_at_type(value: str) -> str:
    """argparse type for `--frozen-at`: must already be `YYYY-MM-DDTHH:MM:SSZ` -- stored in the
    manifest verbatim, never reformatted, so a caller's own value round-trips exactly."""
    if not _FROZEN_AT_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"--frozen-at must look like YYYY-MM-DDTHH:MM:SSZ, got {value!r}"
        )
    return value


# ================================================================================================
# Pure statistics: one room's bytes in, its manifest entry out. No I/O below this line until the
# cmd_* functions.
# ================================================================================================


@dataclass(frozen=True)
class RoomComputation:
    """One room's manifest entry (`to_manifest_dict`), plus the raw `senders` set kept only so
    `_totals` can compute the cross-room union -- `senders` itself is never serialized."""

    room: str
    member: str
    size: int
    sha256: str
    lines: int
    rows: int
    malformed_lines: int
    distinct_seq: int
    seq_min: int | None
    seq_max: int | None
    seq_range: int
    missing_within_range: int
    gap_intervals: tuple[tuple[int, int], ...]
    coverage: float | None
    first_ts: str | None
    last_ts: str | None
    distinct_senders: int
    signed_rows: int
    senders: frozenset[str]

    def to_manifest_dict(self) -> dict[str, object]:
        return {
            "room": self.room,
            "member": self.member,
            "bytes": self.size,
            "sha256": self.sha256,
            "lines": self.lines,
            "rows": self.rows,
            "malformed_lines": self.malformed_lines,
            "distinct_seq": self.distinct_seq,
            "seq_min": self.seq_min,
            "seq_max": self.seq_max,
            "seq_range": self.seq_range,
            "missing_within_range": self.missing_within_range,
            "gap_intervals": [[a, b] for a, b in self.gap_intervals],
            "coverage": self.coverage,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "distinct_senders": self.distinct_senders,
            "signed_rows": self.signed_rows,
        }


def compute_room(room: str, data: bytes) -> RoomComputation:
    """Pure: `room`'s manifest stats from its exact on-disk bytes `data` -- the SAME bytes that
    get written into the archive's tar member (see `_build_archive`) and, on `verify`, the SAME
    bytes read back out of an existing tar member. Never reads or writes anything itself.

    A "row" is a physical line that parses as a JSON object with a plain (non-bool) int `"seq"`
    field -- nothing more is required of it. Every other physical line (blank, invalid JSON, not
    an object, no int `seq`) counts as one of `malformed_lines`; `rows + malformed_lines ==
    lines` always holds. `gap_intervals` are the ascending, inclusive runs of int values strictly
    between the smallest and largest distinct `seq` seen that were never themselves seen.
    """
    sha256 = hashlib.sha256(data).hexdigest()
    physical_lines = data.splitlines()
    malformed_lines = 0
    rows = 0
    seqs: set[int] = set()
    ts_values: list[str] = []
    senders: set[str] = set()
    signed_rows = 0

    for raw_line in physical_lines:
        stripped = raw_line.strip()
        if not stripped:
            malformed_lines += 1
            continue
        try:
            obj: Any = json.loads(stripped.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed_lines += 1
            continue
        if not isinstance(obj, dict) or not _is_plain_int(obj.get("seq")):
            malformed_lines += 1
            continue
        rows += 1
        seqs.add(obj["seq"])
        ts = obj.get("ts")
        if isinstance(ts, str):
            ts_values.append(ts)
        sender = obj.get("sender")
        if isinstance(sender, str):
            senders.add(sender)
        sig = obj.get("sig")
        if isinstance(sig, str) and sig != "":
            signed_rows += 1

    sorted_seqs = sorted(seqs)
    seq_min = sorted_seqs[0] if sorted_seqs else None
    seq_max = sorted_seqs[-1] if sorted_seqs else None
    seq_range = (seq_max - seq_min + 1) if seq_min is not None and seq_max is not None else 0
    distinct_seq = len(sorted_seqs)
    missing_within_range = seq_range - distinct_seq
    gap_intervals = tuple(
        (prev + 1, curr - 1)
        for prev, curr in zip(sorted_seqs, sorted_seqs[1:])
        if curr - prev > 1
    )
    coverage = round(distinct_seq / seq_range, 6) if seq_range > 0 else None

    return RoomComputation(
        room=room,
        member=f"messages/{room}.jsonl",
        size=len(data),
        sha256=sha256,
        lines=len(physical_lines),
        rows=rows,
        malformed_lines=malformed_lines,
        distinct_seq=distinct_seq,
        seq_min=seq_min,
        seq_max=seq_max,
        seq_range=seq_range,
        missing_within_range=missing_within_range,
        gap_intervals=gap_intervals,
        coverage=coverage,
        first_ts=min(ts_values) if ts_values else None,
        last_ts=max(ts_values) if ts_values else None,
        distinct_senders=len(senders),
        signed_rows=signed_rows,
        senders=frozenset(senders),
    )


def _totals(computations: Sequence[RoomComputation]) -> dict[str, object]:
    """The manifest `totals` object: sums across `computations`, except `distinct_senders`
    (the cross-room UNION, so a sender posting in two included rooms is counted once) and
    `first_ts`/`last_ts` (min/max across rooms' own `first_ts`/`last_ts`, lexicographic)."""
    all_senders: set[str] = set()
    first_ts_values: list[str] = []
    last_ts_values: list[str] = []
    for c in computations:
        all_senders |= c.senders
        if c.first_ts is not None:
            first_ts_values.append(c.first_ts)
        if c.last_ts is not None:
            last_ts_values.append(c.last_ts)
    return {
        "rooms": len(computations),
        "bytes": sum(c.size for c in computations),
        "lines": sum(c.lines for c in computations),
        "rows": sum(c.rows for c in computations),
        "malformed_lines": sum(c.malformed_lines for c in computations),
        "distinct_senders": len(all_senders),
        "first_ts": min(first_ts_values) if first_ts_values else None,
        "last_ts": max(last_ts_values) if last_ts_values else None,
    }


def _build_manifest(
    *,
    name: str,
    frozen_at: str,
    computations: list[RoomComputation],
    excluded: list[dict[str, str]],
    archive_file: str,
    gzip_sha256: str,
    gzip_bytes: int,
    tar_sha256: str,
    tar_bytes: int,
) -> dict[str, object]:
    """Pure assembly of the manifest object (`computations` already sorted by room -- see
    `cmd_freeze`); serialization is `_manifest_bytes`, not this function."""
    return {
        "schema": SCHEMA,
        "name": name,
        "frozen_at": frozen_at,
        "row_schema": list(ROW_SCHEMA),
        "layout": LAYOUT,
        "archive": {
            "file": archive_file,
            "gzip_sha256": gzip_sha256,
            "gzip_bytes": gzip_bytes,
            "tar_sha256": tar_sha256,
            "tar_bytes": tar_bytes,
        },
        "rooms": [c.to_manifest_dict() for c in computations],
        "totals": _totals(computations),
        "excluded": excluded,
        "not_claimed": NOT_CLAIMED,
    }


def _manifest_bytes(manifest: dict[str, object]) -> bytes:
    """`manifest` -> its canonical on-disk bytes: `sort_keys=True`, `indent=2`,
    `ensure_ascii=False`, a single trailing LF (LF only, never CRLF)."""
    text = json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def _add_member(tar: tarfile.TarFile, member: str, data: bytes) -> None:
    """Append `data` to `tar` as the regular-file member `member` with every header field fixed
    (`mtime`/`uid`/`gid` 0, empty `uname`/`gname`, mode 0o644), so the tar bytes depend on nothing
    but the member names, their order and their contents."""
    info = tarfile.TarInfo(name=member)
    info.size = len(data)
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    tar.addfile(info, io.BytesIO(data))


def _gzip_deterministic(tar_bytes: bytes) -> bytes:
    """gzip `tar_bytes` with `mtime=0`, no stored filename and the default (maximum) compression
    level. Deterministic for a given zlib; the published hash is what a downloader checks, and
    `verify` compares the DEcompressed tar's hash as well, which does not depend on zlib at all."""
    gzip_buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=gzip_buffer, mode="wb", mtime=0, filename="") as gz:
        gz.write(tar_bytes)
    return gzip_buffer.getvalue()


def _build_archive(
    rooms: Sequence[str], read_room: Callable[[str], bytes]
) -> tuple[list[RoomComputation], bytes, bytes]:
    """`(computations, tar_bytes, gzip_bytes)` for `rooms` in the given (room-sorted) order.
    Each room's bytes come from ONE `read_room(room)` call, feed `compute_room` and the tar member
    in that order, and are dropped before the next room is read. Deterministic: two calls with
    the same rooms and bytes always produce byte-identical output (a test proves it)."""
    computations: list[RoomComputation] = []
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for room in rooms:
            data = read_room(room)
            computation = compute_room(room, data)
            computations.append(computation)
            _add_member(tar, computation.member, data)
            del data
    tar_bytes = tar_buffer.getvalue()
    del tar_buffer
    return computations, tar_bytes, _gzip_deterministic(tar_bytes)


class UnsafeMemberError(Exception):
    """Raised by `_walk_tar_safely` for the first tar member that fails the safety checks
    (absolute path, `..`, a link, a directory, or a name outside `messages/*.jsonl`)."""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"{name}: {reason}")
        self.name = name
        self.reason = reason


def _member_safety_reason(info: tarfile.TarInfo) -> str | None:
    """`None` if `info` is safe to treat as one snapshot's `messages/<room>.jsonl` member, else
    the reason it is refused."""
    name = info.name
    if name.startswith("/") or name.startswith("\\") or (len(name) > 1 and name[1] == ":"):
        return "absolute path"
    if "\\" in name:
        return "backslash in member name"
    if ".." in name.split("/"):
        return "path traversal"
    if info.issym() or info.islnk():
        return "link member"
    if info.isdir():
        return "directory member"
    if not info.isreg():
        return "not a regular file"
    match = _MEMBER_RE.match(name)
    if match is None:
        return "not a messages/<room>.jsonl member"
    if match.group(1) in (".", ".."):
        return "path traversal"
    return None


def _room_of_member(name: str) -> str:
    """The `<room>` of an already safety-checked `messages/<room>.jsonl` member name."""
    return name[len("messages/") : -len(".jsonl")]


def _walk_tar_safely(tar_bytes: bytes) -> list[RoomComputation]:
    """Every `messages/<room>.jsonl` member of `tar_bytes`, safety-checked and recomputed
    (`compute_room`) from its own member bytes -- never a second read of any original disk file.
    Raises `UnsafeMemberError`, naming it, on the FIRST unsafe member found, rather than silently
    skipping it. Returned sorted by room."""
    computations: list[RoomComputation] = []
    seen: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
        for info in tar.getmembers():
            reason = _member_safety_reason(info)
            if reason is not None:
                raise UnsafeMemberError(info.name, reason)
            if info.name in seen:
                raise UnsafeMemberError(info.name, "duplicate member")
            seen.add(info.name)
            extracted = tar.extractfile(info)
            if extracted is None:
                raise UnsafeMemberError(info.name, "unreadable member")
            data = extracted.read()
            computations.append(compute_room(_room_of_member(info.name), data))
    computations.sort(key=lambda c: c.room)
    return computations


# ================================================================================================
# Subcommands: I/O and argument parsing live here, nowhere above.
# ================================================================================================


def cmd_freeze(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="freeze_message_log.py freeze")
    parser.add_argument("--log-root", required=True, dest="log_root")
    parser.add_argument("--out-dir", required=True, dest="out_dir")
    parser.add_argument("--name", required=True)
    parser.add_argument("--exclude", action="append", default=[], dest="exclude")
    parser.add_argument("--exclude-reason", default=None, dest="exclude_reason")
    parser.add_argument("--frozen-at", default=None, dest="frozen_at", type=_frozen_at_type)
    parser.add_argument("--room", action="append", default=None, dest="rooms")
    args = parser.parse_args(argv)

    exclude: list[str] = list(args.exclude)
    if args.rooms is not None and exclude:
        print(
            json.dumps({"error": "--room and --exclude are mutually exclusive"}), file=sys.stderr
        )
        return 2

    log_root = Path(args.log_root)
    messages_dir = log_root / "messages"
    all_rooms = (
        sorted(p.stem for p in messages_dir.glob("*.jsonl")) if messages_dir.is_dir() else []
    )
    all_rooms_set = set(all_rooms)

    if args.rooms is not None:
        include_rooms = sorted(set(args.rooms))
        missing = [r for r in include_rooms if r not in all_rooms_set]
        if missing:
            print(
                json.dumps({"error": f"--room names a room with no file: {sorted(missing)}"}),
                file=sys.stderr,
            )
            return 2
    else:
        missing_exclude = sorted(r for r in set(exclude) if r not in all_rooms_set)
        if missing_exclude:
            print(
                json.dumps(
                    {"error": f"--exclude names a room with no file: {missing_exclude}"}
                ),
                file=sys.stderr,
            )
            return 2
        include_rooms = [r for r in all_rooms if r not in set(exclude)]

    if not include_rooms:
        print(json.dumps({"error": "no rooms to include"}), file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    archive_path = out_dir / f"{args.name}.tar.gz"
    manifest_path = out_dir / f"{args.name}.manifest.json"
    if archive_path.exists() or manifest_path.exists():
        print(json.dumps({"error": "refusing to overwrite existing output"}), file=sys.stderr)
        return 2

    frozen_at = args.frozen_at if args.frozen_at is not None else _iso8601_utc(time.time())

    # One room's bytes are read exactly once, feed its stats and its tar member, then are dropped
    # (see `_build_archive`) -- never every room's raw file in memory at the same time.
    computations, tar_bytes, gzip_bytes = _build_archive(
        include_rooms, lambda room: (messages_dir / f"{room}.jsonl").read_bytes()
    )
    tar_sha256 = hashlib.sha256(tar_bytes).hexdigest()
    gzip_sha256 = hashlib.sha256(gzip_bytes).hexdigest()

    exclude_reason = (
        args.exclude_reason if args.exclude_reason is not None else "excluded by operator"
    )
    excluded_rooms = sorted(r for r in all_rooms if r not in set(include_rooms))
    excluded = [{"room": r, "reason": exclude_reason} for r in excluded_rooms]

    manifest = _build_manifest(
        name=args.name,
        frozen_at=frozen_at,
        computations=computations,
        excluded=excluded,
        archive_file=f"{args.name}.tar.gz",
        gzip_sha256=gzip_sha256,
        gzip_bytes=len(gzip_bytes),
        tar_sha256=tar_sha256,
        tar_bytes=len(tar_bytes),
    )
    manifest_bytes = _manifest_bytes(manifest)

    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(gzip_bytes)
    manifest_path.write_bytes(manifest_bytes)

    print(
        json.dumps(
            {
                "archive": str(archive_path),
                "manifest": str(manifest_path),
                "rooms": len(computations),
                "rows": sum(c.rows for c in computations),
                "gzip_sha256": gzip_sha256,
                "tar_sha256": tar_sha256,
            }
        )
    )
    return 0


def cmd_verify(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="freeze_message_log.py verify")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)

    archive_path = Path(args.archive)
    manifest_path = Path(args.manifest)

    try:
        gzip_bytes = archive_path.read_bytes()
        manifest_raw: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"cannot read input: {exc}"}), file=sys.stderr)
        return 2
    if not isinstance(manifest_raw, dict):
        print(json.dumps({"error": "manifest is not a JSON object"}), file=sys.stderr)
        return 2
    manifest: dict[str, Any] = manifest_raw

    gzip_sha256 = hashlib.sha256(gzip_bytes).hexdigest()
    try:
        tar_bytes = gzip.decompress(gzip_bytes)
    except _DECOMPRESS_ERRORS as exc:
        print(json.dumps({"error": f"cannot decompress archive: {exc}"}), file=sys.stderr)
        return 2
    tar_sha256 = hashlib.sha256(tar_bytes).hexdigest()

    try:
        computations = _walk_tar_safely(tar_bytes)
    except UnsafeMemberError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "mismatches": [f"tar member {exc.name!r}: refused ({exc.reason})"],
                    "rooms": 0,
                    "unchecked": UNCHECKED_FIELDS,
                }
            )
        )
        return 1
    except tarfile.TarError as exc:
        print(json.dumps({"error": f"cannot read tar: {exc}"}), file=sys.stderr)
        return 2

    mismatches: list[str] = []

    archive_obj = manifest.get("archive")
    if not isinstance(archive_obj, dict):
        mismatches.append("archive")
    else:
        for field, actual in (
            ("gzip_sha256", gzip_sha256),
            ("gzip_bytes", len(gzip_bytes)),
            ("tar_sha256", tar_sha256),
            ("tar_bytes", len(tar_bytes)),
        ):
            if archive_obj.get(field) != actual:
                mismatches.append(f"archive.{field}")

    manifest_rooms_raw = manifest.get("rooms")
    manifest_rooms: list[Any] = manifest_rooms_raw if isinstance(manifest_rooms_raw, list) else []
    if not isinstance(manifest_rooms_raw, list):
        mismatches.append("rooms")

    recomputed_by_room = {c.room: c for c in computations}
    manifest_room_names: set[str] = set()

    for index, manifest_room in enumerate(manifest_rooms):
        if not isinstance(manifest_room, dict) or not isinstance(manifest_room.get("room"), str):
            mismatches.append(f"rooms[{index}]")
            continue
        room = manifest_room["room"]
        manifest_room_names.add(room)
        recomputed = recomputed_by_room.get(room)
        if recomputed is None:
            mismatches.append(f"rooms[{index}]: {room!r} not present in archive")
            continue
        for field, expected in recomputed.to_manifest_dict().items():
            if manifest_room.get(field) != expected:
                mismatches.append(f"rooms[{index}].{field}")

    for room in sorted(set(recomputed_by_room) - manifest_room_names):
        mismatches.append(f"rooms: {room!r} present in archive but not in manifest")

    manifest_totals = manifest.get("totals")
    recomputed_totals = _totals(computations)
    if not isinstance(manifest_totals, dict):
        mismatches.append("totals")
    else:
        for field, expected in recomputed_totals.items():
            if manifest_totals.get(field) != expected:
                mismatches.append(f"totals.{field}")

    ok = not mismatches
    print(
        json.dumps(
            {
                "ok": ok,
                "mismatches": mismatches,
                "rooms": len(computations),
                "unchecked": UNCHECKED_FIELDS,
            }
        )
    )
    return 0 if ok else 1


def cmd_extract(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="freeze_message_log.py extract")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--into", required=True)
    args = parser.parse_args(argv)

    archive_path = Path(args.archive)
    into_dir = Path(args.into)
    messages_out = into_dir / "messages"
    if messages_out.exists():
        print(
            json.dumps({"error": f"refusing to overwrite existing {messages_out}"}),
            file=sys.stderr,
        )
        return 2

    try:
        gzip_bytes = archive_path.read_bytes()
        tar_bytes = gzip.decompress(gzip_bytes)
    except _DECOMPRESS_ERRORS as exc:
        print(json.dumps({"error": f"cannot read archive: {exc}"}), file=sys.stderr)
        return 2

    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
            members = tar.getmembers()
            seen: set[str] = set()
            for info in members:
                reason = _member_safety_reason(info)
                if reason is None and info.name in seen:
                    reason = "duplicate member"
                if reason is not None:
                    print(
                        json.dumps({"error": f"unsafe tar member {info.name!r}: {reason}"}),
                        file=sys.stderr,
                    )
                    return 1
                seen.add(info.name)
            extracted_files: dict[str, bytes] = {}
            for info in members:
                extracted = tar.extractfile(info)
                if extracted is None:
                    print(
                        json.dumps({"error": f"unreadable tar member {info.name!r}"}),
                        file=sys.stderr,
                    )
                    return 1
                extracted_files[info.name] = extracted.read()
    except tarfile.TarError as exc:
        print(json.dumps({"error": f"cannot read tar: {exc}"}), file=sys.stderr)
        return 2

    messages_out.mkdir(parents=True)
    for name, data in extracted_files.items():
        (messages_out / f"{_room_of_member(name)}.jsonl").write_bytes(data)

    print(json.dumps({"extracted": len(extracted_files), "into": str(into_dir)}))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    all_argv = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: freeze_message_log.py {freeze,verify,extract} ... (each takes --help)"
    if all_argv and all_argv[0] in ("-h", "--help"):
        print(usage)
        return 0
    if not all_argv or all_argv[0] not in ("freeze", "verify", "extract"):
        print(json.dumps({"error": usage}), file=sys.stderr)
        return 2
    command, rest = all_argv[0], all_argv[1:]
    if command == "freeze":
        return cmd_freeze(rest)
    if command == "verify":
        return cmd_verify(rest)
    return cmd_extract(rest)


if __name__ == "__main__":
    raise SystemExit(main())
