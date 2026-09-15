"""technocore.chat message log: pure parsing of one room's message-page JSON, the append-only
on-disk log built from those parsed pages, room selection over the room *directory*
(`rooms.jsonl`), and a `SourceAdapter` over the logged messages themselves.

The live service answers `GET https://technocore.chat/r/<room>?format=json[&since=<seq>&limit=200]`
with `{"room","count","first_seq","last_seq","generation","messages":[...]}` -- the newest at most
200 messages whose `seq` is greater than `since`, tail-truncated. History older than that
truncation point is unrecoverable from a single request, so a log built by this module is
forward-only from the day it starts: `parse_room_page` turns one such response body into a typed,
`seq`-sorted `RoomPage`, and `MessageLog.append` writes only the genuinely new messages from a
`RoomPage` to `root/messages/<room>.jsonl`, tracking each room's high-water mark and any detected
truncation gaps in `root/message-log-state.json`. Those two locations are the only files this
module ever writes.

NOT guaranteed: `sig` and `nonce` are stored verbatim and shape-validated (a string, possibly
empty) but never cryptographically verified anywhere in this module -- a `RoomMessage`'s `sig`
matching its `sender` is not checked; a recorded gap means the server's own reported `first_seq`
for a poll outran this log's `last_seq` for that room, which is recorded once and never
retroactively resolved -- the messages a gap covers are gone, not merely delayed; a busy room can
still silently outrun polling if the server's tail-truncation window is smaller than the room's
throughput between two polls, in which case the gap is recorded but the missing text itself is
unrecoverable; `p-*` room ids are private by convention and this module refuses to select one
explicitly, but nothing here can stop a caller from constructing a `RoomMessage`/`RoomPage` for
one directly -- the refusal lives in `select_rooms` and the CLI, not in the dataclasses themselves.
"""

import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence, TypeGuard

from openagentsearch.sources.base import AdapterStats, SourceDoc

_ROOM_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_MAX_PAYLOAD_BYTES = 5_000_000
_SCHEMA = "openagentsearch.message-log/1"


def _is_plain_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_int(value: object) -> int:
    """`value` as a plain int, else `ValueError` (the message-item skip path)."""
    if not _is_plain_int(value):
        raise ValueError(f"expected an int, got {value!r}")
    return value


def _as_str(value: object) -> str:
    """`value` as a str, else `ValueError` (the message-item skip path)."""
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {value!r}")
    return value


def _opt_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if not _is_plain_int(value):
        raise ValueError(f"{field!r} must be an int or null, got {value!r}")
    return value


@dataclass(frozen=True)
class RoomMessage:
    """One message as recorded on `<room>.jsonl`. `sig`/`nonce` may be `""` -- messages logged
    before technocore.chat 0.11.0 carry no signature at all -- and `text` may be empty.

    NOT guaranteed: nothing here verifies `sig` against `sender`; it is shape-validated (a
    string) and stored, never cryptographically checked.
    """

    room: str
    seq: int
    ts: str
    sender: str
    text: str
    sig: str
    nonce: str
    observed_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.room, str) or not _ROOM_ID_RE.match(self.room):
            raise ValueError(f"room must match {_ROOM_ID_RE.pattern!r}, got {self.room!r}")
        if not _is_plain_int(self.seq) or self.seq < 0:
            raise ValueError(f"seq must be a non-negative int, got {self.seq!r}")
        if not isinstance(self.ts, str) or not self.ts:
            raise ValueError("ts must be a non-empty string")
        if not isinstance(self.sender, str) or not self.sender:
            raise ValueError("sender must be a non-empty string")
        if not isinstance(self.text, str):
            raise ValueError(f"text must be a string, got {self.text!r}")
        if not isinstance(self.sig, str):
            raise ValueError(f"sig must be a string, got {self.sig!r}")
        if not isinstance(self.nonce, str):
            raise ValueError(f"nonce must be a string, got {self.nonce!r}")
        if isinstance(self.observed_at, bool) or not isinstance(self.observed_at, (int, float)):
            raise ValueError(f"observed_at must be a number, got {self.observed_at!r}")
        if not math.isfinite(self.observed_at):
            raise ValueError(f"observed_at must be finite, got {self.observed_at!r}")
        object.__setattr__(self, "observed_at", float(self.observed_at))


@dataclass(frozen=True)
class RoomPage:
    """One parsed `GET /r/<room>?format=json...` response: `messages` sorted ascending by `seq`.

    `first_seq`/`last_seq` are the server's own reported bounds for the room's known message
    range (not merely the bounds of `messages`), which is what lets `MessageLog.append` detect a
    truncation gap even on a page that happens to carry zero new messages this poll.
    `skipped_malformed` counts message items from the response that could not be parsed into a
    `RoomMessage` at all; it never causes `parse_room_page` itself to raise.
    """

    room: str
    first_seq: int | None
    last_seq: int | None
    messages: tuple[RoomMessage, ...]
    skipped_malformed: int


def parse_room_page(payload: bytes, *, room: str, observed_at: float) -> RoomPage:
    """Strict, pure parse of one room-page JSON body.

    Raises `ValueError` for: a payload over `_MAX_PAYLOAD_BYTES` (5,000,000 bytes), invalid JSON
    or a non-object top level, a `room` field that does not equal the requested `room`, a
    malformed `first_seq`/`last_seq` (present and not an int), or a `messages` field that is not
    a list -- these are all structural problems with the response as a whole, not with any one
    message. A single malformed message item (missing/mistyped `seq`/`ts`/`from`/`text`, or a
    `sig`/`nonce` present but not a string) is skipped and counted in the returned
    `RoomPage.skipped_malformed` instead -- this function never raises because of one bad
    message. `sig`/`nonce` absent (or explicitly `null`) on an item default to `""`.
    """
    if len(payload) > _MAX_PAYLOAD_BYTES:
        raise ValueError(f"room page payload exceeds {_MAX_PAYLOAD_BYTES} bytes")
    try:
        data: Any = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON room page payload: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"room page payload must be a JSON object, got {type(data).__name__}")

    payload_room = data.get("room")
    if payload_room != room:
        raise ValueError(f"payload room {payload_room!r} does not match requested room {room!r}")
    first_seq = _opt_int(data.get("first_seq"), "first_seq")
    last_seq = _opt_int(data.get("last_seq"), "last_seq")
    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("room page payload 'messages' must be a list")

    messages: list[RoomMessage] = []
    skipped_malformed = 0
    for item in raw_messages:
        if not isinstance(item, dict):
            skipped_malformed += 1
            continue
        sig_raw = item.get("sig", "")
        nonce_raw = item.get("nonce", "")
        try:
            message = RoomMessage(
                room=room,
                seq=_as_int(item.get("seq")),
                ts=_as_str(item.get("ts")),
                sender=_as_str(item.get("from")),
                text=_as_str(item.get("text")),
                sig="" if sig_raw is None else _as_str(sig_raw),
                nonce="" if nonce_raw is None else _as_str(nonce_raw),
                observed_at=observed_at,
            )
        except (ValueError, TypeError):
            skipped_malformed += 1
            continue
        messages.append(message)

    messages.sort(key=lambda m: m.seq)
    return RoomPage(
        room=room,
        first_seq=first_seq,
        last_seq=last_seq,
        messages=tuple(messages),
        skipped_malformed=skipped_malformed,
    )


@dataclass(frozen=True)
class AppendReport:
    """What one `MessageLog.append()` call did: `new` messages actually written (ascending
    `seq`, strictly greater than every `seq` this log had already seen for the room), `duplicates`
    messages in the page that were not (because their `seq` was not strictly greater than the
    running high-water mark), and `gap` -- `(expected_next_seq, first_seq_seen)` when the page's
    own `first_seq` outran this log's prior `last_seq`, else `None`."""

    room: str
    new: int
    duplicates: int
    gap: tuple[int, int] | None


class MessageLog:
    """Append-only per-room message log under `root`.

    Writes only two locations, both under `root`: `messages/<room>.jsonl` (one JSON object per
    line, in `RoomMessage` field order, `ensure_ascii=False`, opened in append mode) and
    `message-log-state.json` (the per-room high-water mark, cumulative message count, every
    detected gap, and the wall-clock time of the last `append()` call for that room), the latter
    written atomically (temp file + `os.replace`) on every `append()` call.

    NOT guaranteed: `append()` trusts the `RoomPage` it is given -- it does not re-fetch or
    re-verify anything, and a gap it records reflects only what the passed-in page's own
    `first_seq` said, not an independent check of the messages actually written. The jsonl
    append and the state replace are two separate writes, not one transaction; `last_seq()`
    is self-healing against a state file left behind by that window (see its own docstring),
    so this can never duplicate a message physically, but a gap or `messages` count recorded
    for that same interrupted `append()` call is not similarly reconciled.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.messages_dir = self.root / "messages"
        self.messages_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self.root / "message-log-state.json"
        self._state: dict[str, Any] = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {"rooms": {}, "schema": _SCHEMA}
        raw: Any = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA:
            raise ValueError(
                f"{self._state_path} has an unsupported or missing schema "
                f"(expected {_SCHEMA!r})"
            )
        rooms = raw.get("rooms")
        if not isinstance(rooms, dict):
            raise ValueError(f"{self._state_path} field 'rooms' must be an object")
        return {"rooms": rooms, "schema": _SCHEMA}

    def _write_state(self) -> None:
        tmp = self.root / "message-log-state.json.tmp"
        tmp.write_text(
            json.dumps(self._state, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        os.replace(tmp, self._state_path)

    def last_seq(self, room: str) -> int:
        """The highest `seq` ever appended for `room`, or `-1` if this log has never seen it.

        Self-healing: also takes the max against the physical tail of `messages/<room>.jsonl`,
        so a state file that lagged behind an already-completed jsonl write (a crash, kill, or
        an exception between the jsonl append and the atomic state replace in `append()`) can
        never make an already-written `seq` look unseen and get re-appended as new.
        """
        rec = self._state["rooms"].get(room)
        state_last = -1
        if isinstance(rec, dict):
            value = rec.get("last_seq", -1)
            if _is_plain_int(value):
                state_last = value
        return max(state_last, self._physical_last_seq(room))

    def _physical_last_seq(self, room: str) -> int:
        """The highest `seq` actually present in `messages/<room>.jsonl`, or `-1` if the file
        does not exist or holds no parseable `seq`. A malformed or truncated line (e.g. a
        partial write cut short mid-line) is skipped rather than trusted."""
        path = self.messages_dir / f"{room}.jsonl"
        if not path.is_file():
            return -1
        highest = -1
        with path.open("rb") as fh:
            for raw_line in fh:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    obj: Any = json.loads(stripped.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                seq = obj.get("seq")
                if _is_plain_int(seq) and seq > highest:
                    highest = seq
        return highest

    def append(self, page: RoomPage) -> AppendReport:
        room = page.room
        last = self.last_seq(room)
        cursor = last
        new_messages: list[RoomMessage] = []
        for message in page.messages:
            if message.seq > cursor:
                new_messages.append(message)
                cursor = message.seq
        duplicates = len(page.messages) - len(new_messages)

        gap: tuple[int, int] | None = None
        if page.first_seq is not None and last != -1 and page.first_seq > last + 1:
            gap = (last + 1, page.first_seq)

        if new_messages:
            path = self.messages_dir / f"{room}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                for message in new_messages:
                    fh.write(
                        json.dumps(
                            {
                                "room": message.room,
                                "seq": message.seq,
                                "ts": message.ts,
                                "sender": message.sender,
                                "text": message.text,
                                "sig": message.sig,
                                "nonce": message.nonce,
                                "observed_at": message.observed_at,
                            },
                            ensure_ascii=False,
                        )
                    )
                    fh.write("\n")

        rooms_state = self._state["rooms"]
        rec = rooms_state.get(room)
        if not isinstance(rec, dict):
            rec = {"last_seq": -1, "messages": 0, "gaps": [], "last_poll_at": 0.0}
            rooms_state[room] = rec
        rec["last_seq"] = cursor
        rec["messages"] = int(rec.get("messages", 0) or 0) + len(new_messages)
        gaps = rec.get("gaps")
        if not isinstance(gaps, list):
            gaps = []
            rec["gaps"] = gaps
        if gap is not None:
            gaps.append([gap[0], gap[1]])
        rec["last_poll_at"] = time.time()
        self._write_state()

        return AppendReport(room=room, new=len(new_messages), duplicates=duplicates, gap=gap)


def select_rooms(
    rooms_jsonl: Path,
    *,
    explicit: Sequence[str],
    top: int,
    active_within_s: float,
    now: float,
) -> tuple[str, ...]:
    """Build a deterministic room set: `explicit` rooms first, in the order given (each must
    match the room id shape and must not be a private `p-*` id -- either violation raises
    `ValueError` before anything is read from `rooms_jsonl`), then up to `top` more rooms read
    from the room *directory* `rooms_jsonl`, ranked by `message_count_seen` descending (ties
    broken by room id ascending, for a fully deterministic result), restricted to rooms whose
    `last_activity_ts` lies within `active_within_s` of `now`, excluding `p-*` ids and any room
    already selected via `explicit`. `top <= 0` skips reading `rooms_jsonl` entirely.

    NOT guaranteed: a malformed line or row in `rooms_jsonl` (bad JSON, wrong types, an invalid
    or private id) is silently excluded from the top-N candidates, never an error -- only an
    `explicit` room fails loudly.
    """
    result: list[str] = []
    seen: set[str] = set()
    for room_id in explicit:
        if not isinstance(room_id, str) or not _ROOM_ID_RE.match(room_id):
            raise ValueError(f"invalid room id: {room_id!r}")
        if room_id.startswith("p-"):
            raise ValueError(f"refusing private room id: {room_id!r}")
        if room_id in seen:
            continue
        seen.add(room_id)
        result.append(room_id)

    if top > 0:
        candidates: list[tuple[int, str]] = []
        candidate_ids: set[str] = set()
        with Path(rooms_jsonl).open("rb") as fh:
            for raw_line in fh:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    row: Any = json.loads(stripped.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(row, dict):
                    continue
                candidate = row.get("id")
                if not isinstance(candidate, str) or not _ROOM_ID_RE.match(candidate):
                    continue
                if candidate.startswith("p-") or candidate in seen or candidate in candidate_ids:
                    continue
                message_count_seen = row.get("message_count_seen")
                last_activity_ts = row.get("last_activity_ts")
                if not _is_plain_int(message_count_seen) or not _is_plain_int(last_activity_ts):
                    continue
                assert isinstance(message_count_seen, int)
                assert isinstance(last_activity_ts, int)
                if abs(now - last_activity_ts) > active_within_s:
                    continue
                candidates.append((message_count_seen, candidate))
                candidate_ids.add(candidate)
        candidates.sort(key=lambda pair: (-pair[0], pair[1]))
        for _, room_id in candidates[:top]:
            seen.add(room_id)
            result.append(room_id)

    return tuple(result)


class RoomMessagesAdapter:
    """`SourceAdapter` (A3 protocol) over a `MessageLog`'s on-disk `messages/<room>.jsonl` files:
    each window of up to `window` consecutive logged messages of a room becomes one `SourceDoc`.
    Pure reads; never touches the network and never writes anything.

    NOT guaranteed: a room whose `.jsonl` file does not exist yields nothing for that room, not
    an error; a line that fails to parse as a `RoomMessage`, or whose own `room` field does not
    match the file it was read from, is skipped and counted in `skipped_malformed` -- never fatal
    to the room's remaining lines. The final (possibly short) window of a room's file is emitted
    just like a full one; only `max_docs` truncates emission early, mid-room or not.
    """

    name = "technocore_room_messages"
    kind = "room_message"

    def __init__(
        self,
        root: Path,
        *,
        rooms: Sequence[str] | None = None,
        window: int = 20,
        max_docs: int = 100_000,
    ) -> None:
        if window <= 0:
            raise ValueError("window must be > 0")
        if max_docs <= 0:
            raise ValueError("max_docs must be > 0")
        self.root = Path(root)
        self.rooms: tuple[str, ...] | None = tuple(rooms) if rooms is not None else None
        self.window = window
        self.max_docs = max_docs
        self._read = 0
        self._yielded = 0
        self._skipped_malformed = 0

    def stats(self) -> AdapterStats:
        """Reflects only the lines processed by `iter_documents()` so far."""
        return AdapterStats(
            read=self._read,
            yielded=self._yielded,
            skipped_malformed=self._skipped_malformed,
            skipped_filtered=0,
            skipped_too_large=0,
        )

    def _room_ids(self) -> list[str]:
        if self.rooms is not None:
            return list(self.rooms)
        messages_dir = self.root / "messages"
        if not messages_dir.is_dir():
            return []
        return sorted(p.stem for p in messages_dir.glob("*.jsonl"))

    def _parse_line(self, raw: bytes, expected_room: str) -> RoomMessage | None:
        try:
            obj: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(obj, dict):
            return None
        try:
            observed_raw = obj.get("observed_at")
            if isinstance(observed_raw, bool) or not isinstance(observed_raw, (int, float)):
                return None
            message = RoomMessage(
                room=_as_str(obj.get("room")),
                seq=_as_int(obj.get("seq")),
                ts=_as_str(obj.get("ts")),
                sender=_as_str(obj.get("sender")),
                text=_as_str(obj.get("text")),
                sig=_as_str(obj.get("sig")),
                nonce=_as_str(obj.get("nonce")),
                observed_at=float(observed_raw),
            )
        except (ValueError, TypeError):
            return None
        if message.room != expected_room:
            return None
        return message

    def _doc_from_batch(
        self, room: str, batch: list[RoomMessage], fetched_at: float
    ) -> SourceDoc:
        first_seq = batch[0].seq
        last_seq = batch[-1].seq
        content = "\n".join(f"[{m.seq}] {m.ts} {m.sender}: {m.text}" for m in batch)
        distinct_senders = ", ".join(sorted({m.sender for m in batch}))
        provenance = tuple(
            sorted(
                {
                    "room": room,
                    "seq_from": str(first_seq),
                    "seq_to": str(last_seq),
                    "message_count": str(len(batch)),
                    "distinct_senders": distinct_senders,
                    "first_ts": batch[0].ts,
                    "last_ts": batch[-1].ts,
                    "source_kind": "room_message",
                }.items()
            )
        )
        return SourceDoc(
            url=f"https://technocore.chat/r/{room}#{first_seq}-{last_seq}",
            kind=self.kind,
            content=content,
            content_type="text",
            fetched_at=fetched_at,
            provenance=provenance,
            title=f"Room {room} messages {first_seq}-{last_seq}",
        )

    def iter_documents(self) -> Iterator[SourceDoc]:
        fetched_at = time.time()
        emitted = 0
        for room in self._room_ids():
            if emitted >= self.max_docs:
                break
            path = self.root / "messages" / f"{room}.jsonl"
            if not path.is_file():
                continue
            batch: list[RoomMessage] = []
            with path.open("rb") as fh:
                for raw_line in fh:
                    if emitted >= self.max_docs:
                        break
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    self._read += 1
                    message = self._parse_line(stripped, room)
                    if message is None:
                        self._skipped_malformed += 1
                        continue
                    batch.append(message)
                    if len(batch) >= self.window:
                        yield self._doc_from_batch(room, batch, fetched_at)
                        self._yielded += 1
                        emitted += 1
                        batch = []
            if batch and emitted < self.max_docs:
                yield self._doc_from_batch(room, batch, fetched_at)
                self._yielded += 1
                emitted += 1
