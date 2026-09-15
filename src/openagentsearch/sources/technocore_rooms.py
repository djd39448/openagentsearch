"""Adapter over technocore.chat's room *directory* export (`rooms.jsonl`): one JSON object per
line describing a room's coarse activity shape, never message text.

This is a directory, not a message-content corpus: `rooms.jsonl` carries counts, timestamps and a
classification hint per room, and a small sample of poster DIDs -- never any message body. Rooms
listed here are public rooms only (whatever produced `rooms.jsonl` is responsible for that
filtering; this adapter does not re-derive visibility). The crawler (`bin/crawl.py`) fetches room
messages only to update these records and never persists message text, so this adapter makes no
claim about room content, only about what the directory itself reports.

NOT guaranteed: line reads are bounded to `_MAX_LINE_BYTES` per physical line, but a pathological
input with no newlines at all is not protected against by a hard byte ceiling on total buffered
memory -- this adapter is meant for the crawler's own `rooms.jsonl`, a well-formed line-oriented
file, not arbitrary untrusted input. A blank (whitespace-only) line is skipped silently and is not
counted anywhere in `AdapterStats` (it is not a record). `source_file_sha256` is computed once,
from the file's contents at construction time; if the file changes on disk afterward, the hash is
stale and stats/relative memory usage still reflect what construction saw.
"""

import datetime
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Iterator, TypeGuard

from openagentsearch.sources.base import AdapterStats, SourceDoc

_ROOM_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_CLASSIFICATIONS = ("busy", "active", "sparse", "empty", "unknown")
_MAX_LINE_BYTES = 64 * 1024


def _is_plain_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _iso_utc_or_unknown(ts: object) -> str:
    if ts is None:
        return "unknown"
    if not _is_plain_int(ts):
        raise ValueError(f"timestamp must be an int or None, got {ts!r}")
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _render_room(
    room_id: str,
    classification_hint: str,
    first_seen_ts: object,
    last_activity_ts: object,
    message_count_seen: int,
    last_seq: object,
    sample_from_dids: list[str],
) -> str:
    posters = ", ".join(sample_from_dids) if sample_from_dids else "none"
    last_seq_text = "unknown" if last_seq is None else str(last_seq)
    return (
        f"Room {room_id} on technocore.chat. Classification: {classification_hint}. "
        f"First seen {_iso_utc_or_unknown(first_seen_ts)}; "
        f"last activity {_iso_utc_or_unknown(last_activity_ts)}; "
        f"messages seen {message_count_seen}; last seq {last_seq_text}. "
        f"Sample posters: {posters}."
    )


class RoomDirectoryAdapter:
    """`SourceAdapter` over one `rooms.jsonl` file. `kind = "room"`."""

    name = "technocore_room_directory"
    kind = "room"

    def __init__(
        self,
        path: Path,
        *,
        min_messages: int = 1,
        max_rooms: int = 100_000,
        generated_at: float | None = None,
    ) -> None:
        if min_messages < 0:
            raise ValueError("min_messages must be >= 0")
        if max_rooms <= 0:
            raise ValueError("max_rooms must be > 0")
        self.path = Path(path)
        self.min_messages = min_messages
        self.max_rooms = max_rooms
        self._generated_at = generated_at
        self._file_sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self._read = 0
        self._yielded = 0
        self._skipped_malformed = 0
        self._skipped_filtered = 0
        self._skipped_too_large = 0

    def stats(self) -> AdapterStats:
        """Reflects only the lines processed by `iter_documents()` so far; see the module and
        `AdapterStats` docstrings for what is not counted."""
        return AdapterStats(
            read=self._read,
            yielded=self._yielded,
            skipped_malformed=self._skipped_malformed,
            skipped_filtered=self._skipped_filtered,
            skipped_too_large=self._skipped_too_large,
        )

    def _validate_row(self, row: object) -> dict[str, object] | None:
        if not isinstance(row, dict):
            return None
        room_id = row.get("id")
        classification_hint = row.get("classification_hint")
        message_count_seen = row.get("message_count_seen")
        first_seen_ts = row.get("first_seen_ts")
        last_activity_ts = row.get("last_activity_ts")
        last_seq = row.get("last_seq")
        sample_from_dids = row.get("sample_from_dids")
        ok = (
            isinstance(room_id, str)
            and bool(_ROOM_ID_RE.match(room_id))
            and classification_hint in _CLASSIFICATIONS
            and _is_plain_int(message_count_seen)
            and message_count_seen >= 0
            and (first_seen_ts is None or _is_plain_int(first_seen_ts))
            and (last_activity_ts is None or _is_plain_int(last_activity_ts))
            and (last_seq is None or _is_plain_int(last_seq))
            and isinstance(sample_from_dids, list)
            and all(isinstance(d, str) for d in sample_from_dids)
        )
        return row if ok else None

    def iter_documents(self) -> Iterator[SourceDoc]:
        fetched_at = self._generated_at if self._generated_at is not None else time.time()
        emitted = 0
        with self.path.open("rb") as fh:
            for raw_line in fh:
                if emitted >= self.max_rooms:
                    break
                stripped = raw_line.strip()
                if not stripped:
                    continue  # blank line: not a record, not counted
                self._read += 1
                if len(raw_line) > _MAX_LINE_BYTES:
                    self._skipped_malformed += 1
                    continue
                try:
                    row = json.loads(stripped.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._skipped_malformed += 1
                    continue
                validated = self._validate_row(row)
                if validated is None:
                    self._skipped_malformed += 1
                    continue
                message_count_seen = validated["message_count_seen"]
                assert isinstance(message_count_seen, int)
                if message_count_seen < self.min_messages:
                    self._skipped_filtered += 1
                    continue

                room_id = validated["id"]
                classification_hint = validated["classification_hint"]
                first_seen_ts = validated["first_seen_ts"]
                last_activity_ts = validated["last_activity_ts"]
                last_seq = validated["last_seq"]
                sample_from_dids = validated["sample_from_dids"]
                assert isinstance(room_id, str)
                assert isinstance(classification_hint, str)
                assert isinstance(sample_from_dids, list)

                try:
                    content = _render_room(
                        room_id,
                        classification_hint,
                        first_seen_ts,
                        last_activity_ts,
                        message_count_seen,
                        last_seq,
                        sample_from_dids,
                    )
                except (ValueError, OSError, OverflowError):
                    # a plain int that is nonetheless out of the platform's representable
                    # timestamp range (e.g. datetime.fromtimestamp raising on this OS/Python
                    # build) degrades to one skipped row, not an aborted file.
                    self._skipped_malformed += 1
                    continue
                provenance = tuple(
                    sorted(
                        {
                            "room_id": room_id,
                            "classification_hint": classification_hint,
                            "first_seen_ts": "" if first_seen_ts is None else str(first_seen_ts),
                            "last_activity_ts": (
                                "" if last_activity_ts is None else str(last_activity_ts)
                            ),
                            "last_seq": "" if last_seq is None else str(last_seq),
                            "message_count_seen": str(message_count_seen),
                            "sample_from_dids": ", ".join(sample_from_dids),
                            "source_file_sha256": self._file_sha256,
                            "source_kind": "room",
                        }.items()
                    )
                )
                self._yielded += 1
                emitted += 1
                yield SourceDoc(
                    url=f"https://technocore.chat/r/{room_id}",
                    kind=self.kind,
                    content=content,
                    content_type="text",
                    fetched_at=fetched_at,
                    provenance=provenance,
                    title=f"Room {room_id}",
                )
