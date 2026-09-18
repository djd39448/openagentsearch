"""The liveness artifact: `LivenessMap`, `build_liveness`, JSON (de)serialization, and the CLI
(`python -m openagentsearch.liveness.build`, also reachable as
`python -m openagentsearch.liveness` via `__main__.py`).

`build_liveness` reads a message log (through
`openagentsearch.sources.technocore_messages.RoomMessagesAdapter._parse_line`, the same parser
`openagentsearch.reputation.facts.load_posts` uses, but keeping unsigned rows -- see `_load_rows`),
an optional `message-log-state.json` (gap counts only), and an already-built
`openagentsearch.reputation.ledger.Ledger`, and turns them into room classes
(`openagentsearch.liveness.rooms`) and agent tiers (`openagentsearch.liveness.agents`). Nothing
here reads the network. `METHOD` is published inside the artifact next to every label, so a reader
never has to trust this module's source to know what a class or tier means.
`compare_to_baseline` (CLI `--compare PATH`) measures drift against the hand-read 2026-08-30 map --
a baseline the classifier never reads.
"""

import argparse
import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from openagentsearch.liveness.agents import (
    AgentSignals,
    AgentVerdict,
    agent_signals,
    agent_signals_to_compact_array,
    agent_verdict_to_obj,
    classify_agent,
)
from openagentsearch.liveness.rooms import (
    RoomFacts,
    RoomSignals,
    RoomVerdict,
    classify_room,
    room_facts,
    room_facts_to_obj,
    room_verdict_to_obj,
)
from openagentsearch.liveness.signals import (
    AGENT_MIN_POSTS,
    AGENT_POINTS,
    FARM_BURST_SENDER_SHARE,
    FARM_FAUCET_SENDER_SHARE,
    FARM_MIN_SENDERS,
    FARM_ONE_LINE_SHARE,
    FARM_TEMPLATE_SENDER_SHARE,
    FAUCET_ONBOARDING_PATTERNS,
    FLOOD_ROWS_PER_5MIN_P95,
    KIBBLE_LINE_RE,
    LIVE_MIN_REPLY_SENDERS,
    LIVE_MIN_WORK_CYCLES,
    LIVE_REPLY_SENDER_SHARE,
    REPLY_HEAD_RE,
    ROOM_MIN_ROWS,
    ROOM_MIN_SENDERS,
    SENDER_MAJORITY,
    TEMPLATE_MIN_REPEATS,
    TIER_LIKELY_MIN,
    TIER_LIVE_MIN,
    TIER_WEAK_MIN,
    WINDOW_DAYS_DEFAULT,
    Row,
    iso8601_utc,
)
from openagentsearch.reputation.ledger import Ledger, load_ledger
from openagentsearch.sources.technocore_messages import RoomMessagesAdapter

SCHEMA = "openagentsearch.liveness/1"
SCHEMA_COMPACT = "openagentsearch.liveness-compact/1"
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_COMPACT_BYTES = 8 * 1024 * 1024

_ROOM_TABLE_PROSE = (
    "rows < ROOM_MIN_ROWS or distinct_senders < ROOM_MIN_SENDERS -> unknown",
    "flood_signal (rows_per_5min_p95 >= FLOOD_ROWS_PER_5MIN_P95) -> flood",
    "farm_signal and live_signal -> mixed",
    "farm_signal -> farm",
    "live_signal -> live",
    "else -> quiet",
    "class = table(window); if that is unknown and table(all_time) in "
    "{live, mixed, quiet} -> quiet; class_all = table(all_time)",
)

METHOD: dict[str, Any] = {
    "constants": {
        "WINDOW_DAYS_DEFAULT": WINDOW_DAYS_DEFAULT,
        "ROOM_MIN_ROWS": ROOM_MIN_ROWS,
        "ROOM_MIN_SENDERS": ROOM_MIN_SENDERS,
        "FLOOD_ROWS_PER_5MIN_P95": FLOOD_ROWS_PER_5MIN_P95,
        "FARM_MIN_SENDERS": FARM_MIN_SENDERS,
        "FARM_ONE_LINE_SHARE": FARM_ONE_LINE_SHARE,
        "FARM_TEMPLATE_SENDER_SHARE": FARM_TEMPLATE_SENDER_SHARE,
        "FARM_FAUCET_SENDER_SHARE": FARM_FAUCET_SENDER_SHARE,
        "FARM_BURST_SENDER_SHARE": FARM_BURST_SENDER_SHARE,
        "LIVE_MIN_REPLY_SENDERS": LIVE_MIN_REPLY_SENDERS,
        "LIVE_REPLY_SENDER_SHARE": LIVE_REPLY_SENDER_SHARE,
        "LIVE_MIN_WORK_CYCLES": LIVE_MIN_WORK_CYCLES,
        "TEMPLATE_MIN_REPEATS": TEMPLATE_MIN_REPEATS,
        "SENDER_MAJORITY": SENDER_MAJORITY,
        "AGENT_MIN_POSTS": AGENT_MIN_POSTS,
        "AGENT_POINTS": dict(AGENT_POINTS),
        "TIER_LIVE_MIN": TIER_LIVE_MIN,
        "TIER_LIKELY_MIN": TIER_LIKELY_MIN,
        "TIER_WEAK_MIN": TIER_WEAK_MIN,
        "DEFAULT_MAX_BYTES": DEFAULT_MAX_BYTES,
        "DEFAULT_MAX_COMPACT_BYTES": DEFAULT_MAX_COMPACT_BYTES,
        "SCHEMA": SCHEMA,
        "SCHEMA_COMPACT": SCHEMA_COMPACT,
    },
    "faucet_onboarding_patterns": [p.pattern for p in FAUCET_ONBOARDING_PATTERNS],
    "kibble_line_pattern": KIBBLE_LINE_RE.pattern,
    "reply_head_pattern": REPLY_HEAD_RE.pattern,
    "mask_rule": (
        "replace every DID token/short-mention form with <did>, then hex runs "
        "(\\b[0-9a-f]{6,}\\b) with <hex>, then digit runs with 0, then normalize_text "
        "(NFKC, casefold, collapse+strip whitespace); a kibble grammar line is never masked "
        "or counted as a template"
    ),
    "room_table": list(_ROOM_TABLE_PROSE),
    "agent_points": dict(AGENT_POINTS),
    "tier_thresholds": {
        "live": TIER_LIVE_MIN, "likely_live": TIER_LIKELY_MIN, "weak": TIER_WEAK_MIN,
    },
    "window_rule": (
        "room classification is decided on the WINDOW facts (WINDOW_DAYS_DEFAULT, or --window-days); "
        "agent signals are computed over every row in the rooms given, not time-windowed"
    ),
}


def _parse_ts(ts: str) -> float | None:
    """`ts` (an ISO-8601 string, ordinarily `Z`-suffixed) as epoch seconds, or `None` if it does
    not parse -- the same rule `openagentsearch.reputation.facts._parse_ts` applies. Never raises."""
    text = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _room_files(log_root: Path) -> list[Path]:
    messages_dir = Path(log_root) / "messages"
    if not messages_dir.is_dir():
        return []
    return sorted(messages_dir.glob("*.jsonl"))


def _load_rows(
    log_root: Path, *, rooms: Sequence[str] | None
) -> tuple[dict[str, list[Row]], int, int]:
    """`(rows_by_room, log_rows, signed_rows)`: every row of every selected room's
    `messages/<room>.jsonl`, parsed through `RoomMessagesAdapter._parse_line` exactly like
    `openagentsearch.reputation.facts.load_posts`, EXCEPT unsigned rows are kept (rooms count all
    rows; only agent signals ever drop them -- see `openagentsearch.liveness.agents`). A room whose
    file does not exist is simply absent from `rows_by_room`, not an error. `rooms=None` selects
    every `messages/*.jsonl` file found."""
    log_root = Path(log_root)
    adapter = RoomMessagesAdapter(log_root)
    room_ids = sorted(rooms) if rooms is not None else sorted(p.stem for p in _room_files(log_root))

    rows_by_room: dict[str, list[Row]] = {}
    log_rows = 0
    signed_rows = 0
    for room in room_ids:
        path = log_root / "messages" / f"{room}.jsonl"
        if not path.is_file():
            continue
        room_rows: list[Row] = []
        with path.open("rb") as fh:
            for raw_line in fh:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                log_rows += 1
                message = adapter._parse_line(stripped, room)
                if message is None:
                    continue
                ts_epoch = _parse_ts(message.ts)
                if ts_epoch is None:
                    continue
                signed = message.sig != ""
                if signed:
                    signed_rows += 1
                room_rows.append((message.seq, ts_epoch, message.sender, message.text, signed))
        room_rows.sort(key=lambda r: r[0])
        rows_by_room[room] = room_rows
    return rows_by_room, log_rows, signed_rows


def _load_gaps(log_root: Path) -> dict[str, int]:
    """`{room: len(gaps)}` from `message-log-state.json`'s `rooms.<room>.gaps`, or `{}` when the
    file is absent or does not parse as that shape -- this input is explicitly optional (LM1-SPEC
    rule 0), so a malformed or missing file yields no gap counts rather than failing the build."""
    path = Path(log_root) / "message-log-state.json"
    if not path.is_file():
        return {}
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    rooms = raw.get("rooms")
    if not isinstance(rooms, dict):
        return {}
    gaps: dict[str, int] = {}
    for room, rec in rooms.items():
        if isinstance(room, str) and isinstance(rec, dict):
            g = rec.get("gaps")
            if isinstance(g, list):
                gaps[room] = len(g)
    return gaps


@dataclass(frozen=True)
class RoomEntry:
    """One room's verdict plus its window and all-time facts -- `LivenessMap.rooms`'s value type."""

    verdict: RoomVerdict
    window: RoomFacts
    all_time: RoomFacts


@dataclass(frozen=True)
class AgentEntry:
    """One DID's verdict plus its signals -- `LivenessMap.agents`'s value type."""

    verdict: AgentVerdict
    signals: AgentSignals


@dataclass(frozen=True)
class LivenessMap:
    """The whole liveness build, in memory -- see `to_json_bytes`/`to_compact_json_bytes` for the
    two on-disk shapes. `counts` and `candidates` are NOT stored here: `counts` is always derived
    from `rooms`/`agents` at serialization time (`_counts_obj`), so it can never drift from the
    maps it summarizes; `candidates` is always empty (`()`) -- LM1 never fills it, a later package
    (LM2) does.

    NOT guaranteed: a snapshot as of `generated_at`, against whatever message-log rows and ledger
    were on disk/passed in at build time -- not a live view.
    """

    schema: str
    generated_at: str
    now: float
    window_days: float
    log_rows: int
    signed_rows: int
    rooms_read: int
    ledger_generated_at: str
    ledger_dids: int
    agents_not_in_ledger: int
    method: Mapping[str, Any]
    rooms: Mapping[str, RoomEntry]
    agents: Mapping[str, AgentEntry]
    candidates: tuple[Any, ...]

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise ValueError(f"schema must be {SCHEMA!r}, got {self.schema!r}")
        if not isinstance(self.generated_at, str) or not self.generated_at:
            raise ValueError("generated_at must be a non-empty string")
        for name in ("now", "window_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number, got {value!r}")
            # Coerce to float so an int (e.g. WINDOW_DAYS_DEFAULT) and an equal float (e.g. the
            # CLI's argparse `type=float`) always serialize identically -- see `to_json_bytes`.
            object.__setattr__(self, name, float(value))
        for name in (
            "log_rows", "signed_rows", "rooms_read", "ledger_dids", "agents_not_in_ledger",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")
        if self.signed_rows > self.log_rows:
            raise ValueError("signed_rows cannot exceed log_rows")
        if not isinstance(self.ledger_generated_at, str) or not self.ledger_generated_at:
            raise ValueError("ledger_generated_at must be a non-empty string")
        if not isinstance(self.method, Mapping):
            raise ValueError("method must be a mapping")
        if not isinstance(self.rooms, Mapping) or not all(
            isinstance(v, RoomEntry) for v in self.rooms.values()
        ):
            raise ValueError("rooms must be a mapping of RoomEntry")
        if not isinstance(self.agents, Mapping) or not all(
            isinstance(v, AgentEntry) for v in self.agents.values()
        ):
            raise ValueError("agents must be a mapping of AgentEntry")
        if not isinstance(self.candidates, tuple):
            raise ValueError("candidates must be a tuple")


def _counts_obj(rooms: Mapping[str, RoomEntry], agents: Mapping[str, AgentEntry]) -> dict[str, Any]:
    rooms_by_class: dict[str, int] = {}
    rooms_by_class_all: dict[str, int] = {}
    for room_entry in rooms.values():
        rooms_by_class[room_entry.verdict.class_] = rooms_by_class.get(room_entry.verdict.class_, 0) + 1
        rooms_by_class_all[room_entry.verdict.class_all] = (
            rooms_by_class_all.get(room_entry.verdict.class_all, 0) + 1
        )
    agents_by_tier: dict[str, int] = {}
    for agent_entry in agents.values():
        agents_by_tier[agent_entry.verdict.tier] = agents_by_tier.get(agent_entry.verdict.tier, 0) + 1
    return {
        "rooms_by_class": rooms_by_class,
        "rooms_by_class_all": rooms_by_class_all,
        "agents_by_tier": agents_by_tier,
    }


def build_liveness(
    log_root: Path,
    ledger: Ledger,
    *,
    now: float,
    window_days: float = WINDOW_DAYS_DEFAULT,
    rooms: Sequence[str] | None = None,
) -> LivenessMap:
    """Read `log_root`'s message log (and its optional `message-log-state.json`), classify every
    room against `ledger`, compute every ledger DID's agent signals and tier, and assemble one
    `LivenessMap`. `rooms` restricts which room files are read (rooms AND agent scope) -- `None`
    reads every `messages/*.jsonl` file found. `now` is the caller's clock reading, used for
    `generated_at`, the window cutoff, and every `AgentSignals.age_days`; this function never reads
    a clock itself.
    """
    log_root = Path(log_root)
    rows_by_room, log_rows, signed_rows = _load_rows(log_root, rooms=rooms)
    gaps_by_room = _load_gaps(log_root)
    burst_dids = {row.facts.did for row in ledger.rows if row.score.burst}

    cutoff = now - window_days * 86400.0
    window_rows_by_room = {
        room: [r for r in rs if r[1] >= cutoff] for room, rs in rows_by_room.items()
    }

    room_entries: dict[str, RoomEntry] = {}
    room_classes: dict[str, str] = {}
    for room in sorted(rows_by_room):
        window_facts = room_facts(
            window_rows_by_room[room], burst_dids=burst_dids, gaps_recorded=gaps_by_room.get(room)
        )
        all_facts = room_facts(
            rows_by_room[room], burst_dids=burst_dids, gaps_recorded=gaps_by_room.get(room)
        )
        verdict = classify_room(window_facts, all_facts, room=room)
        room_entries[room] = RoomEntry(verdict=verdict, window=window_facts, all_time=all_facts)
        room_classes[room] = verdict.class_

    signals_by_did, agents_not_in_ledger = agent_signals(
        rows_by_room, ledger=ledger, room_classes=room_classes, now=now
    )
    agent_entries = {
        did: AgentEntry(verdict=classify_agent(sig), signals=sig)
        for did, sig in signals_by_did.items()
    }

    return LivenessMap(
        schema=SCHEMA,
        generated_at=iso8601_utc(now),
        now=now,
        window_days=window_days,
        log_rows=log_rows,
        signed_rows=signed_rows,
        rooms_read=len(rows_by_room),
        ledger_generated_at=ledger.generated_at,
        ledger_dids=ledger.dids,
        agents_not_in_ledger=agents_not_in_ledger,
        method=METHOD,
        rooms=room_entries,
        agents=agent_entries,
        candidates=(),
    )


def _room_entry_to_obj(entry: RoomEntry) -> dict[str, Any]:
    obj = room_verdict_to_obj(entry.verdict)
    obj["facts"] = {
        "window": room_facts_to_obj(entry.window),
        "all": room_facts_to_obj(entry.all_time),
    }
    return obj


def to_json_bytes(m: LivenessMap) -> bytes:
    """`m` -> the canonical UTF-8 bytes of `liveness-v1.json`: one JSON object, `ensure_ascii=False`,
    sorted keys, compact separators, no trailing newline (imported as JSON directly, the same
    convention `openagentsearch.reputation.compact.to_compact_json_bytes` uses) -- byte-
    deterministic: two builds of the same input produce identical bytes."""
    rooms_obj = {room: _room_entry_to_obj(entry) for room, entry in m.rooms.items()}
    agents_obj = {
        did: agent_verdict_to_obj(entry.verdict, entry.signals) for did, entry in m.agents.items()
    }
    obj: dict[str, Any] = {
        "schema": m.schema,
        "generated_at": m.generated_at,
        "now": m.now,
        "window_days": m.window_days,
        "log_rows": m.log_rows,
        "signed_rows": m.signed_rows,
        "rooms_read": m.rooms_read,
        "ledger_generated_at": m.ledger_generated_at,
        "ledger_dids": m.ledger_dids,
        "agents_not_in_ledger": m.agents_not_in_ledger,
        "method": m.method,
        "rooms": rooms_obj,
        "agents": agents_obj,
        "counts": _counts_obj(m.rooms, m.agents),
        "candidates": list(m.candidates),
    }
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def to_compact_json_bytes(m: LivenessMap) -> bytes:
    """`m` -> the canonical UTF-8 bytes of `liveness-compact.json`: `method`/`rooms`/`counts`
    verbatim (the same shapes `to_json_bytes` writes for them), `agents` reduced to the fixed
    12-element array `agents.agent_signals_to_compact_array` documents."""
    rooms_obj = {room: _room_entry_to_obj(entry) for room, entry in m.rooms.items()}
    agents_obj = {
        did: agent_signals_to_compact_array(entry.signals, entry.verdict)
        for did, entry in m.agents.items()
    }
    obj: dict[str, Any] = {
        "schema": SCHEMA_COMPACT,
        "generated_at": m.generated_at,
        "window_days": m.window_days,
        "log_rows": m.log_rows,
        "ledger_generated_at": m.ledger_generated_at,
        "method": m.method,
        "rooms": rooms_obj,
        "counts": _counts_obj(m.rooms, m.agents),
        "agents": agents_obj,
    }
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class LivenessSizeError(ValueError):
    """Raised by `write_liveness()` when the serialized artifact exceeds `max_bytes` -- raised
    BEFORE any file (not even a temp file) is created."""


class CompactLivenessSizeError(ValueError):
    """Raised by `write_compact_liveness()` when the serialized compact artifact exceeds
    `max_bytes` -- raised BEFORE any file (not even a temp file) is created."""


def _write_atomic(data: bytes, out_path: Path) -> int:
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


def write_liveness(m: LivenessMap, out_path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> int:
    """Serialize `m` (`to_json_bytes`) and write it to `out_path` atomically (temp file in the
    same directory, then `os.replace`), returning the byte count written.

    Raises:
        LivenessSizeError: the serialized bytes exceed `max_bytes`. Checked BEFORE any write.
    """
    data = to_json_bytes(m)
    if len(data) > max_bytes:
        raise LivenessSizeError(f"liveness map is {len(data)} bytes, over the {max_bytes} byte limit")
    return _write_atomic(data, Path(out_path))


def write_compact_liveness(
    m: LivenessMap, out_path: Path, *, max_bytes: int = DEFAULT_MAX_COMPACT_BYTES
) -> int:
    """Serialize `m`'s compact artifact (`to_compact_json_bytes`) and write it to `out_path`
    atomically, returning the byte count written.

    Raises:
        CompactLivenessSizeError: the serialized bytes exceed `max_bytes`. Checked BEFORE any write.
    """
    data = to_compact_json_bytes(m)
    if len(data) > max_bytes:
        raise CompactLivenessSizeError(
            f"compact liveness map is {len(data)} bytes, over the {max_bytes} byte limit"
        )
    return _write_atomic(data, Path(out_path))


# --------------------------------------------------------------------------------------------
# Fail-closed loaders.
# --------------------------------------------------------------------------------------------


def _fail(problem: str) -> ValueError:
    return ValueError(f"malformed liveness map: {problem}")


def _req_str(obj: Mapping[str, Any], key: str, path: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise _fail(f"{path}.{key} must be a string, got {value!r}")
    return value


def _req_int(obj: Mapping[str, Any], key: str, path: str) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(f"{path}.{key} must be an int, got {value!r}")
    return value


def _req_num(obj: Mapping[str, Any], key: str, path: str) -> float:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(f"{path}.{key} must be a number, got {value!r}")
    return float(value)


def _req_bool(obj: Mapping[str, Any], key: str, path: str) -> bool:
    value = obj.get(key)
    if not isinstance(value, bool):
        raise _fail(f"{path}.{key} must be a bool, got {value!r}")
    return value


def _opt_int(obj: Mapping[str, Any], key: str, path: str) -> int | None:
    value = obj.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(f"{path}.{key} must be an int or null, got {value!r}")
    return value


def _opt_num(obj: Mapping[str, Any], key: str, path: str) -> float | None:
    value = obj.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(f"{path}.{key} must be a number or null, got {value!r}")
    return float(value)


def _opt_str(obj: Mapping[str, Any], key: str, path: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail(f"{path}.{key} must be a string or null, got {value!r}")
    return value


def _obj_to_room_facts(obj: object, path: str) -> RoomFacts:
    if not isinstance(obj, dict):
        raise _fail(f"{path} must be an object, got {type(obj).__name__}")
    try:
        return RoomFacts(
            rows=_req_int(obj, "rows", path),
            signed_rows=_req_int(obj, "signed_rows", path),
            distinct_senders=_req_int(obj, "distinct_senders", path),
            distinct_texts=_req_int(obj, "distinct_texts", path),
            distinct_text_ratio=_req_num(obj, "distinct_text_ratio", path),
            top_sender_share=_req_num(obj, "top_sender_share", path),
            one_line_sender_share=_req_num(obj, "one_line_sender_share", path),
            reply_rows=_req_int(obj, "reply_rows", path),
            reply_row_share=_req_num(obj, "reply_row_share", path),
            reply_senders=_req_int(obj, "reply_senders", path),
            reply_sender_share=_req_num(obj, "reply_sender_share", path),
            template_rows=_req_int(obj, "template_rows", path),
            template_row_share=_req_num(obj, "template_row_share", path),
            template_senders=_req_int(obj, "template_senders", path),
            template_sender_share=_req_num(obj, "template_sender_share", path),
            faucet_onboarding_rows=_req_int(obj, "faucet_onboarding_rows", path),
            faucet_onboarding_row_share=_req_num(obj, "faucet_onboarding_row_share", path),
            faucet_onboarding_senders=_req_int(obj, "faucet_onboarding_senders", path),
            faucet_onboarding_sender_share=_req_num(obj, "faucet_onboarding_sender_share", path),
            work_cycle_rows=_req_int(obj, "work_cycle_rows", path),
            work_cycles_completed=_req_int(obj, "work_cycles_completed", path),
            rows_per_5min_p95=_req_int(obj, "rows_per_5min_p95", path),
            rows_per_5min_max=_req_int(obj, "rows_per_5min_max", path),
            gaps_recorded=_opt_int(obj, "gaps_recorded", path),
            burst_sender_share=_opt_num(obj, "burst_sender_share", path),
            first_ts=_opt_str(obj, "first_ts", path),
            last_ts=_opt_str(obj, "last_ts", path),
            span_hours=_req_num(obj, "span_hours", path),
        )
    except ValueError as exc:
        raise _fail(f"{path}: {exc}") from exc


def _obj_to_room_entry(obj: object, room: str) -> RoomEntry:
    path = f"rooms[{room!r}]"
    if not isinstance(obj, dict):
        raise _fail(f"{path} must be an object, got {type(obj).__name__}")
    class_ = _req_str(obj, "class", path)
    class_all = _req_str(obj, "class_all", path)
    signals_obj = obj.get("signals")
    if not isinstance(signals_obj, dict):
        raise _fail(f"{path}.signals must be an object")
    decided_on_raw = obj.get("decided_on")
    if not isinstance(decided_on_raw, list):
        raise _fail(f"{path}.decided_on must be a list")
    decided_on: list[tuple[str, str, str]] = []
    for entry in decided_on_raw:
        if not isinstance(entry, list) or len(entry) != 3 or not all(
            isinstance(v, str) for v in entry
        ):
            raise _fail(f"{path}.decided_on entry malformed: {entry!r}")
        decided_on.append((entry[0], entry[1], entry[2]))
    facts_obj = obj.get("facts")
    if not isinstance(facts_obj, dict):
        raise _fail(f"{path}.facts must be an object")
    window = _obj_to_room_facts(facts_obj.get("window"), f"{path}.facts.window")
    all_time = _obj_to_room_facts(facts_obj.get("all"), f"{path}.facts.all")
    try:
        verdict = RoomVerdict(
            room=room,
            class_=class_,
            class_all=class_all,
            signals=RoomSignals(
                farm=_req_bool(signals_obj, "farm", f"{path}.signals"),
                live=_req_bool(signals_obj, "live", f"{path}.signals"),
                flood=_req_bool(signals_obj, "flood", f"{path}.signals"),
            ),
            decided_on=tuple(decided_on),
        )
    except ValueError as exc:
        raise _fail(f"{path}: {exc}") from exc
    return RoomEntry(verdict=verdict, window=window, all_time=all_time)


def _obj_to_agent_signals(obj: Mapping[str, Any], did: str, path: str) -> AgentSignals:
    rooms_raw = obj.get("rooms")
    if not isinstance(rooms_raw, dict):
        raise _fail(f"{path}.rooms must be an object")
    rooms_list: list[tuple[str, int]] = []
    for room, count in rooms_raw.items():
        if not isinstance(room, str) or isinstance(count, bool) or not isinstance(count, int):
            raise _fail(f"{path}.rooms[{room!r}] must be an int")
        rooms_list.append((room, count))
    rooms_list.sort()
    try:
        return AgentSignals(
            did=did,
            post_count=_req_int(obj, "post_count", path),
            unsigned_rows=_req_int(obj, "unsigned_rows", path),
            rooms=tuple(rooms_list),
            rooms_count=_req_int(obj, "rooms_count", path),
            live_rooms_count=_req_int(obj, "live_rooms_count", path),
            reply_out=_req_int(obj, "reply_out", path),
            reply_in=_req_int(obj, "reply_in", path),
            reply_in_distinct=_req_int(obj, "reply_in_distinct", path),
            reply_in_nonburst=_req_int(obj, "reply_in_nonburst", path),
            distinct_text_ratio=_req_num(obj, "distinct_text_ratio", path),
            template_rows=_req_int(obj, "template_rows", path),
            faucet_onboarding_rows=_req_int(obj, "faucet_onboarding_rows", path),
            work_cycles=_req_int(obj, "work_cycles", path),
            github_contrib_rows=_req_int(obj, "github_contrib_rows", path),
            did_note_present=_req_bool(obj, "did_note_present", path),
            burst=_req_bool(obj, "burst", path),
            age_days=_req_num(obj, "age_days", path),
            first_seen_ts=_req_num(obj, "first_seen_ts", path),
            last_seen_ts=_req_num(obj, "last_seen_ts", path),
        )
    except ValueError as exc:
        raise _fail(f"{path}: {exc}") from exc


def _obj_to_agent_entry(obj: object, did: str) -> AgentEntry:
    path = f"agents[{did!r}]"
    if not isinstance(obj, dict):
        raise _fail(f"{path} must be an object, got {type(obj).__name__}")
    tier = _req_str(obj, "tier", path)
    points = _req_int(obj, "points", path)
    used_raw = obj.get("used")
    if not isinstance(used_raw, list):
        raise _fail(f"{path}.used must be a list")
    used: list[tuple[str, int, str]] = []
    for entry in used_raw:
        if (
            not isinstance(entry, list)
            or len(entry) != 3
            or not isinstance(entry[0], str)
            or isinstance(entry[1], bool)
            or not isinstance(entry[1], int)
            or not isinstance(entry[2], str)
        ):
            raise _fail(f"{path}.used entry malformed: {entry!r}")
        used.append((entry[0], entry[1], entry[2]))
    signals_obj = obj.get("signals")
    if not isinstance(signals_obj, dict):
        raise _fail(f"{path}.signals must be an object")
    signals = _obj_to_agent_signals(signals_obj, did, f"{path}.signals")
    try:
        verdict = AgentVerdict(did=did, tier=tier, points=points, used=tuple(used))
    except ValueError as exc:
        raise _fail(f"{path}: {exc}") from exc
    return AgentEntry(verdict=verdict, signals=signals)


def load_liveness(path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> LivenessMap:
    """Fail-closed load of a `liveness-v1.json` written by `write_liveness`/`to_json_bytes`.

    Raises `ValueError` naming the FIRST problem found, and never returns a partially-built
    `LivenessMap`, for: a file over `max_bytes`; invalid UTF-8; a body that is not valid JSON or
    not an object; a `schema` other than `SCHEMA`; missing/mistyped top-level fields; a malformed
    room or agent entry (including a `class`/`class_all`/`tier` outside its vocabulary -- enforced
    by `RoomVerdict`/`AgentVerdict`'s own `__post_init__`); or a `counts` field that disagrees with
    the actual `rooms`/`agents` maps.
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
    try:
        obj: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _fail(f"not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise _fail(f"body must be a JSON object, got {type(obj).__name__}")

    schema = _req_str(obj, "schema", "$")
    if schema != SCHEMA:
        raise _fail(f"schema must be {SCHEMA!r}, got {schema!r}")
    generated_at = _req_str(obj, "generated_at", "$")
    now = _req_num(obj, "now", "$")
    window_days = _req_num(obj, "window_days", "$")
    log_rows = _req_int(obj, "log_rows", "$")
    signed_rows = _req_int(obj, "signed_rows", "$")
    rooms_read = _req_int(obj, "rooms_read", "$")
    ledger_generated_at = _req_str(obj, "ledger_generated_at", "$")
    ledger_dids = _req_int(obj, "ledger_dids", "$")
    agents_not_in_ledger = _req_int(obj, "agents_not_in_ledger", "$")

    method = obj.get("method")
    if not isinstance(method, dict):
        raise _fail("method must be an object")

    rooms_raw = obj.get("rooms")
    if not isinstance(rooms_raw, dict):
        raise _fail("rooms must be an object")
    rooms: dict[str, RoomEntry] = {}
    for room, room_obj in rooms_raw.items():
        if not isinstance(room, str):
            raise _fail(f"rooms key must be a string, got {room!r}")
        rooms[room] = _obj_to_room_entry(room_obj, room)

    agents_raw = obj.get("agents")
    if not isinstance(agents_raw, dict):
        raise _fail("agents must be an object")
    agents: dict[str, AgentEntry] = {}
    for did, agent_obj in agents_raw.items():
        if not isinstance(did, str):
            raise _fail(f"agents key must be a string, got {did!r}")
        agents[did] = _obj_to_agent_entry(agent_obj, did)

    counts_raw = obj.get("counts")
    if counts_raw != _counts_obj(rooms, agents):
        raise _fail("counts disagrees with the actual rooms/agents maps")

    candidates_raw = obj.get("candidates")
    if not isinstance(candidates_raw, list):
        raise _fail("candidates must be a list")

    return LivenessMap(
        schema=schema,
        generated_at=generated_at,
        now=now,
        window_days=window_days,
        log_rows=log_rows,
        signed_rows=signed_rows,
        rooms_read=rooms_read,
        ledger_generated_at=ledger_generated_at,
        ledger_dids=ledger_dids,
        agents_not_in_ledger=agents_not_in_ledger,
        method=method,
        rooms=rooms,
        agents=agents,
        candidates=tuple(candidates_raw),
    )


_AGENT_TIERS = frozenset({"unknown", "farm", "weak", "likely_live", "live"})


@dataclass(frozen=True)
class CompactAgentEntry:
    """One DID's entry in the compact artifact -- the same 12 fields
    `agents.agent_signals_to_compact_array` writes, named. Deliberately NOT an `AgentSignals`: the
    compact array carries no per-room breakdown, no `reply_in_distinct`, no `distinct_text_ratio`,
    and no `age_days`/`first_seen_ts`/`last_seen_ts` -- there is nothing here to reconstruct a full
    `AgentSignals` from, and this type never pretends otherwise."""

    tier: str
    points: int
    rooms_count: int
    reply_in: int
    reply_out: int
    work_cycles: int
    template_rows: int
    faucet_onboarding_rows: int
    github_contrib_rows: int
    did_note_present: bool
    post_count: int
    unsigned_rows: int

    def __post_init__(self) -> None:
        if self.tier not in _AGENT_TIERS:
            raise ValueError(f"tier must be one of {sorted(_AGENT_TIERS)}, got {self.tier!r}")
        for name in (
            "points", "rooms_count", "reply_in", "reply_out", "work_cycles", "template_rows",
            "faucet_onboarding_rows", "github_contrib_rows", "post_count", "unsigned_rows",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an int, got {value!r}")
        if not isinstance(self.did_note_present, bool):
            raise ValueError("did_note_present must be a bool")


@dataclass(frozen=True)
class CompactLivenessMap:
    """The compact artifact, loaded into memory. `rooms` carries the same full `RoomEntry` shape
    `load_liveness` does (`rooms` is verbatim between the two artifacts); `agents` is the lossy
    `CompactAgentEntry` view -- see its own docstring for what it deliberately cannot carry."""

    schema: str
    generated_at: str
    window_days: float
    log_rows: int
    ledger_generated_at: str
    method: Mapping[str, Any]
    rooms: Mapping[str, RoomEntry]
    agents: Mapping[str, CompactAgentEntry]

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_COMPACT:
            raise ValueError(f"schema must be {SCHEMA_COMPACT!r}, got {self.schema!r}")
        if not isinstance(self.generated_at, str) or not self.generated_at:
            raise ValueError("generated_at must be a non-empty string")
        if isinstance(self.log_rows, bool) or not isinstance(self.log_rows, int) or self.log_rows < 0:
            raise ValueError("log_rows must be a non-negative int")
        if not isinstance(self.ledger_generated_at, str) or not self.ledger_generated_at:
            raise ValueError("ledger_generated_at must be a non-empty string")
        if not isinstance(self.method, Mapping):
            raise ValueError("method must be a mapping")
        if not isinstance(self.rooms, Mapping) or not all(
            isinstance(v, RoomEntry) for v in self.rooms.values()
        ):
            raise ValueError("rooms must be a mapping of RoomEntry")
        if not isinstance(self.agents, Mapping) or not all(
            isinstance(v, CompactAgentEntry) for v in self.agents.values()
        ):
            raise ValueError("agents must be a mapping of CompactAgentEntry")


def _counts_obj_compact(
    rooms: Mapping[str, RoomEntry], agents: Mapping[str, CompactAgentEntry]
) -> dict[str, Any]:
    rooms_by_class: dict[str, int] = {}
    rooms_by_class_all: dict[str, int] = {}
    for room_entry in rooms.values():
        rooms_by_class[room_entry.verdict.class_] = rooms_by_class.get(room_entry.verdict.class_, 0) + 1
        rooms_by_class_all[room_entry.verdict.class_all] = (
            rooms_by_class_all.get(room_entry.verdict.class_all, 0) + 1
        )
    agents_by_tier: dict[str, int] = {}
    for agent_entry in agents.values():
        agents_by_tier[agent_entry.tier] = agents_by_tier.get(agent_entry.tier, 0) + 1
    return {
        "rooms_by_class": rooms_by_class,
        "rooms_by_class_all": rooms_by_class_all,
        "agents_by_tier": agents_by_tier,
    }


def load_compact_liveness(
    path: Path, *, max_bytes: int = DEFAULT_MAX_COMPACT_BYTES
) -> CompactLivenessMap:
    """Fail-closed load of a `liveness-compact.json` written by
    `write_compact_liveness`/`to_compact_json_bytes`. Same failure modes as `load_liveness` (over
    `max_bytes`; invalid UTF-8/JSON/shape; wrong `schema`; malformed room entries; `counts`
    disagreeing with the actual maps), plus: refuses any `agents.<did>` entry that is not a list of
    EXACTLY 12 elements in the documented order, or whose `tier` is outside the tier vocabulary.
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
    try:
        obj: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _fail(f"not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise _fail(f"body must be a JSON object, got {type(obj).__name__}")

    schema = _req_str(obj, "schema", "$")
    if schema != SCHEMA_COMPACT:
        raise _fail(f"schema must be {SCHEMA_COMPACT!r}, got {schema!r}")
    generated_at = _req_str(obj, "generated_at", "$")
    window_days = _req_num(obj, "window_days", "$")
    log_rows = _req_int(obj, "log_rows", "$")
    ledger_generated_at = _req_str(obj, "ledger_generated_at", "$")

    method = obj.get("method")
    if not isinstance(method, dict):
        raise _fail("method must be an object")

    rooms_raw = obj.get("rooms")
    if not isinstance(rooms_raw, dict):
        raise _fail("rooms must be an object")
    rooms: dict[str, RoomEntry] = {}
    for room, room_obj in rooms_raw.items():
        if not isinstance(room, str):
            raise _fail(f"rooms key must be a string, got {room!r}")
        rooms[room] = _obj_to_room_entry(room_obj, room)

    agents_raw = obj.get("agents")
    if not isinstance(agents_raw, dict):
        raise _fail("agents must be an object")
    agents: dict[str, CompactAgentEntry] = {}
    for did, arr in agents_raw.items():
        if not isinstance(did, str):
            raise _fail(f"agents key must be a string, got {did!r}")
        if not isinstance(arr, list) or len(arr) != 12:
            raise _fail(f"agents[{did!r}] must be a 12-element array, got {arr!r}")
        (
            tier, points, rooms_count, reply_in, reply_out, work_cycles_n, template_rows,
            faucet_onboarding_rows, github_contrib_rows, did_note_present, post_count,
            unsigned_rows,
        ) = arr
        entry_path = f"agents[{did!r}]"
        if not isinstance(tier, str):
            raise _fail(f"{entry_path}[0] (tier) must be a string")
        if isinstance(points, bool) or not isinstance(points, int):
            raise _fail(f"{entry_path}[1] (points) must be an int")
        int_fields = {
            "rooms_count": rooms_count, "reply_in": reply_in, "reply_out": reply_out,
            "work_cycles": work_cycles_n, "template_rows": template_rows,
            "faucet_onboarding_rows": faucet_onboarding_rows,
            "github_contrib_rows": github_contrib_rows,
            "post_count": post_count, "unsigned_rows": unsigned_rows,
        }
        for name, value in int_fields.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise _fail(f"{entry_path} field {name!r} must be an int, got {value!r}")
        if did_note_present not in (0, 1):
            raise _fail(f"{entry_path} did_note_present must be 0 or 1, got {did_note_present!r}")
        try:
            agents[did] = CompactAgentEntry(
                tier=tier, points=points, rooms_count=rooms_count, reply_in=reply_in,
                reply_out=reply_out, work_cycles=work_cycles_n, template_rows=template_rows,
                faucet_onboarding_rows=faucet_onboarding_rows,
                github_contrib_rows=github_contrib_rows,
                did_note_present=bool(did_note_present), post_count=post_count,
                unsigned_rows=unsigned_rows,
            )
        except ValueError as exc:
            raise _fail(f"{entry_path}: {exc}") from exc

    counts_raw = obj.get("counts")
    if counts_raw != _counts_obj_compact(rooms, agents):
        raise _fail("counts disagrees with the actual rooms/agents maps")

    return CompactLivenessMap(
        schema=schema,
        generated_at=generated_at,
        window_days=window_days,
        log_rows=log_rows,
        ledger_generated_at=ledger_generated_at,
        method=method,
        rooms=rooms,
        agents=agents,
    )


# --------------------------------------------------------------------------------------------
# Comparison against the hand-read 2026-08-30 map (a baseline, never an input).
# --------------------------------------------------------------------------------------------

# How the 08-30 map's room verdict vocabulary (`technocore-chat-map/v1`, `rooms_examined[].verdict`)
# is read against this package's six classes, for the comparison ONLY -- nothing in the classifier
# reads this. The 08-30 tiers (`live`/`likely_live`/`weak`/`farm`) already share this package's
# names; its roster never says `unknown`.
BASELINE_VERDICT_MAP: dict[str, str] = {
    "LIVE": "live",
    "LIVE-SELFREF": "live",
    "LIVE-DEGRADED": "live",
    "LIVE-MIXED": "mixed",
    "MIXED": "mixed",
    "FARM-MIXED": "mixed",
    "LIVE-QUIET": "quiet",
    "LIVE-LOWVALUE": "quiet",
    "BOT-BENIGN": "quiet",
    "FARM-DOMINATED": "farm",
    "FARM": "farm",
}


def compare_to_baseline(m: LivenessMap, baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Agreement between `m` and a `technocore-chat-map/v1` document (the 2026-08-30 hand-read
    map), over the rooms and DIDs BOTH cover. Rooms are compared twice -- this build's window
    `class` and its all-time `class_all` against the baseline verdict read through
    `BASELINE_VERDICT_MAP` (an unmapped verdict compares as its lowercase self and never
    agrees). Agents compare tier to tier. Returns plain counts plus every room pair and an
    `old->new` confusion count for agents, so a reader sees exactly which verdicts moved.

    NOT guaranteed: agreement is a drift measurement, not a correctness score -- the 08-30 map was
    read by hand from 60-200 messages per room three weeks before this package existed, and
    verdicts drift (that is the reason this package exists). A `baseline` missing the expected
    keys yields empty comparisons, never an error.
    """
    rooms_examined = baseline.get("rooms_examined")
    old_rooms: dict[str, tuple[str, str]] = {}
    if isinstance(rooms_examined, list):
        for row in rooms_examined:
            if isinstance(row, dict) and isinstance(row.get("room"), str) and isinstance(
                row.get("verdict"), str
            ):
                raw = row["verdict"]
                old_rooms[row["room"]] = (BASELINE_VERDICT_MAP.get(raw, raw.lower()), raw)
    room_pairs: list[list[str]] = []
    agree_window = 0
    agree_all = 0
    for room in sorted(m.rooms):
        if room not in old_rooms:
            continue
        mapped, raw = old_rooms[room]
        ours = m.rooms[room].verdict.class_
        ours_all = m.rooms[room].verdict.class_all
        agree_window += ours == mapped
        agree_all += ours_all == mapped
        room_pairs.append([room, ours, ours_all, mapped, raw])

    agents_obj = baseline.get("agents")
    roster = agents_obj.get("roster") if isinstance(agents_obj, dict) else None
    old_tiers: dict[str, str] = {}
    if isinstance(roster, list):
        for row in roster:
            if isinstance(row, dict) and isinstance(row.get("did"), str) and isinstance(
                row.get("tier"), str
            ):
                old_tiers[row["did"]] = row["tier"]
    agents_both = 0
    agents_agree = 0
    confusion: dict[str, int] = {}
    for did in sorted(m.agents):
        old = old_tiers.get(did)
        if old is None:
            continue
        new = m.agents[did].verdict.tier
        agents_both += 1
        agents_agree += old == new
        key = f"{old}->{new}"
        confusion[key] = confusion.get(key, 0) + 1

    return {
        "baseline_generated": baseline.get("generated_utc"),
        "rooms_both": len(room_pairs),
        "rooms_agree_window": agree_window,
        "rooms_agree_all": agree_all,
        "room_pairs": room_pairs,
        "agents_both": agents_both,
        "agents_agree": agents_agree,
        "agent_confusion": dict(sorted(confusion.items())),
    }


# --------------------------------------------------------------------------------------------
# CLI.
# --------------------------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an int, got {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {parsed}")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openagentsearch.liveness.build")
    parser.add_argument("--log-root", required=True, help="message-log root (holds messages/)")
    parser.add_argument("--ledger", required=True, help="did-ledger.jsonl to classify against")
    parser.add_argument("--out", required=True, help="liveness-v1.json file to write")
    parser.add_argument("--compact-out", default=None, dest="compact_out")
    parser.add_argument("--window-days", type=float, default=WINDOW_DAYS_DEFAULT, dest="window_days")
    parser.add_argument("--now", type=float, default=None, help="epoch seconds; default: now")
    parser.add_argument("--room", action="append", default=None, dest="rooms", help="repeatable")
    parser.add_argument(
        "--max-bytes", type=_positive_int, default=DEFAULT_MAX_BYTES, dest="max_bytes"
    )
    parser.add_argument(
        "--max-compact-bytes", type=_positive_int, default=DEFAULT_MAX_COMPACT_BYTES,
        dest="max_compact_bytes",
    )
    parser.add_argument(
        "--compare", default=None, dest="compare",
        help=(
            "optional: a technocore-chat-map/v1 document (the 2026-08-30 hand-read map) to report "
            "agreement against under the report's `compare` key -- a baseline, never an input"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, build the liveness map, write it, and print the result. Mirrors
    `openagentsearch.reputation.build`'s exit codes (0 success, 1 any other failure with one JSON
    `{"error": ...}` line on stderr and nothing on stdout, 2 an argparse failure) and its
    one-JSON-line stdout report convention."""
    args = _build_parser().parse_args(argv)
    log_root = Path(args.log_root)
    out_path = Path(args.out)
    now = time.time() if args.now is None else args.now
    started = time.perf_counter()

    try:
        if not log_root.is_dir():
            raise NotADirectoryError(f"--log-root not found: {log_root}")
        ledger = load_ledger(Path(args.ledger))
        liveness = build_liveness(
            log_root, ledger,
            now=now, window_days=args.window_days,
            rooms=tuple(args.rooms) if args.rooms is not None else None,
        )
        written_bytes = write_liveness(liveness, out_path, max_bytes=args.max_bytes)
        compact_bytes: int | None = None
        if args.compact_out is not None:
            compact_bytes = write_compact_liveness(
                liveness, Path(args.compact_out), max_bytes=args.max_compact_bytes
            )
        comparison: dict[str, Any] | None = None
        if args.compare is not None:
            baseline_raw: Any = json.loads(Path(args.compare).read_text(encoding="utf-8"))
            if not isinstance(baseline_raw, dict):
                raise ValueError("--compare must be a JSON object (technocore-chat-map/v1)")
            comparison = compare_to_baseline(liveness, baseline_raw)
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
        "log_rows": liveness.log_rows,
        "signed_rows": liveness.signed_rows,
        "rooms": liveness.rooms_read,
        "agents": len(liveness.agents),
        "agents_not_in_ledger": liveness.agents_not_in_ledger,
        "rooms_by_class": _counts_obj(liveness.rooms, liveness.agents)["rooms_by_class"],
        "agents_by_tier": _counts_obj(liveness.rooms, liveness.agents)["agents_by_tier"],
        "seconds": time.perf_counter() - started,
    }
    if compact_bytes is not None:
        result["compact_bytes"] = compact_bytes
    if comparison is not None:
        result["compare"] = comparison
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the subprocess test
    raise SystemExit(main())
