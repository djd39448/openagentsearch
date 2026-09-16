"""The typed DID reputation ledger and its on-disk JSONL file.

`build_ledger` is the single pure(ish) orchestration -- load the message log (`facts.load_posts`),
load the optional `did-*` notes (`notes.load_did_notes`), compute `DidFacts` (`facts.build_facts`)
and `Score`s (`score.rank`) -- glued into one `Ledger` and a `LedgerBuildReport`. The only I/O is
reading the log and the notes file; nothing here writes anything. `to_jsonl_bytes` /
`write_ledger` / `load_ledger` mirror the atomic-write and fail-closed-load conventions used
elsewhere in this repository (`openagentsearch.pipeline.publish`'s `_write_atomic`,
`openagentsearch.lexical.build.write_lexical_index`, `openagentsearch.lexical.index.
load_lexical_index`) rather than inventing new ones.
"""

import bisect
import json
import os
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openagentsearch.reputation.facts import (
    BURST_MIN_NEW_DEFAULT,
    BURST_WINDOW_S_DEFAULT,
    PER_DID_BURST_PER_MINUTE_DEFAULT,
    DidFacts,
    build_facts,
    load_posts,
)
from openagentsearch.reputation.notes import load_did_notes
from openagentsearch.reputation.score import Score, rank

SCHEMA = "openagentsearch.did-ledger/1"
DEFAULT_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class LedgerRow:
    """One ledger row: a DID's `DidFacts` alongside the `Score` computed from it. `facts.did`
    and `score.did` must agree -- `__post_init__` enforces it."""

    facts: DidFacts
    score: Score

    def __post_init__(self) -> None:
        if not isinstance(self.facts, DidFacts):
            raise ValueError(f"facts must be a DidFacts, got {self.facts!r}")
        if not isinstance(self.score, Score):
            raise ValueError(f"score must be a Score, got {self.score!r}")
        if self.facts.did != self.score.did:
            raise ValueError(
                f"facts.did {self.facts.did!r} does not match score.did {self.score.did!r}"
            )


@dataclass(frozen=True)
class Ledger:
    """The whole reputation ledger as of one build. `rows` is always sorted ascending by
    `facts.did`, with no duplicate `did` -- this is the SAME order `to_jsonl_bytes` writes and
    `load_ledger` requires on read, so nothing re-sorts at serialization time.

    NOT guaranteed: this is a snapshot as of `generated_at` against whatever message-log rows
    were on disk at build time -- not a live view, and not a promise that any `did` here still
    posts, or ever will again.
    """

    schema: str
    generated_at: str
    log_rows: int
    posts: int
    dids: int
    bursts: int
    rows: tuple[LedgerRow, ...]

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise ValueError(f"schema must be {SCHEMA!r}, got {self.schema!r}")
        if not isinstance(self.generated_at, str) or not self.generated_at:
            raise ValueError("generated_at must be a non-empty string")
        for name, value in (
            ("log_rows", self.log_rows),
            ("posts", self.posts),
            ("dids", self.dids),
            ("bursts", self.bursts),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")
        if self.posts > self.log_rows:
            raise ValueError(f"posts ({self.posts}) cannot exceed log_rows ({self.log_rows})")
        if self.dids > self.posts:
            raise ValueError(f"dids ({self.dids}) cannot exceed posts ({self.posts})")
        if self.bursts > self.dids:
            raise ValueError(f"bursts ({self.bursts}) cannot exceed dids ({self.dids})")
        if not isinstance(self.rows, tuple) or not all(isinstance(r, LedgerRow) for r in self.rows):
            raise ValueError("rows must be a tuple of LedgerRow")
        if len(self.rows) != self.dids:
            raise ValueError(f"dids ({self.dids}) does not match len(rows) ({len(self.rows)})")
        dids_seen = [row.facts.did for row in self.rows]
        if dids_seen != sorted(dids_seen):
            raise ValueError("rows must be sorted ascending by facts.did")
        if len(set(dids_seen)) != len(dids_seen):
            raise ValueError("rows must not repeat a did")


@dataclass(frozen=True)
class LedgerBuildReport:
    """What one `build_ledger()` call did. `log_rows`/`posts`/`skipped_malformed`/
    `skipped_unsigned` mirror `facts.LoadReport`; `notes_*` mirror `notes.NotesReport`; `dids` and
    `bursts` are the same counts recorded on the `Ledger` itself; `seconds` is this call's own
    wall-clock cost (unrelated to `generated_at`, which is a value recorded IN the ledger, not
    measured by this build -- the same convention `lexical.build.LexicalBuildReport` uses)."""

    log_rows: int
    posts: int
    skipped_malformed: int
    skipped_unsigned: int
    dids: int
    bursts: int
    notes_lines: int
    notes_used: int
    notes_skipped_malformed: int
    notes_skipped_not_self_authored: int
    seconds: float


def _iso8601_utc(timestamp: float) -> str:
    """`timestamp` (Unix epoch seconds) as a `Z`-suffixed ISO-8601 string in UTC, whole-second
    precision -- the same convention `pipeline.publish`/`lexical.build` use for `generated_at`."""
    stamp = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")
    return stamp.replace("+00:00", "Z")


def build_ledger(
    log_root: Path,
    *,
    now: float,
    rooms: Sequence[str] | None = None,
    notes_path: Path | None = None,
    burst_window_s: float = BURST_WINDOW_S_DEFAULT,
    burst_min_new: int = BURST_MIN_NEW_DEFAULT,
    per_did_burst_per_minute: int = PER_DID_BURST_PER_MINUTE_DEFAULT,
) -> tuple[Ledger, LedgerBuildReport]:
    """Read `log_root`'s message log (and, optionally, a `did-*` notes file), compute every
    `DidFacts` and `Score`, and assemble one `Ledger` -- rows sorted ascending by `did` (see
    `Ledger`'s own docstring), NOT by score/rank; `score.rank()`'s ordering is used only to
    compute each `Score`, never to order `Ledger.rows`.

    `now` is the caller's clock reading, used both for `Ledger.generated_at` and for every
    `Score.score_did`'s `age_days` -- this function never reads a clock itself. `rooms` is passed
    straight through to `facts.load_posts`; `notes_path` straight through to
    `notes.load_did_notes`; `burst_window_s` / `burst_min_new` straight through to
    `facts.build_facts`, and `per_did_burst_per_minute` to `score.rank` (their defaults are the
    SAME defaults those functions use).

    NOT guaranteed: nothing here writes anything -- see `write_ledger` for the atomic on-disk
    step. `bursts` counts distinct FIRST-SEEN-TIMESTAMP burst groups only (`DidFacts.burst_id`
    values); a DID that is a burst member solely because of its own posting RATE (`burst_id is
    None`, `max_posts_per_minute` over threshold) is not counted in `bursts`, even though
    `is_burst_member` treats it as a burst member for scoring.
    """
    started = time.perf_counter()
    posts, load_report = load_posts(Path(log_root), rooms=rooms)
    notes, notes_report = load_did_notes(notes_path)
    all_facts = build_facts(
        posts,
        notes=notes,
        burst_window_s=burst_window_s,
        burst_min_new=burst_min_new,
    )
    scores_by_did = {
        s.did: s
        for s in rank(all_facts, now=now, per_did_burst_per_minute=per_did_burst_per_minute)
    }
    rows = tuple(
        LedgerRow(facts=all_facts[did], score=scores_by_did[did]) for did in sorted(all_facts)
    )
    bursts = len({f.burst_id for f in all_facts.values() if f.burst_id is not None})

    ledger = Ledger(
        schema=SCHEMA,
        generated_at=_iso8601_utc(now),
        log_rows=load_report.rows,
        posts=load_report.posts,
        dids=len(all_facts),
        bursts=bursts,
        rows=rows,
    )
    report = LedgerBuildReport(
        log_rows=load_report.rows,
        posts=load_report.posts,
        skipped_malformed=load_report.skipped_malformed,
        skipped_unsigned=load_report.skipped_unsigned,
        dids=len(all_facts),
        bursts=bursts,
        notes_lines=notes_report.lines,
        notes_used=notes_report.used,
        notes_skipped_malformed=notes_report.skipped_malformed,
        notes_skipped_not_self_authored=notes_report.skipped_not_self_authored,
        seconds=time.perf_counter() - started,
    )
    return ledger, report


def _facts_to_obj(f: DidFacts) -> dict[str, Any]:
    return {
        "did": f.did,
        "first_seen_seq": f.first_seen_seq,
        "first_seen_ts": f.first_seen_ts,
        "first_seen_room": f.first_seen_room,
        "last_seen_ts": f.last_seen_ts,
        "post_count": f.post_count,
        "rooms_posted": list(f.rooms_posted),
        "distinct_text_count": f.distinct_text_count,
        "distinct_text_ratio": f.distinct_text_ratio,
        "outbound_mentions": list(f.outbound_mentions),
        "inbound_mentions": [[did, count] for did, count in f.inbound_mentions],
        "unresolved_mentions": f.unresolved_mentions,
        "github_login": f.github_login,
        "github_login_source": f.github_login_source,
        "burst_id": f.burst_id,
        "max_posts_per_minute": f.max_posts_per_minute,
    }


def _score_to_obj(s: Score) -> dict[str, Any]:
    return {
        "did": s.did,
        "score": s.score,
        "burst": s.burst,
        "facts_used": [[name, value] for name, value in s.facts_used],
    }


def to_jsonl_bytes(ledger: Ledger) -> bytes:
    """`ledger` -> its canonical UTF-8 JSONL bytes: one compact, key-sorted, `ensure_ascii=False`
    header line (`schema`/`generated_at`/`log_rows`/`posts`/`dids`/`bursts`), then one such line
    per row, sorted by `did` (re-sorted here regardless of `ledger.rows`'s own order, which is
    already did-sorted by construction -- see `Ledger`'s own invariant -- so this is a defensive
    re-assertion, not a behavior change). Two equal `Ledger` values always produce identical
    bytes, which is what makes two builds of the same input byte-identical."""
    header = {
        "schema": ledger.schema,
        "generated_at": ledger.generated_at,
        "log_rows": ledger.log_rows,
        "posts": ledger.posts,
        "dids": ledger.dids,
        "bursts": ledger.bursts,
    }
    lines = [json.dumps(header, ensure_ascii=False, sort_keys=True, separators=(",", ":"))]
    for row in sorted(ledger.rows, key=lambda r: r.facts.did):
        obj = {
            "did": row.facts.did,
            "facts": _facts_to_obj(row.facts),
            "score": _score_to_obj(row.score),
        }
        lines.append(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return ("\n".join(lines) + "\n").encode("utf-8")


class LedgerSizeError(ValueError):
    """Raised by `write_ledger()` when the serialized ledger exceeds `max_bytes` -- raised BEFORE
    any file (not even a temp file) is created."""


def write_ledger(ledger: Ledger, out_path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> int:
    """Serialize `ledger` (`to_jsonl_bytes`) and write it to `out_path` atomically (temp file in
    the same directory, then `os.replace`), returning the byte count written.

    Raises:
        LedgerSizeError: the serialized bytes exceed `max_bytes`. Checked BEFORE any write, so a
            refusal leaves `out_path`'s directory exactly as it was found.
    """
    data = to_jsonl_bytes(ledger)
    if len(data) > max_bytes:
        raise LedgerSizeError(f"ledger is {len(data)} bytes, over the {max_bytes} byte limit")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(out_path.parent), prefix=f".{out_path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, out_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return len(data)


def _fail(problem: str) -> ValueError:
    return ValueError(f"malformed ledger: {problem}")


def _require_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise _fail(f"{name} must be a string, got {value!r}")
    return value


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(f"{name} must be an int, got {value!r}")
    return value


def _require_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(f"{name} must be a number, got {value!r}")
    return float(value)


def _require_str_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _fail(f"{name} must be a list of strings, got {value!r}")
    return value


def _obj_to_facts(obj: object) -> DidFacts:
    if not isinstance(obj, dict):
        raise _fail(f"facts must be an object, got {type(obj).__name__}")
    rooms_posted = _require_str_list(obj.get("rooms_posted"), "facts.rooms_posted")
    outbound_mentions = _require_str_list(obj.get("outbound_mentions"), "facts.outbound_mentions")

    inbound_raw = obj.get("inbound_mentions")
    if not isinstance(inbound_raw, list):
        raise _fail(f"facts.inbound_mentions must be a list, got {inbound_raw!r}")
    inbound: list[tuple[str, int]] = []
    for item in inbound_raw:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or isinstance(item[1], bool)
            or not isinstance(item[1], int)
        ):
            raise _fail(f"facts.inbound_mentions entry must be [did, count], got {item!r}")
        inbound.append((item[0], item[1]))

    github_login = obj.get("github_login")
    if github_login is not None and not isinstance(github_login, str):
        raise _fail(f"facts.github_login must be a string or null, got {github_login!r}")

    burst_id = obj.get("burst_id")
    if burst_id is not None and (isinstance(burst_id, bool) or not isinstance(burst_id, int)):
        raise _fail(f"facts.burst_id must be an int or null, got {burst_id!r}")

    return DidFacts(
        did=_require_str(obj.get("did"), "facts.did"),
        first_seen_seq=_require_int(obj.get("first_seen_seq"), "facts.first_seen_seq"),
        first_seen_ts=_require_number(obj.get("first_seen_ts"), "facts.first_seen_ts"),
        first_seen_room=_require_str(obj.get("first_seen_room"), "facts.first_seen_room"),
        last_seen_ts=_require_number(obj.get("last_seen_ts"), "facts.last_seen_ts"),
        post_count=_require_int(obj.get("post_count"), "facts.post_count"),
        rooms_posted=tuple(rooms_posted),
        distinct_text_count=_require_int(
            obj.get("distinct_text_count"), "facts.distinct_text_count"
        ),
        distinct_text_ratio=_require_number(
            obj.get("distinct_text_ratio"), "facts.distinct_text_ratio"
        ),
        outbound_mentions=tuple(outbound_mentions),
        inbound_mentions=tuple(inbound),
        unresolved_mentions=_require_int(
            obj.get("unresolved_mentions"), "facts.unresolved_mentions"
        ),
        github_login=github_login,
        github_login_source=_require_str(
            obj.get("github_login_source"), "facts.github_login_source"
        ),
        burst_id=burst_id,
        max_posts_per_minute=_require_int(
            obj.get("max_posts_per_minute"), "facts.max_posts_per_minute"
        ),
    )


def _obj_to_score(obj: object) -> Score:
    if not isinstance(obj, dict):
        raise _fail(f"score must be an object, got {type(obj).__name__}")
    facts_used_raw = obj.get("facts_used")
    if not isinstance(facts_used_raw, list):
        raise _fail(f"score.facts_used must be a list, got {facts_used_raw!r}")
    facts_used: list[tuple[str, str]] = []
    for item in facts_used_raw:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise _fail(f"score.facts_used entry must be [name, value] strings, got {item!r}")
        facts_used.append((item[0], item[1]))
    burst = obj.get("burst")
    if not isinstance(burst, bool):
        raise _fail(f"score.burst must be a bool, got {burst!r}")
    return Score(
        did=_require_str(obj.get("did"), "score.did"),
        score=_require_number(obj.get("score"), "score.score"),
        burst=burst,
        facts_used=tuple(facts_used),
    )


def load_ledger(path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Ledger:
    """Fail-closed load of a ledger JSONL file written by `write_ledger`/`to_jsonl_bytes`.

    Raises `ValueError` naming the FIRST problem found, and never returns a partially-built
    `Ledger`, for: a file over `max_bytes`; invalid UTF-8; a header or row line that is not valid
    JSON or not an object; a `schema` other than `SCHEMA`; a malformed `facts`/`score` object
    (wrong type, a missing or mistyped field); a `did` that disagrees between the row, its
    `facts.did` and its `score.did`; rows not STRICTLY sorted ascending by `did` (a repeat or an
    out-of-order `did` both fail); or a header `dids` count that disagrees with the actual number
    of rows.

    NOT guaranteed: this does not re-derive `log_rows`/`posts`/`dids`/`bursts` from the rows to
    check they are independently consistent beyond the `dids`-vs-row-count check above, and it
    does not re-run `score.score_did` to confirm a stored `Score` is still what the formula would
    produce today -- a hand-edited file with self-consistent-looking but wrong numbers loads
    without complaint (`facts_used` existing and being recomputable, per `docs/reputation.md`, is
    a property a caller checks separately, not something this loader verifies).
    """
    path = Path(path)
    size = path.stat().st_size
    if size > max_bytes:
        raise _fail(f"file is {size} bytes, over the {max_bytes} byte limit")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail(f"not valid UTF-8: {exc}") from exc

    lines = [line for line in text.split("\n") if line != ""]
    if not lines:
        raise _fail("empty ledger file")

    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise _fail(f"header is not valid JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise _fail(f"header must be a JSON object, got {type(header).__name__}")
    schema = _require_str(header.get("schema"), "schema")
    if schema != SCHEMA:
        raise _fail(f"schema must be {SCHEMA!r}, got {schema!r}")
    generated_at = _require_str(header.get("generated_at"), "generated_at")
    log_rows = _require_int(header.get("log_rows"), "log_rows")
    posts = _require_int(header.get("posts"), "posts")
    dids = _require_int(header.get("dids"), "dids")
    bursts = _require_int(header.get("bursts"), "bursts")

    rows: list[LedgerRow] = []
    previous_did: str | None = None
    for line in lines[1:]:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _fail(f"row is not valid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise _fail(f"row must be a JSON object, got {type(obj).__name__}")
        did = obj.get("did")
        if not isinstance(did, str) or not did:
            raise _fail(f"row.did must be a non-empty string, got {did!r}")
        facts = _obj_to_facts(obj.get("facts"))
        score = _obj_to_score(obj.get("score"))
        if facts.did != did or score.did != did:
            raise _fail(
                f"row did mismatch: row={did!r} facts.did={facts.did!r} score.did={score.did!r}"
            )
        if previous_did is not None and did <= previous_did:
            raise _fail(
                f"rows must be strictly sorted ascending by did: {did!r} after {previous_did!r}"
            )
        previous_did = did
        rows.append(LedgerRow(facts=facts, score=score))

    if len(rows) != dids:
        raise _fail(f"header dids={dids} does not match row count {len(rows)}")

    return Ledger(
        schema=schema,
        generated_at=generated_at,
        log_rows=log_rows,
        posts=posts,
        dids=dids,
        bursts=bursts,
        rows=tuple(rows),
    )


def lookup(ledger: Ledger, did: str) -> LedgerRow | None:
    """`ledger`'s row for `did`, or `None` if it has none. `ledger.rows` is always sorted
    ascending by `did` (see `Ledger`'s own invariant), so this is a binary search, not a scan."""
    dids = [row.facts.did for row in ledger.rows]
    index = bisect.bisect_left(dids, did)
    if index < len(dids) and dids[index] == did:
        return ledger.rows[index]
    return None
