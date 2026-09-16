"""Per-DID facts, computed purely from already-logged message rows.

`load_posts` reads `log_root/messages/<room>.jsonl` -- the same on-disk file
`openagentsearch.sources.technocore_messages.MessageLog` writes and `RoomMessagesAdapter` reads --
and turns each accepted row into a `Post`. It reuses `RoomMessagesAdapter._parse_line` for the
line parser rather than re-implementing the on-disk row shape a second time, exactly as
`docs/message-log.md` documents it.

`build_facts` is pure: the same `Sequence[Post]` (and the same `notes`/burst parameters) always
produces byte-identical `DidFacts` through `ledger.to_jsonl_bytes`. Nothing here reads a clock,
touches the network, or trusts anything about `sig`/`nonce` beyond their presence -- see each
function's own docstring for what is NOT guaranteed.
"""

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from openagentsearch.sources.technocore_messages import RoomMessagesAdapter

# The live service's own base58 alphabet (no 0, O, I, l -- see docs/api.md's did:key pattern).
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_DID_SUFFIX_LEN = 8

# A full did:key:z... token, anywhere in a message's text.
_DID_TOKEN_RE = re.compile(rf"did:key:z[{_BASE58}]{{1,120}}")
# "@" followed by EXACTLY 8 base58 characters (the room UI's short-mention form, e.g.
# "@oYdTuizf"): a trailing negative lookahead keeps this from matching a prefix of a longer run.
_AT_MENTION_RE = re.compile(rf"@([{_BASE58}]{{{_DID_SUFFIX_LEN}}})(?![{_BASE58}])")
# The room's own abbreviated rendering of a DID in running text, e.g. "re z6Mk..HKkZ" or
# "following z6Mk..uizf": the multibase prefix, up to four more characters, "..", then EXACTLY
# the DID's last 4 characters. Measured 2026-09-16 on 269k live rows: this form appears where the
# 8-character "@" form never resolves to a DID at all.
_ABBR_SUFFIX_LEN = 4
_ABBR_MENTION_RE = re.compile(
    rf"\bz6Mk[{_BASE58}]{{0,4}}\.\.([{_BASE58}]{{{_ABBR_SUFFIX_LEN}}})(?![{_BASE58}])"
)

# The default per-DID posting-rate burst threshold (messages inside any 60s window). It is a
# SCORING-time parameter: `build_facts` only records `max_posts_per_minute`; `is_burst_member`,
# `score.score_did` / `score.rank`, `ledger.build_ledger` and the CLI's
# `--per-did-burst-per-minute` all take it explicitly and default to this constant.
PER_DID_BURST_PER_MINUTE_DEFAULT = 20
BURST_WINDOW_S_DEFAULT = 60.0
BURST_MIN_NEW_DEFAULT = 50
_RATE_WINDOW_S = 60.0  # fixed 60s window for the per-DID posting-rate check (not burst_window_s)


@dataclass(frozen=True)
class Post:
    """One accepted message-log row: a signed post by an identity.

    `ts` is the row's server-assigned timestamp, already parsed from the ISO-8601 `Z` string to
    epoch seconds by `load_posts` -- a row whose `ts` does not parse never becomes a `Post` (see
    `load_posts`). `signed` is `sig != ""`; in practice every `Post` this module ever constructs
    has `signed == True`, because `load_posts` never turns an unsigned row into a `Post` at all
    (see `LoadReport.skipped_unsigned`) -- the field is still carried explicitly, per the B1 spec,
    rather than assumed by callers.
    """

    room: str
    seq: int
    ts: float
    sender: str
    text: str
    signed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.room, str) or not self.room:
            raise ValueError(f"room must be a non-empty string, got {self.room!r}")
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 0:
            raise ValueError(f"seq must be a non-negative int, got {self.seq!r}")
        if isinstance(self.ts, bool) or not isinstance(self.ts, (int, float)):
            raise ValueError(f"ts must be a number, got {self.ts!r}")
        object.__setattr__(self, "ts", float(self.ts))
        if not isinstance(self.sender, str) or not self.sender:
            raise ValueError(f"sender must be a non-empty string, got {self.sender!r}")
        if not isinstance(self.text, str):
            raise ValueError(f"text must be a string, got {self.text!r}")
        if not isinstance(self.signed, bool):
            raise ValueError(f"signed must be a bool, got {self.signed!r}")


@dataclass(frozen=True)
class LoadReport:
    """What one `load_posts()` call read: `rows` is every non-blank JSONL line seen across the
    selected rooms; `posts` is `len()` of the returned list; `skipped_malformed` counts lines that
    failed to parse at all OR parsed but carried a `ts` that does not parse as ISO-8601; and
    `skipped_unsigned` counts lines that parsed fine but carried an empty `sig` (never a `Post` --
    see `Post`'s docstring). `rows == posts + skipped_malformed + skipped_unsigned` always holds.
    `rooms` is every room id actually considered, sorted.
    """

    rows: int
    posts: int
    skipped_malformed: int
    skipped_unsigned: int
    rooms: tuple[str, ...]


def _parse_ts(ts: str) -> float | None:
    """`ts` (an ISO-8601 string, ordinarily `Z`-suffixed) as epoch seconds, or `None` if it does
    not parse. Never raises."""
    text = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def load_posts(
    log_root: Path, *, rooms: Sequence[str] | None = None
) -> tuple[list[Post], LoadReport]:
    """Read `log_root/messages/<room>.jsonl` for every room (or just `rooms`, if given) and
    return the accepted `Post`s plus a `LoadReport`.

    Deterministic order: by `(room, seq)`. A malformed line (bad JSON, wrong shape, a `room`
    field that does not match the file it came from -- the same checks
    `RoomMessagesAdapter._parse_line` already makes) or a line whose `ts` does not parse is
    skipped and counted in `LoadReport.skipped_malformed`; a line that parses fine but carries an
    empty `sig` is skipped and counted in `LoadReport.skipped_unsigned` -- neither ever raises.

    NOT guaranteed: a room named in `rooms` whose `.jsonl` file does not exist on disk yields no
    rows for that room, not an error -- exactly like `RoomMessagesAdapter`. This function trusts
    `sig`'s mere presence as "signed"; it never verifies a signature against its sender (nothing
    in this repository does -- see `technocore_messages`'s own module docstring).
    """
    log_root = Path(log_root)
    # Only `_parse_line` is reused from the adapter -- room selection below is this function's
    # own (trivial) directory listing, since `_room_ids()` is the same one-liner either way.
    adapter = RoomMessagesAdapter(log_root)
    room_ids = (
        sorted(rooms) if rooms is not None else sorted(p.stem for p in _room_files(log_root))
    )

    posts: list[Post] = []
    rows = 0
    skipped_malformed = 0
    skipped_unsigned = 0
    for room in room_ids:
        path = log_root / "messages" / f"{room}.jsonl"
        if not path.is_file():
            continue
        with path.open("rb") as fh:
            for raw_line in fh:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                rows += 1
                # Reuses the adapter's own on-disk row parser rather than re-implementing it --
                # see this module's docstring and the B1 spec's "Read first" section.
                message = adapter._parse_line(stripped, room)
                if message is None:
                    skipped_malformed += 1
                    continue
                ts_epoch = _parse_ts(message.ts)
                if ts_epoch is None:
                    skipped_malformed += 1
                    continue
                if message.sig == "":
                    skipped_unsigned += 1
                    continue
                posts.append(
                    Post(
                        room=room,
                        seq=message.seq,
                        ts=ts_epoch,
                        sender=message.sender,
                        text=message.text,
                        signed=True,
                    )
                )

    posts.sort(key=lambda p: (p.room, p.seq))
    report = LoadReport(
        rows=rows,
        posts=len(posts),
        skipped_malformed=skipped_malformed,
        skipped_unsigned=skipped_unsigned,
        rooms=tuple(room_ids),
    )
    return posts, report


def _room_files(log_root: Path) -> list[Path]:
    messages_dir = Path(log_root) / "messages"
    if not messages_dir.is_dir():
        return []
    return sorted(messages_dir.glob("*.jsonl"))


def normalize_text(text: str) -> str:
    """`text`, NFKC-normalized, casefolded, and whitespace-collapsed+stripped. Used only to judge
    whether two posts carry "the same" text (`DidFacts.distinct_text_ratio`) -- never used to
    alter a stored `Post.text`, and not a general-purpose text-cleaning function."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def mention_targets(text: str, known: Mapping[str, str]) -> tuple[str, ...]:
    """DIDs `text` addresses: every full `did:key:z...` token found anywhere in it, plus every
    `@`-followed-by-exactly-8-base58-characters short mention (the room UI's own rendering, e.g.
    `@oYdTuizf`) or the room's abbreviated form `z6Mk..` + exactly 4 base58 characters (e.g.
    `re z6Mk..HKkZ`), either of which resolves through `known` (a `suffix -> DID` map holding
    8-character and 4-character suffixes that belong to exactly ONE DID; a mention whose suffix
    is absent from `known` -- because it names no DID at all, or because it is genuinely
    ambiguous between more than one -- resolves to nothing here). Sorted, deduplicated.

    NOT guaranteed: this function does not know who sent `text`, so it cannot exclude the sender
    from its own result -- callers that need "never the sender itself" (`build_facts` does) must
    filter `post.sender` out of the returned tuple themselves. It also does not report *why* an
    `@`-mention failed to resolve (unknown vs. ambiguous suffix); `build_facts` computes
    `DidFacts.unresolved_mentions` itself, from the same ambiguous-suffix set it built `known`
    from, rather than through this function's return value.
    """
    found: set[str] = set()
    for match in _DID_TOKEN_RE.finditer(text):
        found.add(match.group(0))
    for regex in (_AT_MENTION_RE, _ABBR_MENTION_RE):
        for match in regex.finditer(text):
            did = known.get(match.group(1))
            if did is not None:
                found.add(did)
    return tuple(sorted(found))


@dataclass(frozen=True)
class DidFacts:
    """Everything this package knows about one DID, computed purely from the message log (plus,
    optionally, its own self-authored `did-*` note -- see `notes.py`). Every field here is a
    fact, not a judgment: `score.py` is where facts become a ranking number.

    `inbound_mentions` is `((from_did, count), ...)`, sorted ascending by `from_did` -- one entry
    per distinct DID that has ever mentioned this one, with how many times. `outbound_mentions` is
    this DID's own distinct mention targets, sorted. `unresolved_mentions` counts this DID's own
    `@`-short-mentions whose suffix was genuinely ambiguous (matched more than one known DID) --
    not mentions that simply named nobody at all, which are not counted anywhere.

    `burst_id` is the id of the first-seen-timestamp cluster this DID belongs to (`None` if it
    belongs to none); `max_posts_per_minute` is the largest number of this DID's OWN posts found
    inside any 60-second window, regardless of whether that is over any threshold. Burst
    membership itself is `burst_id is not None or max_posts_per_minute >=
    PER_DID_BURST_PER_MINUTE_DEFAULT` -- see `is_burst_member`.

    NOT guaranteed: nothing here re-verifies `sig` against `sender` (see `Post`); `github_login`
    is only ever read from a note the DID wrote about itself and is never checked against any
    real GitHub account (see `notes.py`) -- `github_login_source` labels this plainly so a reader
    never mistakes it for a verified identity link.
    """

    did: str
    first_seen_seq: int
    first_seen_ts: float
    first_seen_room: str
    last_seen_ts: float
    post_count: int
    rooms_posted: tuple[str, ...]
    distinct_text_count: int
    distinct_text_ratio: float
    outbound_mentions: tuple[str, ...]
    inbound_mentions: tuple[tuple[str, int], ...]
    unresolved_mentions: int
    github_login: str | None
    github_login_source: str
    burst_id: int | None
    max_posts_per_minute: int

    def __post_init__(self) -> None:
        if not isinstance(self.did, str) or not self.did:
            raise ValueError(f"did must be a non-empty string, got {self.did!r}")
        _require_nonneg_int(self.first_seen_seq, "first_seen_seq")
        _require_finite_number(self.first_seen_ts, "first_seen_ts")
        object.__setattr__(self, "first_seen_ts", float(self.first_seen_ts))
        if not isinstance(self.first_seen_room, str) or not self.first_seen_room:
            raise ValueError("first_seen_room must be a non-empty string")
        _require_finite_number(self.last_seen_ts, "last_seen_ts")
        object.__setattr__(self, "last_seen_ts", float(self.last_seen_ts))
        _require_nonneg_int(self.post_count, "post_count")
        if self.post_count < 1:
            raise ValueError(f"post_count must be >= 1, got {self.post_count!r}")
        _require_sorted_str_tuple(self.rooms_posted, "rooms_posted")
        _require_nonneg_int(self.distinct_text_count, "distinct_text_count")
        if self.distinct_text_count > self.post_count:
            raise ValueError("distinct_text_count cannot exceed post_count")
        _require_finite_number(self.distinct_text_ratio, "distinct_text_ratio")
        object.__setattr__(self, "distinct_text_ratio", float(self.distinct_text_ratio))
        if not (0.0 <= self.distinct_text_ratio <= 1.0):
            raise ValueError("distinct_text_ratio must be within [0.0, 1.0]")
        _require_sorted_str_tuple(self.outbound_mentions, "outbound_mentions")
        _require_inbound_mentions(self.inbound_mentions)
        _require_nonneg_int(self.unresolved_mentions, "unresolved_mentions")
        if self.github_login is not None and not isinstance(self.github_login, str):
            raise ValueError(f"github_login must be a string or None, got {self.github_login!r}")
        if self.github_login_source not in ("", "did-note (convention strength, unverified)"):
            raise ValueError(f"unexpected github_login_source: {self.github_login_source!r}")
        if (self.github_login is None) != (self.github_login_source == ""):
            raise ValueError("github_login and github_login_source must agree on presence")
        if self.burst_id is not None:
            _require_nonneg_int(self.burst_id, "burst_id")
        _require_nonneg_int(self.max_posts_per_minute, "max_posts_per_minute")


def _require_finite_number(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")


def _require_nonneg_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative int, got {value!r}")


def _require_sorted_str_tuple(value: object, name: str) -> None:
    if not isinstance(value, tuple) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{name} must be a tuple of strings, got {value!r}")
    if list(value) != sorted(set(value)):
        raise ValueError(f"{name} must be sorted and deduplicated, got {value!r}")


def _require_inbound_mentions(value: object) -> None:
    if not isinstance(value, tuple):
        raise ValueError(f"inbound_mentions must be a tuple, got {value!r}")
    froms: list[str] = []
    for pair in value:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or isinstance(pair[1], bool)
            or not isinstance(pair[1], int)
            or pair[1] < 1
        ):
            raise ValueError(
                f"each inbound_mentions entry must be a (str, positive int) pair, got {pair!r}"
            )
        froms.append(pair[0])
    if len(froms) != len(set(froms)):
        raise ValueError(f"inbound_mentions from_did values must be unique, got {froms!r}")
    if list(value) != sorted(value):
        raise ValueError(f"inbound_mentions must be sorted by from_did, got {value!r}")


def is_burst_member(
    facts: DidFacts, *, per_did_burst_per_minute: int = PER_DID_BURST_PER_MINUTE_DEFAULT
) -> bool:
    """`facts.burst_id is not None or facts.max_posts_per_minute >= per_did_burst_per_minute` --
    the burst-membership predicate the B1 spec defines. `score.score_did`/`rank` pass their own
    `per_did_burst_per_minute` through to this function, so one threshold governs both a DID's
    own membership and that of the DIDs mentioning it."""
    return facts.burst_id is not None or facts.max_posts_per_minute >= per_did_burst_per_minute


def _max_posts_per_minute(timestamps: Sequence[float]) -> int:
    """The largest number of `timestamps` found inside any window of `_RATE_WINDOW_S` (60)
    seconds -- a standard ascending sliding-window maximum-count scan. `0` for an empty input."""
    ordered = sorted(timestamps)
    best = 0
    lo = 0
    for hi in range(len(ordered)):
        while ordered[hi] - ordered[lo] > _RATE_WINDOW_S:
            lo += 1
        best = max(best, hi - lo + 1)
    return best


def _group_bursts(
    first_seen: Sequence[tuple[float, str]], *, window_s: float, min_new: int
) -> dict[str, int]:
    """Deterministic, single-pass greedy grouping over `first_seen` (`(ts, did)` pairs, already
    sorted ascending by `(ts, did)`): starting from the earliest not-yet-grouped point, extend a
    candidate group as far right as possible while its span (`last.ts - first.ts`) stays within
    `window_s`; if the resulting group has `>= min_new` members, every member gets the same
    `burst_id` (assigned from 0, in the time order groups are closed) and the scan resumes right
    after the group; otherwise the scan simply advances by one and tries the next start point.

    This is a left-to-right greedy grouping, not an exhaustive search over every possible window
    placement -- it is what makes the simple boundary cases (`N-1` points within one window ->
    no group; `N` -> one group covering all of them) exactly the outcome BUILDSPEC/B1-SPEC
    describes, but it does NOT claim to find every conceivable maximal overlapping cluster in
    adversarial, unevenly-spaced input; see `docs/reputation.md` for the documented rule this
    implements.
    """
    burst_id_by_did: dict[str, int] = {}
    next_id = 0
    n = len(first_seen)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and first_seen[j + 1][0] - first_seen[i][0] <= window_s:
            j += 1
        group = first_seen[i : j + 1]
        if len(group) >= min_new:
            for _, did in group:
                burst_id_by_did[did] = next_id
            next_id += 1
            i = j + 1
        else:
            i += 1
    return burst_id_by_did


def build_facts(
    posts: Sequence[Post],
    *,
    notes: Mapping[str, str] | None = None,
    burst_window_s: float = BURST_WINDOW_S_DEFAULT,
    burst_min_new: int = BURST_MIN_NEW_DEFAULT,
) -> dict[str, DidFacts]:
    """Every `DidFacts` computable from `posts` (and, optionally, self-authored `did-*` notes),
    keyed by `did`. Pure: the same `posts`/`notes`/burst parameters always produce the same
    result (see the module docstring).

    `known` (the `suffix -> DID` map `mention_targets` resolves short mentions through) is built
    from every distinct `sender` in `posts`, keyed by each sender's last 8 characters (the `@`
    form) AND its last 4 characters (the `z6Mk..xxxx` form) -- a suffix shared by more than one
    sender is NEVER put in `known` (a mention using it resolves to nothing there), and
    separately, any short mention using one of those ambiguous suffixes increments the SENDING
    DID's own `unresolved_mentions`. The per-DID posting-rate threshold is NOT a parameter here:
    `max_posts_per_minute` is recorded as a fact and `is_burst_member` / `score.py` apply the
    threshold.

    NOT guaranteed: a DID mentioned (by full token or by an unambiguous `@`-suffix) who never
    itself sent a post in `posts` gets no `DidFacts` entry at all -- there is nothing to attach an
    inbound-mention count to -- so that mention is invisible everywhere except the mentioning
    DID's own `outbound_mentions`. Burst grouping is the specific greedy algorithm documented on
    `_group_bursts`, not an exhaustive maximal-clustering search.
    """
    notes = notes or {}

    posts_by_did: dict[str, list[Post]] = {}
    for post in posts:
        posts_by_did.setdefault(post.sender, []).append(post)
    senders = sorted(posts_by_did)

    suffix_map: dict[str, set[str]] = {}
    for did in senders:
        for length in (_DID_SUFFIX_LEN, _ABBR_SUFFIX_LEN):
            suffix = did[-length:] if len(did) >= length else did
            suffix_map.setdefault(suffix, set()).add(did)
    known: dict[str, str] = {
        suffix: next(iter(dids)) for suffix, dids in suffix_map.items() if len(dids) == 1
    }
    ambiguous_suffixes = {suffix for suffix, dids in suffix_map.items() if len(dids) > 1}

    outbound: dict[str, set[str]] = {did: set() for did in senders}
    inbound: dict[str, dict[str, int]] = {did: {} for did in senders}
    unresolved: dict[str, int] = dict.fromkeys(senders, 0)

    for post in posts:
        targets = [d for d in mention_targets(post.text, known) if d != post.sender]
        for target in targets:
            outbound[post.sender].add(target)
            if target in inbound:
                inbound[target][post.sender] = inbound[target].get(post.sender, 0) + 1
        for regex in (_AT_MENTION_RE, _ABBR_MENTION_RE):
            for match in regex.finditer(post.text):
                if match.group(1) in ambiguous_suffixes:
                    unresolved[post.sender] += 1

    first_seen_ts_by_did: dict[str, float] = {}
    for did in senders:
        dposts = sorted(posts_by_did[did], key=lambda p: (p.ts, p.room, p.seq))
        first_seen_ts_by_did[did] = dposts[0].ts

    ordered_first_seen = sorted((first_seen_ts_by_did[d], d) for d in senders)
    burst_id_by_did = _group_bursts(
        ordered_first_seen, window_s=burst_window_s, min_new=burst_min_new
    )

    result: dict[str, DidFacts] = {}
    for did in senders:
        dposts = sorted(posts_by_did[did], key=lambda p: (p.ts, p.room, p.seq))
        first_post = dposts[0]
        distinct_texts = {normalize_text(p.text) for p in dposts}
        post_count = len(dposts)
        distinct_text_count = len(distinct_texts)
        github_login = notes.get(did)
        github_login_source = (
            "did-note (convention strength, unverified)" if github_login is not None else ""
        )
        result[did] = DidFacts(
            did=did,
            first_seen_seq=first_post.seq,
            first_seen_ts=first_post.ts,
            first_seen_room=first_post.room,
            last_seen_ts=max(p.ts for p in dposts),
            post_count=post_count,
            rooms_posted=tuple(sorted({p.room for p in dposts})),
            distinct_text_count=distinct_text_count,
            distinct_text_ratio=distinct_text_count / post_count,
            outbound_mentions=tuple(sorted(outbound[did])),
            inbound_mentions=tuple(sorted(inbound[did].items())),
            unresolved_mentions=unresolved[did],
            github_login=github_login,
            github_login_source=github_login_source,
            burst_id=burst_id_by_did.get(did),
            max_posts_per_minute=_max_posts_per_minute([p.ts for p in dposts]),
        )
    return result
