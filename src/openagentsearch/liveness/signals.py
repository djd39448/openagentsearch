"""Shared, pure text-level primitives for `rooms.py` and `agents.py`: constants, regex sets,
`mask_text`, `is_kibble_line`, `kibble_stage`, `matches_faucet_onboarding`, `reply_targets`,
`starts_as_reply`, `work_cycles`, `masked_text_counts`, `p95`, `build_known_suffix_map`,
`iso8601_utc`. Nothing here knows about rooms or agents: every function takes plain rows or plain
text and returns plain facts -- `rooms.py`/`agents.py` decide what those facts mean.

`Row` is the one row shape every function here (and `rooms.room_facts`) takes: `(seq, ts, sender,
text, signed)` -- the same fields `openagentsearch.reputation.facts.load_posts`/`Post` carry, plus
`signed`, in a plain tuple rather than a dataclass, because these functions have no per-row
identity to protect (unlike `Post`, nothing here is ever written back to disk row-by-row).

Base58 alphabet and three of the four DID-mention forms are `openagentsearch.reputation.facts`'s
own (`_BASE58`, `_DID_TOKEN_RE`, `_AT_MENTION_RE`, `_ABBR_MENTION_RE`) -- imported, not
re-declared, so the two packages can never drift on what a DID token looks like. The one new form
this package adds is the live log's U+2026 ("...") short mention: `z6Mk` + up to four base58
characters + U+2026 + exactly four base58 characters (e.g. `@z6Mk...afJi` in arxiv-jam) --
`ABBR_MENTION_ELLIPSIS_RE`.
"""

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone

from openagentsearch.reputation.facts import _AT_MENTION_RE as AT_MENTION_RE
from openagentsearch.reputation.facts import _BASE58 as BASE58
from openagentsearch.reputation.facts import _ABBR_MENTION_RE as ABBR_MENTION_RE
from openagentsearch.reputation.facts import _DID_TOKEN_RE as DID_TOKEN_RE
from openagentsearch.reputation.facts import normalize_text

# One row: (seq, ts_epoch, sender, text, signed). Signed and unsigned rows are both represented --
# callers decide which subset of rows to pass to a given function (see each function's docstring).
Row = tuple[int, float, str, str, bool]

# --------------------------------------------------------------------------------------------
# Section 2: constants. Every one of these appears verbatim in `build.METHOD["constants"]` --
# `tests/test_liveness_build.py` checks it, so a change here is never silently unpublished.
# --------------------------------------------------------------------------------------------

WINDOW_DAYS_DEFAULT = 7
ROOM_MIN_ROWS = 20
ROOM_MIN_SENDERS = 3
FLOOD_ROWS_PER_5MIN_P95 = 150
FARM_MIN_SENDERS = 20
FARM_ONE_LINE_SHARE = 0.75
FARM_TEMPLATE_SENDER_SHARE = 0.5
FARM_FAUCET_SENDER_SHARE = 0.5
FARM_BURST_SENDER_SHARE = 0.5
LIVE_MIN_REPLY_SENDERS = 3
LIVE_REPLY_SENDER_SHARE = 0.05
LIVE_MIN_WORK_CYCLES = 1
TEMPLATE_MIN_REPEATS = 5
SENDER_MAJORITY = 0.5
AGENT_MIN_POSTS = 2
AGENT_POINTS: dict[str, int] = {
    "work_cycles_ge_1": 3,
    "work_cycles_ge_5": 1,
    "reply_in_nonburst_ge_1": 2,
    "reply_in_nonburst_ge_3": 1,
    "reply_out_ge_1": 2,
    "reply_out_ge_5": 1,
    "github_contrib_ge_1": 2,
    "live_rooms_ge_2": 1,
    "did_note_present": 1,
    "distinct_ge_0_8_and_posts_ge_3": 1,
    "age_ge_7d": 1,
    "faucet_onboarding_majority": -3,
    "template_majority_and_posts_ge_3": -3,
    "one_line": -2,
}
TIER_LIVE_MIN = 6
TIER_LIKELY_MIN = 3
TIER_WEAK_MIN = 1
NEGATIVE_MARKERS: frozenset[str] = frozenset(
    {"faucet_onboarding_majority", "template_majority_and_posts_ge_3", "one_line"}
)

# --------------------------------------------------------------------------------------------
# Section 3: regex sets.
# --------------------------------------------------------------------------------------------

# The live log's U+2026 short mention: "z6Mk" + up to 4 base58 + an ellipsis + exactly 4 base58
# (e.g. "@z6Mk...afJi" in arxiv-jam, "following z6Mk...uizf"). Same suffix length/shape as
# `ABBR_MENTION_RE`, only the separator differs ("..." vs U+2026).
ABBR_MENTION_ELLIPSIS_RE = re.compile(rf"\bz6Mk[{BASE58}]{{0,4}}…([{BASE58}]{{4}})(?![{BASE58}])")

FAUCET_ONBOARDING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"faucet claim", re.IGNORECASE),
    re.compile(r"\bcheck(?:ing|ed)?[ -]?in\b", re.IGNORECASE),
    re.compile(r"\bagent presence\b|\bpresence (?:active|pulse|check)\b", re.IGNORECASE),
    re.compile(r"\bheartbeat\b", re.IGNORECASE),
    re.compile(r"\bnetwork participant #\d+", re.IGNORECASE),
    re.compile(r"\binfrastructure operational\b", re.IGNORECASE),
    re.compile(r"\bmeta-layer engaged\b|\bobserving technocore meta-layer\b", re.IGNORECASE),
    re.compile(r"\bready for the airdrop\b", re.IGNORECASE),
    re.compile(r"^\s*(?:hello|hi|hey|gm)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:autonomous participation logged|did active|identity maintained|agent online"
        r"|node online)\b",
        re.IGNORECASE,
    ),
    re.compile(r"fleet-test/v1"),
)

KIBBLE_LINE_RE = re.compile(r"^(JOB|CLAIM|RESULT|DELIVER|ATTEST|SUBMIT|ACCEPT|HELLO|BRIEF) v1 \| (\S+) \|")

REPLY_HEAD_RE = re.compile(r"^\s*re\b", re.IGNORECASE)

_HEX_RUN_RE = re.compile(r"\b[0-9a-f]{6,}\b")
_DIGIT_RUN_RE = re.compile(r"[0-9]+")

# The five kibble-grammar stages (in order) that make one completed work cycle -- see
# `work_cycles`. RESULT and DELIVER are interchangeable at that one position (subsequence match).
_CYCLE_STAGES: tuple[str | tuple[str, ...], ...] = ("JOB", "CLAIM", ("RESULT", "DELIVER"), "ATTEST")


def is_kibble_line(text: str) -> bool:
    """`True` when `text` matches the kibble-v1 protocol grammar (`KIBBLE_LINE_RE`). A kibble
    grammar line is never masked or counted as a template -- this function is checked first
    everywhere templates are computed (`masked_text_counts`, `rooms.room_facts`,
    `agents.agent_signals`)."""
    return KIBBLE_LINE_RE.match(text) is not None


def kibble_stage(text: str) -> tuple[str, str] | None:
    """`(stage, job_id)` when `text` is a kibble grammar line, else `None`."""
    match = KIBBLE_LINE_RE.match(text)
    if match is None:
        return None
    return match.group(1), match.group(2)


def matches_faucet_onboarding(text: str) -> bool:
    """`True` when `text` matches any pattern in `FAUCET_ONBOARDING_PATTERNS`. Callers check
    `is_kibble_line` first where the room/agent facts tables say to (a kibble grammar line is
    never counted as faucet/onboarding text for the ROOM facts table -- see `rooms.room_facts`)."""
    return any(pattern.search(text) for pattern in FAUCET_ONBOARDING_PATTERNS)


def starts_as_reply(text: str) -> bool:
    """`True` when `text` starts with `re` (case-insensitive, `REPLY_HEAD_RE`) -- e.g. "Re: seq
    56187 -- ...". A bare `@nickname` is deliberately NOT a reply -- see `reply_targets`."""
    return REPLY_HEAD_RE.match(text) is not None


def build_known_suffix_map(senders: Iterable[str]) -> dict[str, str]:
    """`key -> DID` for every distinct `sender` in `senders`: keyed by each sender's last 8
    characters AND last 4 characters (the same construction
    `openagentsearch.reputation.facts.build_facts` uses) AND by the full DID string itself, so
    `reply_targets` resolves a full `did:key:z...` token through the same map and only when that
    token names a sender in scope. A suffix shared by more than one sender is never put in the
    map (a mention using it resolves to nothing through `reply_targets`); a full DID is never
    ambiguous with an 8- or 4-character suffix (it is always longer)."""
    suffix_map: dict[str, set[str]] = {}
    for did in senders:
        for length in (8, 4):
            suffix = did[-length:] if len(did) >= length else did
            suffix_map.setdefault(suffix, set()).add(did)
        suffix_map.setdefault(did, set()).add(did)
    return {suffix: next(iter(dids)) for suffix, dids in suffix_map.items() if len(dids) == 1}


def reply_targets(sender: str, text: str, known: Mapping[str, str]) -> tuple[str, ...]:
    """DIDs `text` addresses, sorted and deduplicated, never including `sender` itself: every
    full `did:key:z...` token that names a sender in scope, plus every `@`+8-base58,
    `z6Mk..`+4-base58, or `z6Mk`+U+2026+4-base58 short mention that resolves through `known` (the
    map `build_known_suffix_map` builds: UNAMBIGUOUS 8- and 4-character suffixes AND every
    in-scope sender's full DID, each mapped to that DID).

    A full DID token that names nobody in scope is NOT a reply (measured 2026-09-18: a swarm
    bot's truncated `@did:key:z6Mkhe...`, a leaderboard bot's `did:key:z6Mkib…9KH9r` lines and a
    faucet claim naming its own DID would otherwise have made technocore-genesis a "live" room
    with 984 "reply senders" against 4 real ones). A bare `@nickname` that is not exactly 8
    base58 characters is never a match either (builders has 100+ such presence lines) -- `text`
    "starting as a reply" (`starts_as_reply`) is a SEPARATE signal from this function, which only
    resolves explicit mentions of identities that have posted in scope.

    NOT guaranteed: a mention whose suffix names no DID at all, or is genuinely ambiguous between
    more than one, resolves to nothing here -- silently, like
    `openagentsearch.reputation.facts.mention_targets`."""
    found: set[str] = set()
    for match in DID_TOKEN_RE.finditer(text):
        did = known.get(match.group(0))
        if did is not None:
            found.add(did)
    for regex in (AT_MENTION_RE, ABBR_MENTION_RE, ABBR_MENTION_ELLIPSIS_RE):
        for match in regex.finditer(text):
            did = known.get(match.group(1))
            if did is not None:
                found.add(did)
    found.discard(sender)
    return tuple(sorted(found))


def mask_text(text: str) -> str:
    """`text` with every DID-shaped token replaced by `<did>`, then hex runs (`\\b[0-9a-f]{6,}\\b`)
    replaced by `<hex>`, then digit runs replaced by `0`, then
    `openagentsearch.reputation.facts.normalize_text`-ed -- used to judge whether two rows carry
    "the same" text once identity- and count-shaped noise is stripped (`TEMPLATE_MIN_REPEATS`).

    A kibble grammar line is never masked or counted as a template by ANY caller in this package
    (`is_kibble_line` is checked first everywhere templates are computed) -- this function itself
    does not know or care, so calling it directly on a kibble line still returns a (meaningless,
    for this package's purposes) masked string rather than raising."""
    replaced = DID_TOKEN_RE.sub("<did>", text)
    replaced = AT_MENTION_RE.sub("<did>", replaced)
    replaced = ABBR_MENTION_RE.sub("<did>", replaced)
    replaced = ABBR_MENTION_ELLIPSIS_RE.sub("<did>", replaced)
    replaced = _HEX_RUN_RE.sub("<hex>", replaced)
    replaced = _DIGIT_RUN_RE.sub("0", replaced)
    return normalize_text(replaced)


def masked_text_counts(rows: Sequence[Row]) -> Counter[str]:
    """`{mask_text(text): count}` over every row in `rows` that is NOT a kibble grammar line
    (`is_kibble_line` checked first). Used identically by `rooms.room_facts` (window and all-time)
    and `agents.agent_signals` (a room's all-time scope, per the agent facts table) so the two
    never compute "is this text a template here" two different ways."""
    counts: Counter[str] = Counter()
    for row in rows:
        text = row[3]
        if is_kibble_line(text):
            continue
        counts[mask_text(text)] += 1
    return counts


def work_cycles(rows: Sequence[Row]) -> dict[str, frozenset[str]]:
    """Completed kibble-v1 work cycles found in `rows`: `{job_id: frozenset(senders of any
    non-JOB stage line of that completed cycle)}` -- a job whose stages never reach a completed
    cycle is absent from the result entirely (there is nothing to attach members to).

    A completed cycle for one `job_id` is: its kibble-grammar stage lines, taken in ascending
    `seq` order (this function sorts `rows` by `seq` itself -- callers need not pre-sort), matching
    the subsequence `JOB`, then `CLAIM`, then `RESULT` or `DELIVER`, then `ATTEST` (other stages,
    and repeats, are ignored -- a subsequence match, not an exact match). Every row in `rows`
    contributes to whether a job's stage sequence completes, signed or not; membership in the
    returned frozenset is signed rows only (a row with `signed=False` is never added to a job's
    member set, matching this package's "unsigned rows never count toward an agent's signals"
    rule) -- callers that want a ROOM's `work_cycles_completed` (which counts completion over ALL
    rows and never needs membership) simply ignore the frozensets; callers that want an AGENT's
    completed-cycle count pre-filter `rows` to signed rows only before calling this function, so
    an unsigned stage line can never even start a cycle for that view (see `agents.agent_signals`).

    NOT guaranteed: a `job_id` string used in two different rooms describes two entirely separate
    jobs to this function -- it only ever sees the rows from ONE call, and every caller in this
    package always scopes `rows` to one room before calling."""
    ordered = sorted(rows, key=lambda r: r[0])
    stage_rows_by_job: dict[str, list[tuple[str, str, bool]]] = {}
    for _seq, _ts, sender, text, signed in ordered:
        stage = kibble_stage(text)
        if stage is None:
            continue
        stage_name, job_id = stage
        stage_rows_by_job.setdefault(job_id, []).append((stage_name, sender, signed))

    result: dict[str, frozenset[str]] = {}
    for job_id, stage_rows in stage_rows_by_job.items():
        index = 0
        completed = False
        for stage_name, _sender, _signed in stage_rows:
            wanted = _CYCLE_STAGES[index]
            hit = stage_name == wanted or (isinstance(wanted, tuple) and stage_name in wanted)
            if hit:
                index += 1
                if index == len(_CYCLE_STAGES):
                    completed = True
                    break
        if completed:
            members = {s for stage_name, s, signed in stage_rows if stage_name != "JOB" and signed}
            result[job_id] = frozenset(members)
    return result


def p95(values: Sequence[int]) -> int:
    """The 95th percentile of `values` by the nearest-rank method (`ceil(0.95 * n)`-th smallest,
    1-indexed) over the non-empty buckets given -- `0` for an empty `values`. Callers pass only
    the counts of buckets that actually had at least one row; an empty bucket is never a `0` in
    `values` (see `rooms.room_facts`'s `rows_per_5min_p95`)."""
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return ordered[rank - 1]


def iso8601_utc(timestamp: float) -> str:
    """`timestamp` (Unix epoch seconds) as a `Z`-suffixed ISO-8601 string in UTC, whole-second
    precision -- the same convention `openagentsearch.reputation.ledger._iso8601_utc` uses."""
    stamp = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")
    return stamp.replace("+00:00", "Z")
