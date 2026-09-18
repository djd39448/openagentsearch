"""LM1-SPEC section 4/5: `room_facts` and `classify_room`. Every table branch, the
`unknown` -> `quiet` history fallback, the empty-scope facts, and `decided_on` recomputing the
class from the row alone."""

from openagentsearch.liveness.rooms import (
    RoomFacts,
    classify_room,
    room_facts,
    room_facts_to_obj,
    room_verdict_to_obj,
)
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
)


# base58 excludes '0'/'O'/'I'/'l' -- see openagentsearch.reputation.facts._BASE58. Map each
# decimal digit to a distinct base58-safe character so a DID built from a zero-padded index never
# contains an invalid character (which would silently truncate DID_TOKEN_RE matches against it and
# corrupt every mention/reply computation in these tests).
_DIGIT_MAP = str.maketrans("0123456789", "123456789A")


def _did(tag: str, n: int) -> str:
    digits = f"{n:04d}".translate(_DIGIT_MAP)
    return f"did:key:z6MkQs9bRt7Y{tag}{digits}abcJ"


_FILLER = [
    "checking the build output", "reviewing the latest changes", "syncing with the team",
    "looking at the failing test", "updating the documentation", "investigating the slow query",
    "drafting the release notes", "triaging the open issues", "pairing on the tricky bug",
    "cleaning up the changelog", "verifying the deploy steps", "revisiting the design doc",
    "polishing the error messages", "adding a missing test case", "refactoring the helper module",
    "measuring the benchmark results", "sketching the next milestone", "closing out old threads",
    "confirming the rollback plan", "walking through the runbook", "checking disk usage today",
    "auditing the access logs", "tuning the cache settings", "scanning for dead code",
    "writing a small script", "validating the config schema", "chasing a flaky failure",
    "summarizing the week's work", "catching up on notifications", "exploring a new approach",
]


def _rows(n_rows: int, n_senders: int, *, base_ts: float = 1_000_000.0, text_fn=None, signed=True):
    """`n_rows` rows spread evenly across `n_senders` distinct DIDs, one second apart. Default
    text cycles through `_FILLER` (30 distinct, non-numeric, non-mention, non-faucet-shaped
    phrases) so a room built with no `text_fn` override never accidentally reads as templated,
    faucet/onboarding, or reply-shaped."""
    text_fn = text_fn or (lambda i, s: _FILLER[i % len(_FILLER)])
    rows = []
    for i in range(n_rows):
        sender = _did("S", i % n_senders)
        rows.append((i + 1, base_ts + i, sender, text_fn(i, sender), signed))
    return rows


def test_empty_scope_yields_zeros_and_nulls():
    f = room_facts([], burst_dids=set(), gaps_recorded=None)
    assert f.rows == 0
    assert f.distinct_senders == 0
    assert f.first_ts is None and f.last_ts is None
    assert f.burst_sender_share is None
    assert f.span_hours == 0.0


def test_below_room_min_rows_is_unknown():
    rows = _rows(ROOM_MIN_ROWS - 1, 5)
    f = room_facts(rows, burst_dids=set(), gaps_recorded=None)
    verdict = classify_room(f, f, room="r")
    assert verdict.class_ == "unknown"
    assert verdict.class_all == "unknown"


def test_below_room_min_senders_is_unknown():
    rows = _rows(ROOM_MIN_ROWS + 5, 2)
    f = room_facts(rows, burst_dids=set(), gaps_recorded=None)
    verdict = classify_room(f, f, room="r")
    assert verdict.class_ == "unknown"


def test_quiet_when_no_signal_fires():
    rows = _rows(30, 10)
    f = room_facts(rows, burst_dids=set(), gaps_recorded=None)
    verdict = classify_room(f, f, room="r")
    assert verdict.class_ == "quiet"
    assert verdict.signals.farm is False
    assert verdict.signals.live is False
    assert verdict.signals.flood is False


def test_live_via_reply_senders():
    def text_fn(i, sender):
        if i < LIVE_MIN_REPLY_SENDERS:
            return "Re: seq 1 -- following up on this"
        return f"generic row {i}"

    rows = _rows(30, 10, text_fn=text_fn)
    f = room_facts(rows, burst_dids=set(), gaps_recorded=None)
    verdict = classify_room(f, f, room="r")
    assert f.reply_senders >= LIVE_MIN_REPLY_SENDERS
    assert verdict.signals.live is True
    assert verdict.class_ == "live"


def test_live_via_work_cycle():
    rows = [
        (1, 1_000_000.0, _did("W", 0), "JOB v1 | j1 | do it", True),
        (2, 1_000_010.0, _did("W", 1), "CLAIM v1 | j1 | mine", True),
        (3, 1_000_020.0, _did("W", 2), "DELIVER v1 | j1 | done", True),
        (4, 1_000_030.0, _did("W", 3), "ATTEST v1 | j1 | good", True),
        *_rows(20, 8, base_ts=1_000_100.0),
    ]
    f = room_facts(rows, burst_dids=set(), gaps_recorded=None)
    verdict = classify_room(f, f, room="r")
    assert f.work_cycles_completed >= LIVE_MIN_WORK_CYCLES
    assert verdict.signals.live is True


def test_farm_via_one_line_share():
    rows = _rows(FARM_MIN_SENDERS + 5, FARM_MIN_SENDERS + 5)  # one row per sender
    f = room_facts(rows, burst_dids=set(), gaps_recorded=None)
    assert f.one_line_sender_share >= FARM_ONE_LINE_SHARE
    verdict = classify_room(f, f, room="r")
    assert verdict.signals.farm is True
    assert verdict.class_ == "farm"


def test_farm_via_template_sender_share():
    def text_fn(i, sender):
        # sender S0 posts the same text >= TEMPLATE_MIN_REPEATS times room-wide
        if sender == _did("S", 0):
            return "identical templated line for majority testing"
        return f"unique row {i}"

    # give S0 many rows, each identical -> its own template_sender_share hits majority
    rows = []
    seq = 1
    for i in range(6):
        rows.append((seq, 1_000_000.0 + seq, _did("S", 0), "identical templated line for majority testing", True))
        seq += 1
    for i in range(1, FARM_MIN_SENDERS):
        rows.append((seq, 1_000_000.0 + seq, _did("S", i), f"unique row {i}", True))
        seq += 1
    f = room_facts(rows, burst_dids=set(), gaps_recorded=None)
    assert f.template_sender_share >= FARM_TEMPLATE_SENDER_SHARE
    verdict = classify_room(f, f, room="r")
    assert verdict.signals.farm is True


def test_farm_via_faucet_sender_share():
    def text_fn(i, sender):
        return "gm, checking in for the first time today"

    rows = _rows(FARM_MIN_SENDERS + 2, FARM_MIN_SENDERS + 2, text_fn=text_fn)
    f = room_facts(rows, burst_dids=set(), gaps_recorded=None)
    assert f.faucet_onboarding_sender_share >= FARM_FAUCET_SENDER_SHARE
    verdict = classify_room(f, f, room="r")
    assert verdict.signals.farm is True


def test_farm_via_burst_sender_share():
    rows = _rows(FARM_MIN_SENDERS + 3, FARM_MIN_SENDERS + 3)
    signed_senders = {r[2] for r in rows if r[4]}
    burst = set(list(signed_senders)[: int(len(signed_senders) * 0.6)])
    f = room_facts(rows, burst_dids=burst, gaps_recorded=None)
    assert f.burst_sender_share is not None and f.burst_sender_share >= FARM_BURST_SENDER_SHARE
    verdict = classify_room(f, f, room="r")
    assert verdict.signals.farm is True


def test_mixed_when_farm_and_live_both_fire():
    def text_fn(i, sender):
        return "Re: seq 1 -- following up on this"  # every row is a reply -> live, and one-line -> farm

    rows = _rows(FARM_MIN_SENDERS + 5, FARM_MIN_SENDERS + 5, text_fn=text_fn)
    f = room_facts(rows, burst_dids=set(), gaps_recorded=None)
    verdict = classify_room(f, f, room="r")
    assert verdict.signals.farm is True
    assert verdict.signals.live is True
    assert verdict.class_ == "mixed"


def test_flood_wins_over_farm_and_live():
    # every row in the same 300s bucket, plus reply text and one-line senders (farm+live also true)
    anchor = 2_000_000.0
    anchor -= anchor % 300  # align to a bucket start so the whole burst fits in one bucket
    anchor += 1.0
    rows = []
    for i in range(FLOOD_ROWS_PER_5MIN_P95 + 5):
        sender = _did("F", i)
        rows.append((i + 1, anchor + i, sender, "Re: seq 1 -- reply text here", True))
    f = room_facts(rows, burst_dids=set(), gaps_recorded=None)
    assert f.rows_per_5min_p95 >= FLOOD_ROWS_PER_5MIN_P95
    verdict = classify_room(f, f, room="r")
    assert verdict.signals.flood is True
    assert verdict.class_ == "flood"


def test_unknown_window_falls_back_to_quiet_when_all_time_has_standing():
    window = room_facts(_rows(ROOM_MIN_ROWS - 5, 5), burst_dids=set(), gaps_recorded=None)
    all_time = room_facts(_rows(30, 10), burst_dids=set(), gaps_recorded=None)
    verdict = classify_room(window, all_time, room="r")
    assert verdict.class_ == "quiet"
    assert verdict.class_all == "quiet"
    assert any(entry[0] == "class_all" for entry in verdict.decided_on)


def test_unknown_window_stays_unknown_when_all_time_also_unknown():
    window = room_facts(_rows(5, 3), burst_dids=set(), gaps_recorded=None)
    all_time = room_facts(_rows(5, 3), burst_dids=set(), gaps_recorded=None)
    verdict = classify_room(window, all_time, room="r")
    assert verdict.class_ == "unknown"
    assert verdict.class_all == "unknown"


def test_gaps_recorded_and_burst_sender_share_are_carried_through():
    rows = _rows(25, 5)
    f = room_facts(rows, burst_dids=set(), gaps_recorded=3)
    assert f.gaps_recorded == 3
    assert f.burst_sender_share == 0.0  # no burst dids given, but signed senders exist


def _recompute_class_from_decided_on(decided_on):
    """A minimal, independent re-implementation of the table, driven only by `decided_on`'s
    recorded (name, value, threshold) triples -- proving the artifact is self-describing."""
    by_name = {}
    for name, value, _threshold in decided_on:
        by_name.setdefault(name, []).append(value)

    def as_float(name):
        return float(by_name[name][0])

    if as_float("rows") < ROOM_MIN_ROWS or as_float("distinct_senders") < ROOM_MIN_SENDERS:
        return "unknown"
    if as_float("rows_per_5min_p95") >= FLOOD_ROWS_PER_5MIN_P95:
        return "flood"
    farm = as_float("distinct_senders_farm_gate") >= FARM_MIN_SENDERS and (
        as_float("one_line_sender_share") >= FARM_ONE_LINE_SHARE
        or as_float("template_sender_share") >= FARM_TEMPLATE_SENDER_SHARE
        or as_float("faucet_onboarding_sender_share") >= FARM_FAUCET_SENDER_SHARE
        or (by_name["burst_sender_share"][0] != "None" and as_float("burst_sender_share") >= FARM_BURST_SENDER_SHARE)
    )
    live = as_float("work_cycles_completed") >= LIVE_MIN_WORK_CYCLES or (
        as_float("reply_senders") >= LIVE_MIN_REPLY_SENDERS
        and as_float("reply_sender_share") >= LIVE_REPLY_SENDER_SHARE
    )
    if farm and live:
        return "mixed"
    if farm:
        return "farm"
    if live:
        return "live"
    return "quiet"


def test_decided_on_recomputes_the_window_class():
    scenarios = [
        _rows(30, 10),
        _rows(FARM_MIN_SENDERS + 5, FARM_MIN_SENDERS + 5),
        _rows(ROOM_MIN_ROWS - 1, 5),
    ]
    for rows in scenarios:
        f = room_facts(rows, burst_dids=set(), gaps_recorded=None)
        verdict = classify_room(f, f, room="r")
        # the fallback entry (if present) is extra evidence, not part of the window table itself
        window_entries = [e for e in verdict.decided_on if e[0] != "class_all"]
        recomputed = _recompute_class_from_decided_on(window_entries)
        assert recomputed == verdict.class_


def test_room_facts_to_obj_and_verdict_to_obj_round_trip_shape():
    rows = _rows(30, 10)
    f = room_facts(rows, burst_dids=set(), gaps_recorded=None)
    verdict = classify_room(f, f, room="r")
    fobj = room_facts_to_obj(f)
    assert fobj["rows"] == f.rows
    assert fobj["first_ts"] == f.first_ts
    vobj = room_verdict_to_obj(verdict)
    assert vobj["class"] == verdict.class_
    assert vobj["signals"] == {"farm": False, "live": False, "flood": False}


def test_room_facts_rejects_inconsistent_construction():
    import pytest

    with pytest.raises(ValueError):
        RoomFacts(
            rows=5, signed_rows=10, distinct_senders=1, distinct_texts=1, distinct_text_ratio=0.2,
            top_sender_share=0.5, one_line_sender_share=0.5, reply_rows=0, reply_row_share=0.0,
            reply_senders=0, reply_sender_share=0.0, template_rows=0, template_row_share=0.0,
            template_senders=0, template_sender_share=0.0, faucet_onboarding_rows=0,
            faucet_onboarding_row_share=0.0, faucet_onboarding_senders=0,
            faucet_onboarding_sender_share=0.0, work_cycle_rows=0, work_cycles_completed=0,
            rows_per_5min_p95=0, rows_per_5min_max=0, gaps_recorded=None, burst_sender_share=None,
            first_ts=None, last_ts=None, span_hours=0.0,
        )
