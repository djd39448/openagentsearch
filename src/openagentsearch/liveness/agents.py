"""Agent facts and the agent-tier points table (LM1-SPEC section 6).

`agent_signals` turns every row across a set of rooms (all rows read for the build -- NOT time-
windowed; only room classification is window-scoped, see `rooms.py`) into one `AgentSignals` per
DID that (a) posted at least one signed row in scope and (b) is present in the reputation ledger --
a signed row from a sender absent from the ledger is counted in `agents_not_in_ledger` and
produces no entry (the ledger's own "only DIDs that have themselves posted get a facts entry"
rule, carried over from `openagentsearch.reputation.facts`). `classify_agent` turns one
`AgentSignals` into an `AgentVerdict`: a points total (`signals.AGENT_POINTS`, applied exactly as
named) and a tier.

`GITHUB_CONTRIB_ROOM` is the one place a room name appears in code anywhere in this package.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from openagentsearch.reputation.facts import normalize_text

from openagentsearch.liveness.signals import (
    AGENT_MIN_POSTS,
    AGENT_POINTS,
    NEGATIVE_MARKERS,
    TEMPLATE_MIN_REPEATS,
    TIER_LIKELY_MIN,
    TIER_LIVE_MIN,
    TIER_WEAK_MIN,
    Row,
    build_known_suffix_map,
    is_kibble_line,
    mask_text,
    masked_text_counts,
    matches_faucet_onboarding,
    reply_targets,
    work_cycles,
)
from openagentsearch.reputation.ledger import Ledger

# The one room name this package hardcodes -- the 08-30 method's "github-contrib code
# coordination" signal (LM1-SPEC section 6's `github_contrib_rows`).
GITHUB_CONTRIB_ROOM = "github-contrib"

_AGENT_TIERS = frozenset({"unknown", "farm", "weak", "likely_live", "live"})


@dataclass(frozen=True)
class AgentSignals:
    """One DID's counted facts over the rooms given (LM1-SPEC section 6's field table). `rooms` is
    `((room, signed_row_count), ...)`, sorted by room. `unsigned_rows` is informational only --
    never a point (`classify_agent` never reads it): unsigned rows never count toward an agent's
    signals, the same rule the reputation ledger applies to `Post`.

    NOT guaranteed: `first_seen_ts`/`last_seen_ts`/`did_note_present`/`burst` are the reputation
    ledger's OWN values for this DID, verbatim -- not recomputed from the rows given here, and
    possibly reflecting a different (usually larger) room scope than this build's `--room` list.
    """

    did: str
    post_count: int
    unsigned_rows: int
    rooms: tuple[tuple[str, int], ...]
    rooms_count: int
    live_rooms_count: int
    reply_out: int
    reply_in: int
    reply_in_distinct: int
    reply_in_nonburst: int
    distinct_text_ratio: float
    template_rows: int
    faucet_onboarding_rows: int
    work_cycles: int
    github_contrib_rows: int
    did_note_present: bool
    burst: bool
    age_days: float
    first_seen_ts: float
    last_seen_ts: float

    def __post_init__(self) -> None:
        if not isinstance(self.did, str) or not self.did:
            raise ValueError("did must be a non-empty string")
        for name in (
            "post_count", "unsigned_rows", "rooms_count", "live_rooms_count", "reply_out",
            "reply_in", "reply_in_distinct", "reply_in_nonburst", "template_rows",
            "faucet_onboarding_rows", "work_cycles", "github_contrib_rows",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")
        if self.post_count < 1:
            raise ValueError("post_count must be >= 1 (an AgentSignals only exists for a poster)")
        if self.reply_in_distinct > self.reply_in:
            raise ValueError("reply_in_distinct cannot exceed reply_in")
        if self.reply_in_nonburst > self.reply_in_distinct:
            raise ValueError("reply_in_nonburst cannot exceed reply_in_distinct")
        if self.live_rooms_count > self.rooms_count:
            raise ValueError("live_rooms_count cannot exceed rooms_count")
        if not isinstance(self.rooms, tuple) or list(self.rooms) != sorted(self.rooms):
            raise ValueError("rooms must be a tuple sorted by room name")
        if sum(count for _room, count in self.rooms) != self.post_count:
            raise ValueError("rooms' counts must sum to post_count")
        if len(self.rooms) != self.rooms_count:
            raise ValueError("rooms_count must equal len(rooms)")
        if isinstance(self.distinct_text_ratio, bool) or not isinstance(
            self.distinct_text_ratio, (int, float)
        ):
            raise ValueError("distinct_text_ratio must be a number")
        if not (0.0 <= float(self.distinct_text_ratio) <= 1.0):
            raise ValueError("distinct_text_ratio must be within [0.0, 1.0]")
        if not isinstance(self.did_note_present, bool):
            raise ValueError("did_note_present must be a bool")
        if not isinstance(self.burst, bool):
            raise ValueError("burst must be a bool")
        for name in ("age_days", "first_seen_ts", "last_seen_ts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number, got {value!r}")
        if self.age_days < 0:
            raise ValueError("age_days must be >= 0")


def agent_signals(
    rows_by_room: Mapping[str, Sequence[Row]],
    *,
    ledger: Ledger,
    room_classes: Mapping[str, str],
    now: float,
) -> tuple[dict[str, AgentSignals], int]:
    """Every `AgentSignals` computable from `rows_by_room` (every row read for this build, across
    every room given -- NOT time-windowed) against `ledger` and `room_classes` (each room's window
    `class_`, from `rooms.classify_room`), plus `agents_not_in_ledger` -- the count of distinct
    signed senders present in `rows_by_room` but absent from `ledger`.

    Pure given its inputs; `now` is used only for `AgentSignals.age_days` (never read here).
    """
    ledger_by_did = {row.facts.did: row for row in ledger.rows}
    burst_dids = {did for did, row in ledger_by_did.items() if row.score.burst}

    all_senders: set[str] = set()
    for rows in rows_by_room.values():
        for row in rows:
            all_senders.add(row[2])
    known = build_known_suffix_map(all_senders)

    masked_counts_by_room: dict[str, Counter[str]] = {
        room: masked_text_counts(rows) for room, rows in rows_by_room.items()
    }
    cycles_by_room: dict[str, dict[str, frozenset[str]]] = {
        room: work_cycles([r for r in rows if r[4]]) for room, rows in rows_by_room.items()
    }

    posts_by_did: dict[str, list[tuple[str, Row]]] = {}
    unsigned_by_did: dict[str, int] = {}
    reply_out_by_did: dict[str, int] = {}
    reply_in_rows_by_did: dict[str, int] = {}
    reply_in_senders_by_did: dict[str, set[str]] = {}

    for room, rows in rows_by_room.items():
        for row in rows:
            _seq, _ts, sender, text, signed = row
            if signed:
                posts_by_did.setdefault(sender, []).append((room, row))
                targets = reply_targets(sender, text, known)
                if targets:
                    reply_out_by_did[sender] = reply_out_by_did.get(sender, 0) + 1
            else:
                unsigned_by_did[sender] = unsigned_by_did.get(sender, 0) + 1
                targets = reply_targets(sender, text, known)
            for target in targets:
                if target in ledger_by_did:
                    reply_in_rows_by_did[target] = reply_in_rows_by_did.get(target, 0) + 1
                    reply_in_senders_by_did.setdefault(target, set()).add(sender)

    signals_by_did: dict[str, AgentSignals] = {}
    agents_not_in_ledger = 0
    for did, entries in posts_by_did.items():
        ledger_row = ledger_by_did.get(did)
        if ledger_row is None:
            agents_not_in_ledger += 1
            continue

        post_count = len(entries)
        rooms_counter: dict[str, int] = {}
        texts: set[str] = set()
        template_rows = 0
        faucet_rows = 0
        github_rows = 0
        for room, row in entries:
            rooms_counter[room] = rooms_counter.get(room, 0) + 1
            texts.add(normalize_text(row[3]))
            room_masked = masked_counts_by_room.get(room, Counter())
            if not is_kibble_line(row[3]) and room_masked[mask_text(row[3])] >= TEMPLATE_MIN_REPEATS:
                template_rows += 1
            if matches_faucet_onboarding(row[3]):
                faucet_rows += 1
            if room == GITHUB_CONTRIB_ROOM:
                github_rows += 1

        cycles_count = sum(
            1
            for room_jobs in cycles_by_room.values()
            for members in room_jobs.values()
            if did in members
        )

        rooms_tuple = tuple(sorted(rooms_counter.items()))
        live_rooms_count = sum(
            1 for room, _count in rooms_tuple if room_classes.get(room) in ("live", "mixed")
        )

        reply_in_rows = reply_in_rows_by_did.get(did, 0)
        reply_in_sender_set = reply_in_senders_by_did.get(did, set())
        reply_in_nonburst = sum(
            1 for s in reply_in_sender_set if s in ledger_by_did and s not in burst_dids
        )

        signals_by_did[did] = AgentSignals(
            did=did,
            post_count=post_count,
            unsigned_rows=unsigned_by_did.get(did, 0),
            rooms=rooms_tuple,
            rooms_count=len(rooms_tuple),
            live_rooms_count=live_rooms_count,
            reply_out=reply_out_by_did.get(did, 0),
            reply_in=reply_in_rows,
            reply_in_distinct=len(reply_in_sender_set),
            reply_in_nonburst=reply_in_nonburst,
            distinct_text_ratio=round(len(texts) / post_count, 6),
            template_rows=template_rows,
            faucet_onboarding_rows=faucet_rows,
            work_cycles=cycles_count,
            github_contrib_rows=github_rows,
            did_note_present=ledger_row.facts.github_login is not None,
            burst=ledger_row.score.burst,
            age_days=round(max(0.0, (now - ledger_row.facts.first_seen_ts) / 86400.0), 3),
            first_seen_ts=ledger_row.facts.first_seen_ts,
            last_seen_ts=ledger_row.facts.last_seen_ts,
        )

    return signals_by_did, agents_not_in_ledger


@dataclass(frozen=True)
class AgentVerdict:
    """One DID's tier. `used` is `((point_name, points, evidence_str), ...)`, in the order the
    points table (LM1-SPEC section 6) evaluated them -- `points` is the sum of `used`'s second
    elements. See `classify_agent` for the tier rule."""

    did: str
    tier: str
    points: int
    used: tuple[tuple[str, int, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.did, str) or not self.did:
            raise ValueError("did must be a non-empty string")
        if self.tier not in _AGENT_TIERS:
            raise ValueError(f"tier must be one of {sorted(_AGENT_TIERS)}, got {self.tier!r}")
        if isinstance(self.points, bool) or not isinstance(self.points, int):
            raise ValueError("points must be an int")
        if not isinstance(self.used, tuple):
            raise ValueError("used must be a tuple")
        for entry in self.used:
            if (
                not isinstance(entry, tuple)
                or len(entry) != 3
                or not isinstance(entry[0], str)
                or isinstance(entry[1], bool)
                or not isinstance(entry[1], int)
                or not isinstance(entry[2], str)
            ):
                raise ValueError(f"each used entry must be a (str, int, str) triple, got {entry!r}")
        if sum(pts for _name, pts, _ev in self.used) != self.points:
            raise ValueError("points must equal the sum of used's points")


def classify_agent(s: AgentSignals) -> AgentVerdict:
    """`s` (one DID's `AgentSignals`) as an `AgentVerdict` -- the points table (LM1-SPEC section 6)
    applied exactly as named, then the tier rule:

    1. `s.burst` -> `"farm"` (points still computed and shown).
    2. `s.post_count < AGENT_MIN_POSTS` and no fired negative marker other than `"one_line"` ->
       `"unknown"` (a single post is never enough evidence on its own, even with a positive
       marker also firing -- see LM1-SPEC section 6's Dave example).
    3. No marker fired at all (`used` empty) -> `"unknown"`.
    4. `points >= TIER_LIVE_MIN` -> `"live"`; `>= TIER_LIKELY_MIN` -> `"likely_live"`;
       `>= TIER_WEAK_MIN` -> `"weak"`; else -> `"farm"`.
    """
    used: list[tuple[str, int, str]] = []
    points = 0

    def fire(name: str, evidence: str) -> None:
        nonlocal points
        pts = AGENT_POINTS[name]
        points += pts
        used.append((name, pts, evidence))

    if s.work_cycles >= 1:
        fire("work_cycles_ge_1", f"work_cycles={s.work_cycles} >= 1")
    if s.work_cycles >= 5:
        fire("work_cycles_ge_5", f"work_cycles={s.work_cycles} >= 5")
    if s.reply_in_nonburst >= 1:
        fire("reply_in_nonburst_ge_1", f"reply_in_nonburst={s.reply_in_nonburst} >= 1")
    if s.reply_in_nonburst >= 3:
        fire("reply_in_nonburst_ge_3", f"reply_in_nonburst={s.reply_in_nonburst} >= 3")
    if s.reply_out >= 1:
        fire("reply_out_ge_1", f"reply_out={s.reply_out} >= 1")
    if s.reply_out >= 5:
        fire("reply_out_ge_5", f"reply_out={s.reply_out} >= 5")
    if s.github_contrib_rows >= 1:
        fire("github_contrib_ge_1", f"github_contrib_rows={s.github_contrib_rows} >= 1")
    if s.live_rooms_count >= 2:
        fire("live_rooms_ge_2", f"live_rooms_count={s.live_rooms_count} >= 2")
    if s.did_note_present:
        fire("did_note_present", "did_note_present=true")
    if s.distinct_text_ratio >= 0.8 and s.post_count >= 3:
        fire(
            "distinct_ge_0_8_and_posts_ge_3",
            f"distinct_text_ratio={s.distinct_text_ratio} >= 0.8 and post_count={s.post_count} >= 3",
        )
    if s.age_days >= 7:
        fire("age_ge_7d", f"age_days={s.age_days} >= 7")
    if s.faucet_onboarding_rows * 2 >= s.post_count and s.faucet_onboarding_rows > 0:
        fire(
            "faucet_onboarding_majority",
            f"faucet_onboarding_rows={s.faucet_onboarding_rows} >= majority of "
            f"post_count={s.post_count}",
        )
    if s.post_count >= 3 and s.template_rows * 2 >= s.post_count and s.template_rows > 0:
        fire(
            "template_majority_and_posts_ge_3",
            f"template_rows={s.template_rows} >= majority of post_count={s.post_count} "
            "and post_count >= 3",
        )
    if s.post_count == 1:
        fire("one_line", "post_count == 1")

    fired_names = {name for name, _pts, _ev in used}
    negative_fired = fired_names & NEGATIVE_MARKERS

    if s.burst:
        tier = "farm"
    elif s.post_count < AGENT_MIN_POSTS and negative_fired <= {"one_line"}:
        tier = "unknown"
    elif not used:
        tier = "unknown"
    elif points >= TIER_LIVE_MIN:
        tier = "live"
    elif points >= TIER_LIKELY_MIN:
        tier = "likely_live"
    elif points >= TIER_WEAK_MIN:
        tier = "weak"
    else:
        tier = "farm"

    return AgentVerdict(did=s.did, tier=tier, points=points, used=tuple(used))


def agent_signals_to_obj(s: AgentSignals) -> dict[str, Any]:
    """`s` as the exact JSON-shaped object the artifact's `agents.<did>.signals` carries."""
    return {
        "did": s.did,
        "post_count": s.post_count,
        "unsigned_rows": s.unsigned_rows,
        "rooms": {room: count for room, count in s.rooms},
        "rooms_count": s.rooms_count,
        "live_rooms_count": s.live_rooms_count,
        "reply_out": s.reply_out,
        "reply_in": s.reply_in,
        "reply_in_distinct": s.reply_in_distinct,
        "reply_in_nonburst": s.reply_in_nonburst,
        "distinct_text_ratio": s.distinct_text_ratio,
        "template_rows": s.template_rows,
        "faucet_onboarding_rows": s.faucet_onboarding_rows,
        "work_cycles": s.work_cycles,
        "github_contrib_rows": s.github_contrib_rows,
        "did_note_present": s.did_note_present,
        "burst": s.burst,
        "age_days": s.age_days,
        "first_seen_ts": s.first_seen_ts,
        "last_seen_ts": s.last_seen_ts,
    }


def agent_verdict_to_obj(v: AgentVerdict, s: AgentSignals) -> dict[str, Any]:
    """`v`/`s` as the exact JSON-shaped object the artifact's `agents.<did>` carries."""
    return {
        "tier": v.tier,
        "points": v.points,
        "used": [[name, pts, evidence] for name, pts, evidence in v.used],
        "signals": agent_signals_to_obj(s),
    }


def agent_signals_to_compact_array(s: AgentSignals, v: AgentVerdict) -> list[Any]:
    """The fixed 12-element compact array for `agents.<did>` in the compact artifact:
    `[tier, points, rooms_count, reply_in, reply_out, work_cycles, template_rows,
    faucet_onboarding_rows, github_contrib_rows, did_note_present(0/1), post_count,
    unsigned_rows]`."""
    return [
        v.tier,
        v.points,
        s.rooms_count,
        s.reply_in,
        s.reply_out,
        s.work_cycles,
        s.template_rows,
        s.faucet_onboarding_rows,
        s.github_contrib_rows,
        1 if s.did_note_present else 0,
        s.post_count,
        s.unsigned_rows,
    ]
