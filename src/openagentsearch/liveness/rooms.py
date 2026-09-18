"""Room facts and the room-class decision table (LM1-SPEC section 4/5).

`room_facts` turns one room's rows (for ONE scope -- a 7-day window, or all time) into a
`RoomFacts`: counted facts only, no judgment. `classify_room` decides a `RoomVerdict` from a
window `RoomFacts` and an all-time `RoomFacts` -- see the module-level decision table
(`_class_from_table`) and `decided_on`, which records every number the table looked at so a reader
can recompute the class from the row alone, without re-running this module.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from openagentsearch.reputation.facts import normalize_text

from openagentsearch.liveness.signals import (
    FARM_BURST_SENDER_SHARE,
    FARM_FAUCET_SENDER_SHARE,
    FARM_MIN_SENDERS,
    FARM_ONE_LINE_SHARE,
    FARM_TEMPLATE_SENDER_SHARE,
    FLOOD_ROWS_PER_5MIN_P95,
    LIVE_MIN_REPLY_SENDERS,
    LIVE_MIN_WORK_CYCLES,
    LIVE_REPLY_SENDER_SHARE,
    ROOM_MIN_ROWS,
    ROOM_MIN_SENDERS,
    SENDER_MAJORITY,
    TEMPLATE_MIN_REPEATS,
    Row,
    build_known_suffix_map,
    is_kibble_line,
    iso8601_utc,
    mask_text,
    masked_text_counts,
    matches_faucet_onboarding,
    p95,
    reply_targets,
    starts_as_reply,
    work_cycles,
)

_ROOM_CLASSES = frozenset({"unknown", "quiet", "live", "farm", "mixed", "flood"})


@dataclass(frozen=True)
class RoomFacts:
    """One room's counted facts for one scope (a window or all time) -- see LM1-SPEC section 4's
    field table. `gaps_recorded` and `burst_sender_share` are the only fields that reach outside
    this room's own rows (the message-log poller's gap ledger, and the DID reputation ledger's
    burst set, respectively); every other field is computed purely from `rows`.

    An empty scope (no rows at all) yields a `RoomFacts` of zeros/nulls, never an error -- so a
    room with no window rows still appears with `class: unknown` rather than being dropped.

    NOT guaranteed: `gaps_recorded` is NOT time-bounded -- it is `message-log-state.json`'s
    cumulative gap count for this room, the SAME value on both the window and the all-time
    `RoomFacts` for a room, never split by when a gap was recorded.
    """

    rows: int
    signed_rows: int
    distinct_senders: int
    distinct_texts: int
    distinct_text_ratio: float
    top_sender_share: float
    one_line_sender_share: float
    reply_rows: int
    reply_row_share: float
    reply_senders: int
    reply_sender_share: float
    template_rows: int
    template_row_share: float
    template_senders: int
    template_sender_share: float
    faucet_onboarding_rows: int
    faucet_onboarding_row_share: float
    faucet_onboarding_senders: int
    faucet_onboarding_sender_share: float
    work_cycle_rows: int
    work_cycles_completed: int
    rows_per_5min_p95: int
    rows_per_5min_max: int
    gaps_recorded: int | None
    burst_sender_share: float | None
    first_ts: str | None
    last_ts: str | None
    span_hours: float

    def __post_init__(self) -> None:
        for name in (
            "rows", "signed_rows", "distinct_senders", "distinct_texts", "reply_rows",
            "reply_senders", "template_rows", "template_senders", "faucet_onboarding_rows",
            "faucet_onboarding_senders", "work_cycle_rows", "work_cycles_completed",
            "rows_per_5min_p95", "rows_per_5min_max",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")
        if self.signed_rows > self.rows:
            raise ValueError("signed_rows cannot exceed rows")
        if self.distinct_texts > self.rows:
            raise ValueError("distinct_texts cannot exceed rows")
        for name in (
            "distinct_text_ratio", "top_sender_share", "one_line_sender_share",
            "reply_row_share", "reply_sender_share", "template_row_share",
            "template_sender_share", "faucet_onboarding_row_share",
            "faucet_onboarding_sender_share",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number, got {value!r}")
            if not (0.0 <= float(value) <= 1.0):
                raise ValueError(f"{name} must be within [0.0, 1.0], got {value!r}")
        if self.gaps_recorded is not None:
            if isinstance(self.gaps_recorded, bool) or not isinstance(self.gaps_recorded, int):
                raise ValueError(f"gaps_recorded must be an int or None, got {self.gaps_recorded!r}")
            if self.gaps_recorded < 0:
                raise ValueError("gaps_recorded must be >= 0")
        if self.burst_sender_share is not None:
            if isinstance(self.burst_sender_share, bool) or not isinstance(
                self.burst_sender_share, (int, float)
            ):
                raise ValueError("burst_sender_share must be a number or None")
            if not (0.0 <= float(self.burst_sender_share) <= 1.0):
                raise ValueError("burst_sender_share must be within [0.0, 1.0]")
        if (self.first_ts is None) != (self.last_ts is None):
            raise ValueError("first_ts and last_ts must both be set or both be None")
        if self.first_ts is not None and not isinstance(self.first_ts, str):
            raise ValueError("first_ts must be a string or None")
        if self.last_ts is not None and not isinstance(self.last_ts, str):
            raise ValueError("last_ts must be a string or None")
        if isinstance(self.span_hours, bool) or not isinstance(self.span_hours, (int, float)):
            raise ValueError(f"span_hours must be a number, got {self.span_hours!r}")
        if self.span_hours < 0:
            raise ValueError("span_hours must be >= 0")
        if self.rows == 0 and (self.first_ts is not None or self.last_ts is not None):
            raise ValueError("an empty scope must carry null first_ts/last_ts")


def _empty_room_facts(*, gaps_recorded: int | None) -> RoomFacts:
    return RoomFacts(
        rows=0, signed_rows=0, distinct_senders=0, distinct_texts=0, distinct_text_ratio=0.0,
        top_sender_share=0.0, one_line_sender_share=0.0, reply_rows=0, reply_row_share=0.0,
        reply_senders=0, reply_sender_share=0.0, template_rows=0, template_row_share=0.0,
        template_senders=0, template_sender_share=0.0, faucet_onboarding_rows=0,
        faucet_onboarding_row_share=0.0, faucet_onboarding_senders=0,
        faucet_onboarding_sender_share=0.0, work_cycle_rows=0, work_cycles_completed=0,
        rows_per_5min_p95=0, rows_per_5min_max=0, gaps_recorded=gaps_recorded,
        burst_sender_share=None, first_ts=None, last_ts=None, span_hours=0.0,
    )


def room_facts(
    rows: Sequence[Row],
    *,
    burst_dids: Mapping[str, bool] | frozenset[str] | set[str],
    gaps_recorded: int | None,
) -> RoomFacts:
    """`rows` (this room's rows for ONE scope, signed and unsigned -- rooms count every row) as a
    `RoomFacts`. `burst_dids` is the set of DIDs the reputation ledger marks as burst members
    (`score.burst`); `gaps_recorded` is `message-log-state.json`'s `rooms.<room>.gaps` length, or
    `None` when the file or the room entry is absent.

    Pure: the same `rows`/`burst_dids`/`gaps_recorded` always produce an equal `RoomFacts`. An
    empty `rows` yields a `RoomFacts` of zeros/nulls (`_empty_room_facts`), never an error.
    """
    if not rows:
        return _empty_room_facts(gaps_recorded=gaps_recorded)

    ordered = sorted(rows, key=lambda r: r[0])
    n = len(ordered)

    by_sender: dict[str, list[Row]] = {}
    for row in ordered:
        by_sender.setdefault(row[2], []).append(row)
    senders = sorted(by_sender)
    sender_count = len(senders)
    known = build_known_suffix_map(senders)

    signed_rows = sum(1 for row in ordered if row[4])
    distinct_texts_set = {normalize_text(row[3]) for row in ordered}
    distinct_texts = len(distinct_texts_set)
    distinct_text_ratio = round(distinct_texts / n, 6)

    top_sender_count = max(len(v) for v in by_sender.values())
    top_sender_share = round(top_sender_count / n, 6)
    one_line_senders = sum(1 for v in by_sender.values() if len(v) == 1)
    one_line_sender_share = round(one_line_senders / sender_count, 6)

    masked_counts = masked_text_counts(ordered)

    reply_rows = 0
    reply_sender_set: set[str] = set()
    template_rows = 0
    faucet_rows = 0
    for _seq, _ts, sender, text, _signed in ordered:
        is_reply = starts_as_reply(text) or bool(reply_targets(sender, text, known))
        if is_reply:
            reply_rows += 1
            reply_sender_set.add(sender)
        if not is_kibble_line(text):
            if masked_counts[mask_text(text)] >= TEMPLATE_MIN_REPEATS:
                template_rows += 1
            if matches_faucet_onboarding(text):
                faucet_rows += 1
    reply_row_share = round(reply_rows / n, 6)
    reply_senders = len(reply_sender_set)
    reply_sender_share = round(reply_senders / sender_count, 6)
    template_row_share = round(template_rows / n, 6)
    faucet_onboarding_row_share = round(faucet_rows / n, 6)

    template_senders = 0
    faucet_onboarding_senders = 0
    for srows in by_sender.values():
        k = len(srows)
        t = sum(
            1 for row in srows
            if not is_kibble_line(row[3]) and masked_counts[mask_text(row[3])] >= TEMPLATE_MIN_REPEATS
        )
        f = sum(1 for row in srows if not is_kibble_line(row[3]) and matches_faucet_onboarding(row[3]))
        if t >= k * SENDER_MAJORITY:
            template_senders += 1
        if f >= k * SENDER_MAJORITY:
            faucet_onboarding_senders += 1
    template_sender_share = round(template_senders / sender_count, 6)
    faucet_onboarding_sender_share = round(faucet_onboarding_senders / sender_count, 6)

    work_cycle_rows = sum(1 for row in ordered if is_kibble_line(row[3]))
    work_cycles_completed = len(work_cycles(ordered))

    bucket_counts_map: dict[int, int] = {}
    for row in ordered:
        bucket = int(row[1] // 300)
        bucket_counts_map[bucket] = bucket_counts_map.get(bucket, 0) + 1
    bucket_counts = list(bucket_counts_map.values())
    rows_per_5min_p95 = p95(bucket_counts)
    rows_per_5min_max = max(bucket_counts)

    signed_senders = {row[2] for row in ordered if row[4]}
    if signed_senders:
        burst_sender_share: float | None = round(
            len(signed_senders & set(burst_dids)) / len(signed_senders), 6
        )
    else:
        burst_sender_share = None

    first_ts = ordered[0][1]
    last_ts = ordered[-1][1]
    span_hours = round((last_ts - first_ts) / 3600, 1)

    return RoomFacts(
        rows=n,
        signed_rows=signed_rows,
        distinct_senders=sender_count,
        distinct_texts=distinct_texts,
        distinct_text_ratio=distinct_text_ratio,
        top_sender_share=top_sender_share,
        one_line_sender_share=one_line_sender_share,
        reply_rows=reply_rows,
        reply_row_share=reply_row_share,
        reply_senders=reply_senders,
        reply_sender_share=reply_sender_share,
        template_rows=template_rows,
        template_row_share=template_row_share,
        template_senders=template_senders,
        template_sender_share=template_sender_share,
        faucet_onboarding_rows=faucet_rows,
        faucet_onboarding_row_share=faucet_onboarding_row_share,
        faucet_onboarding_senders=faucet_onboarding_senders,
        faucet_onboarding_sender_share=faucet_onboarding_sender_share,
        work_cycle_rows=work_cycle_rows,
        work_cycles_completed=work_cycles_completed,
        rows_per_5min_p95=rows_per_5min_p95,
        rows_per_5min_max=rows_per_5min_max,
        gaps_recorded=gaps_recorded,
        burst_sender_share=burst_sender_share,
        first_ts=iso8601_utc(first_ts),
        last_ts=iso8601_utc(last_ts),
        span_hours=span_hours,
    )


@dataclass(frozen=True)
class RoomSignals:
    """The three window booleans `classify_room` derived `class` from -- see `_class_from_table`.
    Always computed from the WINDOW `RoomFacts`, regardless of whether the room even cleared the
    `ROOM_MIN_ROWS`/`ROOM_MIN_SENDERS` evidence bar (an `unknown` room can still show `farm=True`
    here; the class table simply never gets to look at it)."""

    farm: bool
    live: bool
    flood: bool

    def __post_init__(self) -> None:
        for name in ("farm", "live", "flood"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a bool")


@dataclass(frozen=True)
class RoomVerdict:
    """One room's classification. `class_` serializes to the JSON key `"class"` (a reserved word
    in Python, so the attribute is spelled with a trailing underscore -- see `room_verdict_to_obj`).
    `decided_on` is `((fact_name, value_str, threshold_expr_str), ...)` -- every number the WINDOW
    table looked at, in the order it looked at them, plus one extra entry when the
    `unknown` -> `quiet` history fallback fired -- so a reader can recompute `class_` from the row
    alone, without re-running this module.
    """

    room: str
    class_: str
    class_all: str
    signals: RoomSignals
    decided_on: tuple[tuple[str, str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.room, str) or not self.room:
            raise ValueError("room must be a non-empty string")
        if self.class_ not in _ROOM_CLASSES:
            raise ValueError(f"class_ must be one of {sorted(_ROOM_CLASSES)}, got {self.class_!r}")
        if self.class_all not in _ROOM_CLASSES:
            raise ValueError(f"class_all must be one of {sorted(_ROOM_CLASSES)}, got {self.class_all!r}")
        if not isinstance(self.signals, RoomSignals):
            raise ValueError("signals must be a RoomSignals")
        if not isinstance(self.decided_on, tuple) or not self.decided_on:
            raise ValueError("decided_on must be a non-empty tuple")
        for entry in self.decided_on:
            if not isinstance(entry, tuple) or len(entry) != 3 or not all(
                isinstance(v, str) for v in entry
            ):
                raise ValueError(f"each decided_on entry must be a (str, str, str) triple, got {entry!r}")


def _farm_signal(f: RoomFacts) -> bool:
    return f.distinct_senders >= FARM_MIN_SENDERS and (
        f.one_line_sender_share >= FARM_ONE_LINE_SHARE
        or f.template_sender_share >= FARM_TEMPLATE_SENDER_SHARE
        or f.faucet_onboarding_sender_share >= FARM_FAUCET_SENDER_SHARE
        or (f.burst_sender_share is not None and f.burst_sender_share >= FARM_BURST_SENDER_SHARE)
    )


def _live_signal(f: RoomFacts) -> bool:
    return f.work_cycles_completed >= LIVE_MIN_WORK_CYCLES or (
        f.reply_senders >= LIVE_MIN_REPLY_SENDERS and f.reply_sender_share >= LIVE_REPLY_SENDER_SHARE
    )


def _flood_signal(f: RoomFacts) -> bool:
    return f.rows_per_5min_p95 >= FLOOD_ROWS_PER_5MIN_P95


def _class_from_table(f: RoomFacts, *, farm: bool, live: bool, flood: bool) -> str:
    """The room decision table (LM1-SPEC section 5), applied to one scope's `RoomFacts` and its
    already-computed `farm`/`live`/`flood` booleans."""
    if f.rows < ROOM_MIN_ROWS or f.distinct_senders < ROOM_MIN_SENDERS:
        return "unknown"
    if flood:
        return "flood"
    if farm and live:
        return "mixed"
    if farm:
        return "farm"
    if live:
        return "live"
    return "quiet"


def _decided_on_entries(f: RoomFacts) -> list[tuple[str, str, str]]:
    return [
        ("rows", str(f.rows), f">= {ROOM_MIN_ROWS} (ROOM_MIN_ROWS)"),
        ("distinct_senders", str(f.distinct_senders), f">= {ROOM_MIN_SENDERS} (ROOM_MIN_SENDERS)"),
        (
            "rows_per_5min_p95", str(f.rows_per_5min_p95),
            f">= {FLOOD_ROWS_PER_5MIN_P95} (FLOOD_ROWS_PER_5MIN_P95)",
        ),
        (
            "distinct_senders_farm_gate", str(f.distinct_senders),
            f">= {FARM_MIN_SENDERS} (FARM_MIN_SENDERS)",
        ),
        (
            "one_line_sender_share", str(f.one_line_sender_share),
            f">= {FARM_ONE_LINE_SHARE} (FARM_ONE_LINE_SHARE)",
        ),
        (
            "template_sender_share", str(f.template_sender_share),
            f">= {FARM_TEMPLATE_SENDER_SHARE} (FARM_TEMPLATE_SENDER_SHARE)",
        ),
        (
            "faucet_onboarding_sender_share", str(f.faucet_onboarding_sender_share),
            f">= {FARM_FAUCET_SENDER_SHARE} (FARM_FAUCET_SENDER_SHARE)",
        ),
        (
            "burst_sender_share", str(f.burst_sender_share),
            f">= {FARM_BURST_SENDER_SHARE} (FARM_BURST_SENDER_SHARE)",
        ),
        (
            "work_cycles_completed", str(f.work_cycles_completed),
            f">= {LIVE_MIN_WORK_CYCLES} (LIVE_MIN_WORK_CYCLES)",
        ),
        (
            "reply_senders", str(f.reply_senders),
            f">= {LIVE_MIN_REPLY_SENDERS} (LIVE_MIN_REPLY_SENDERS)",
        ),
        (
            "reply_sender_share", str(f.reply_sender_share),
            f">= {LIVE_REPLY_SENDER_SHARE} (LIVE_REPLY_SENDER_SHARE)",
        ),
    ]


def classify_room(window: RoomFacts, all_time: RoomFacts, *, room: str) -> RoomVerdict:
    """`window`/`all_time` (the same room's `RoomFacts` for the 7-day window and for all time) as
    a `RoomVerdict`. `class_` is decided on `window`; when `window`'s table says `unknown` AND
    `all_time`'s table says `live`/`mixed`/`quiet`, `class_` becomes `quiet` instead (a room that
    went silent this week keeps its history's standing; a room that never reached the evidence bar
    stays `unknown`). `class_all` is always `all_time`'s own table result, untouched by that
    fallback."""
    farm, live, flood = _farm_signal(window), _live_signal(window), _flood_signal(window)
    window_class = _class_from_table(window, farm=farm, live=live, flood=flood)
    decided_on = _decided_on_entries(window)

    all_farm = _farm_signal(all_time)
    all_live = _live_signal(all_time)
    all_flood = _flood_signal(all_time)
    all_class = _class_from_table(all_time, farm=all_farm, live=all_live, flood=all_flood)

    final_class = window_class
    if window_class == "unknown" and all_class in ("live", "mixed", "quiet"):
        final_class = "quiet"
        decided_on = [
            *decided_on,
            (
                "class_all", all_class,
                "window unknown; all_time in {live, mixed, quiet} -> quiet",
            ),
        ]

    return RoomVerdict(
        room=room,
        class_=final_class,
        class_all=all_class,
        signals=RoomSignals(farm=farm, live=live, flood=flood),
        decided_on=tuple(decided_on),
    )


def room_facts_to_obj(f: RoomFacts) -> dict[str, Any]:
    """`f` as the exact JSON-shaped object the artifact's `rooms.<room>.facts.window`/`.all`
    carries."""
    return {
        "rows": f.rows,
        "signed_rows": f.signed_rows,
        "distinct_senders": f.distinct_senders,
        "distinct_texts": f.distinct_texts,
        "distinct_text_ratio": f.distinct_text_ratio,
        "top_sender_share": f.top_sender_share,
        "one_line_sender_share": f.one_line_sender_share,
        "reply_rows": f.reply_rows,
        "reply_row_share": f.reply_row_share,
        "reply_senders": f.reply_senders,
        "reply_sender_share": f.reply_sender_share,
        "template_rows": f.template_rows,
        "template_row_share": f.template_row_share,
        "template_senders": f.template_senders,
        "template_sender_share": f.template_sender_share,
        "faucet_onboarding_rows": f.faucet_onboarding_rows,
        "faucet_onboarding_row_share": f.faucet_onboarding_row_share,
        "faucet_onboarding_senders": f.faucet_onboarding_senders,
        "faucet_onboarding_sender_share": f.faucet_onboarding_sender_share,
        "work_cycle_rows": f.work_cycle_rows,
        "work_cycles_completed": f.work_cycles_completed,
        "rows_per_5min_p95": f.rows_per_5min_p95,
        "rows_per_5min_max": f.rows_per_5min_max,
        "gaps_recorded": f.gaps_recorded,
        "burst_sender_share": f.burst_sender_share,
        "first_ts": f.first_ts,
        "last_ts": f.last_ts,
        "span_hours": f.span_hours,
    }


def room_verdict_to_obj(v: RoomVerdict) -> dict[str, Any]:
    """`v` as the exact JSON-shaped object the artifact's `rooms.<room>` carries, minus `facts`
    (assembled by the caller, which alone knows both scopes' `RoomFacts`)."""
    return {
        "class": v.class_,
        "class_all": v.class_all,
        "signals": {"farm": v.signals.farm, "live": v.signals.live, "flood": v.signals.flood},
        "decided_on": [list(entry) for entry in v.decided_on],
    }
