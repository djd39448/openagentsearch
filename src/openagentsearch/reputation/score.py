"""Evidence-weighted ranking over `DidFacts` (see `facts.py`).

`score_did` is the whole formula: `age_days x distinct_text_ratio x (1 + inbound_from_non_burst)`,
rounded to 6 decimal places -- with **identity count and post count never a multiplier** (BUILDSPEC
S2): a burst of any size cannot buy score, because every burst member scores exactly `0.0`
regardless of anything else about it (`score_did`'s first check), and because
`inbound_from_non_burst` only counts *distinct* mentioning DIDs that are not themselves burst
members, so 2,000 burst identities mentioning one target contribute at most however many of THOSE
2,000 are not burst members (in practice: none, since they are the burst).

Deterministic: no randomness, no wall clock -- `now` is always caller-supplied. Every `Score`
carries the exact facts it was computed from (`Score.facts_used`), specifically so a reader (or a
test) can recompute the score from those pairs alone without needing the original `DidFacts` or
`all_facts` mapping again.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from openagentsearch.reputation.facts import (
    PER_DID_BURST_PER_MINUTE_DEFAULT,
    DidFacts,
    is_burst_member,
)

DEFAULT_MAX_AGE_DAYS = 90.0


@dataclass(frozen=True)
class Score:
    """One DID's computed reputation score. `facts_used` is `((name, value-as-string), ...)`,
    always non-empty, always enough on its own to recompute `score` (given `burst`) -- see
    `score_did`'s docstring for the exact five pairs it records and their order.

    NOT guaranteed: this is a ranking signal computed from message-log evidence only -- it is not
    a claim about the identity's real-world trustworthiness, and it is not a signature
    verification (see `facts.Post`'s own docstring for what `signed` does and does not mean).
    """

    did: str
    score: float
    burst: bool
    facts_used: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.did, str) or not self.did:
            raise ValueError(f"did must be a non-empty string, got {self.did!r}")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise ValueError(f"score must be a number, got {self.score!r}")
        object.__setattr__(self, "score", float(self.score))
        if not isinstance(self.burst, bool):
            raise ValueError(f"burst must be a bool, got {self.burst!r}")
        if self.burst and self.score != 0.0:
            raise ValueError(f"a burst member must score 0.0, got {self.score!r}")
        if not isinstance(self.facts_used, tuple) or not self.facts_used:
            raise ValueError("facts_used must be a non-empty tuple")
        for pair in self.facts_used:
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or not isinstance(pair[1], str)
            ):
                raise ValueError(f"each facts_used entry must be a (str, str) pair, got {pair!r}")


def score_did(
    facts: DidFacts,
    *,
    all_facts: Mapping[str, DidFacts],
    now: float,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    per_did_burst_per_minute: int = PER_DID_BURST_PER_MINUTE_DEFAULT,
) -> Score:
    """`facts`'s evidence-weighted `Score`. `per_did_burst_per_minute` is the posting-rate
    threshold `is_burst_member` applies -- to `facts` itself and to every DID mentioning it.

    `age_days = max(0.0, min((now - facts.first_seen_ts) / 86400, max_age_days))`, rounded to 6 dp
    before it is used in the formula (not just for display) -- this is what makes the score
    exactly recomputable from `facts_used` alone, with no residual floating-point drift from the
    unrounded value. The `max(0.0, ...)` floor guards a `now` that precedes `first_seen_ts` (a
    stale/incorrect `--now` passed to the CLI, or clock skew between the message-log server and
    the machine building the ledger): without it a non-burst DID could score negative, which nothing
    else here rejects. `inbound_from_non_burst` is the count of DISTINCT DIDs in
    `facts.inbound_mentions` that (a) are themselves present in `all_facts` and (b) are not burst
    members per `is_burst_member` -- a DID mentioned by 2,000 burst identities gains nothing from
    them, because none of them pass (b); a `from_did` absent from `all_facts` altogether is never
    counted either way (there is no `DidFacts` to check burst membership against).

    `score = age_days x distinct_text_ratio x (1 + inbound_from_non_burst)`, rounded to 6 dp --
    UNLESS `facts` is itself a burst member (`is_burst_member(facts)`), in which case `score` is
    exactly `0.0` regardless of every other input; `post_count` is recorded in `facts_used` for
    transparency but is never part of the multiplication (count is never a ranking input --
    BUILDSPEC S2). `facts_used` always carries exactly these five pairs, in this order:
    `age_days`, `distinct_text_ratio`, `inbound_from_non_burst`, `burst` (`"true"`/`"false"`,
    lowercase), `post_count`.

    Pure: no randomness, no wall clock read here -- `now` is always the caller's value.
    """
    age_days = round(max(0.0, min((now - facts.first_seen_ts) / 86400.0, max_age_days)), 6)
    distinct_text_ratio = round(facts.distinct_text_ratio, 6)

    seen: set[str] = set()
    inbound_from_non_burst = 0
    for from_did, _count in facts.inbound_mentions:
        if from_did in seen:
            continue
        seen.add(from_did)
        from_facts = all_facts.get(from_did)
        if from_facts is not None and not is_burst_member(
            from_facts, per_did_burst_per_minute=per_did_burst_per_minute
        ):
            inbound_from_non_burst += 1

    burst = is_burst_member(facts, per_did_burst_per_minute=per_did_burst_per_minute)
    raw_score = round(age_days * distinct_text_ratio * (1 + inbound_from_non_burst), 6)
    score_value = 0.0 if burst else raw_score

    facts_used = (
        ("age_days", str(age_days)),
        ("distinct_text_ratio", str(distinct_text_ratio)),
        ("inbound_from_non_burst", str(inbound_from_non_burst)),
        ("burst", "true" if burst else "false"),
        ("post_count", str(facts.post_count)),
    )
    return Score(did=facts.did, score=score_value, burst=burst, facts_used=facts_used)


def rank(
    all_facts: Mapping[str, DidFacts],
    *,
    now: float,
    per_did_burst_per_minute: int = PER_DID_BURST_PER_MINUTE_DEFAULT,
) -> list[Score]:
    """Every DID in `all_facts`, scored via `score_did` and sorted by `(-score, did)` -- highest
    score first, ties broken by `did` ascending for a fully deterministic order. A burst of any
    size sorts to the bottom together (all scoring exactly `0.0`), ordered among themselves only
    by `did`."""
    return sorted(
        (
            score_did(
                facts,
                all_facts=all_facts,
                now=now,
                per_did_burst_per_minute=per_did_burst_per_minute,
            )
            for facts in all_facts.values()
        ),
        key=lambda s: (-s.score, s.did),
    )
