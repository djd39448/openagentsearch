"""LM1-SPEC section 6: `AgentSignals`/`classify_agent`'s points table, tier rule, `unsigned_rows`
being informational only, and the compact 12-element array."""

import pytest

from openagentsearch.liveness.agents import (
    AgentSignals,
    agent_signals_to_compact_array,
    agent_verdict_to_obj,
    classify_agent,
)
from openagentsearch.liveness.signals import AGENT_POINTS, TIER_LIKELY_MIN, TIER_LIVE_MIN, TIER_WEAK_MIN

DID = "did:key:z6MkQs9bRt7YabcJ8x1HKkZ"


def _signals(**overrides) -> AgentSignals:
    base = dict(
        did=DID,
        post_count=3,
        unsigned_rows=0,
        rooms=(("room-a", 3),),
        rooms_count=1,
        live_rooms_count=0,
        reply_out=0,
        reply_in=0,
        reply_in_distinct=0,
        reply_in_nonburst=0,
        distinct_text_ratio=0.5,
        template_rows=0,
        faucet_onboarding_rows=0,
        work_cycles=0,
        github_contrib_rows=0,
        did_note_present=False,
        burst=False,
        age_days=0.0,
        first_seen_ts=0.0,
        last_seen_ts=0.0,
    )
    base.update(overrides)
    return AgentSignals(**base)


def test_burst_is_always_farm_regardless_of_points():
    s = _signals(burst=True, work_cycles=10, reply_in_nonburst=10, reply_in_distinct=10,
                 reply_in=10, reply_out=10, post_count=20,
                 rooms=(("room-a", 20),), distinct_text_ratio=1.0)
    v = classify_agent(s)
    assert v.tier == "farm"
    assert v.points > 0  # points still computed and shown, per spec


def test_one_generic_row_is_unknown_even_with_a_positive_marker():
    # Dave's worked example: post_count=1, one positive marker (github_contrib), net points can
    # be >= 0, but the tier is still "unknown" because the ONLY negative marker is one_line.
    s = _signals(post_count=1, rooms=(("github-contrib", 1),), github_contrib_rows=1)
    v = classify_agent(s)
    assert v.points == 0  # -2 (one_line) + 2 (github_contrib_ge_1)
    assert v.tier == "unknown"


def test_single_post_with_a_second_negative_marker_is_not_the_special_unknown_case():
    # post_count == 1 AND a genuine second negative marker (faucet majority) -> falls through to
    # the ordinary points-based rule, not the "unknown" exception.
    s = _signals(post_count=1, faucet_onboarding_rows=1, rooms=(("room-a", 1),))
    v = classify_agent(s)
    assert v.points == -5  # -2 one_line, -3 faucet_onboarding_majority
    assert v.tier == "farm"


def test_no_marker_fired_at_all_is_unknown():
    s = _signals(post_count=2, rooms=(("room-a", 2),), distinct_text_ratio=0.5)
    v = classify_agent(s)
    assert v.used == ()
    assert v.points == 0
    assert v.tier == "unknown"


def test_work_cycles_points():
    s = _signals(post_count=5, rooms=(("room-a", 5),), work_cycles=1)
    v = classify_agent(s)
    assert ("work_cycles_ge_1", AGENT_POINTS["work_cycles_ge_1"], "work_cycles=1 >= 1") in v.used
    s5 = _signals(post_count=5, rooms=(("room-a", 5),), work_cycles=5)
    v5 = classify_agent(s5)
    names = {n for n, _p, _e in v5.used}
    assert {"work_cycles_ge_1", "work_cycles_ge_5"} <= names
    assert v5.points == AGENT_POINTS["work_cycles_ge_1"] + AGENT_POINTS["work_cycles_ge_5"]


def test_reply_in_nonburst_points():
    s1 = _signals(post_count=5, rooms=(("room-a", 5),), reply_in_nonburst=1,
                  reply_in_distinct=1, reply_in=1)
    assert ("reply_in_nonburst_ge_1", AGENT_POINTS["reply_in_nonburst_ge_1"], "reply_in_nonburst=1 >= 1") in classify_agent(s1).used
    s3 = _signals(post_count=5, rooms=(("room-a", 5),), reply_in_nonburst=3,
                  reply_in_distinct=3, reply_in=3)
    names = {n for n, _p, _e in classify_agent(s3).used}
    assert {"reply_in_nonburst_ge_1", "reply_in_nonburst_ge_3"} <= names


def test_reply_out_points():
    s1 = _signals(post_count=5, rooms=(("room-a", 5),), reply_out=1)
    assert ("reply_out_ge_1", AGENT_POINTS["reply_out_ge_1"], "reply_out=1 >= 1") in classify_agent(s1).used
    s5 = _signals(post_count=5, rooms=(("room-a", 5),), reply_out=5)
    names = {n for n, _p, _e in classify_agent(s5).used}
    assert {"reply_out_ge_1", "reply_out_ge_5"} <= names


def test_github_contrib_points():
    s = _signals(post_count=5, rooms=(("github-contrib", 5),), github_contrib_rows=1)
    assert ("github_contrib_ge_1", AGENT_POINTS["github_contrib_ge_1"], "github_contrib_rows=1 >= 1") in classify_agent(s).used


def test_live_rooms_points():
    s = _signals(post_count=5, rooms=(("room-a", 3), ("room-b", 2)), rooms_count=2,
                 live_rooms_count=2)
    assert ("live_rooms_ge_2", AGENT_POINTS["live_rooms_ge_2"], "live_rooms_count=2 >= 2") in classify_agent(s).used


def test_did_note_present_points():
    s = _signals(post_count=5, rooms=(("room-a", 5),), did_note_present=True)
    assert ("did_note_present", AGENT_POINTS["did_note_present"], "did_note_present=true") in classify_agent(s).used


def test_distinct_ratio_and_post_count_points():
    s = _signals(post_count=3, rooms=(("room-a", 3),), distinct_text_ratio=0.8)
    used = classify_agent(s).used
    assert any(n == "distinct_ge_0_8_and_posts_ge_3" for n, _p, _e in used)
    # below post_count=3 threshold, the bonus never fires even at ratio 1.0
    s2 = _signals(post_count=2, rooms=(("room-a", 2),), distinct_text_ratio=1.0)
    used2 = classify_agent(s2).used
    assert not any(n == "distinct_ge_0_8_and_posts_ge_3" for n, _p, _e in used2)


def test_age_points():
    s = _signals(post_count=5, rooms=(("room-a", 5),), age_days=7.0)
    assert ("age_ge_7d", AGENT_POINTS["age_ge_7d"], "age_days=7.0 >= 7") in classify_agent(s).used
    s2 = _signals(post_count=5, rooms=(("room-a", 5),), age_days=6.999)
    assert not any(n == "age_ge_7d" for n, _p, _e in classify_agent(s2).used)


def test_faucet_onboarding_majority_negative():
    s = _signals(post_count=4, rooms=(("room-a", 4),), faucet_onboarding_rows=2)
    used = classify_agent(s).used
    assert ("faucet_onboarding_majority", AGENT_POINTS["faucet_onboarding_majority"],
            "faucet_onboarding_rows=2 >= majority of post_count=4") in used


def test_template_majority_negative_requires_post_count_ge_3():
    s2 = _signals(post_count=2, rooms=(("room-a", 2),), template_rows=2)
    assert not any(n == "template_majority_and_posts_ge_3" for n, _p, _e in classify_agent(s2).used)
    s3 = _signals(post_count=3, rooms=(("room-a", 3),), template_rows=2)
    used3 = classify_agent(s3).used
    assert ("template_majority_and_posts_ge_3", AGENT_POINTS["template_majority_and_posts_ge_3"],
            "template_rows=2 >= majority of post_count=3 and post_count >= 3") in used3


def test_one_line_negative():
    s = _signals(post_count=1, rooms=(("room-a", 1),), work_cycles=1)  # avoid the unknown gate
    used = classify_agent(s).used
    assert ("one_line", AGENT_POINTS["one_line"], "post_count == 1") in used


def test_unsigned_rows_never_contributes_a_point():
    low = _signals(post_count=5, rooms=(("room-a", 5),), unsigned_rows=0, work_cycles=1)
    high = _signals(post_count=5, rooms=(("room-a", 5),), unsigned_rows=500, work_cycles=1)
    assert classify_agent(low).points == classify_agent(high).points
    assert classify_agent(low).used == classify_agent(high).used


def test_tier_thresholds():
    def with_points(target: int) -> AgentSignals:
        # work_cycles>=1 (+3) and >=5 (+1) = 4; reply_out>=1 (+2); reply_in_nonburst>=1 (+2) --
        # combine to reach each threshold exactly for this test.
        kwargs = dict(post_count=10, rooms=(("room-a", 10),))
        if target >= TIER_WEAK_MIN:
            kwargs["reply_out"] = 1  # +2
        if target >= TIER_LIKELY_MIN:
            kwargs["github_contrib_rows"] = 1  # +2 more (total 4)
            kwargs["rooms"] = (("github-contrib", 10),)
        if target >= TIER_LIVE_MIN:
            kwargs["work_cycles"] = 1  # +3 more (total 7)
        return _signals(**kwargs)

    assert classify_agent(with_points(TIER_WEAK_MIN)).tier == "weak"
    assert classify_agent(with_points(TIER_LIKELY_MIN)).tier == "likely_live"
    assert classify_agent(with_points(TIER_LIVE_MIN)).tier == "live"


def test_points_equal_sum_of_used():
    s = _signals(post_count=10, rooms=(("room-a", 10),), work_cycles=5, reply_out=5,
                 reply_in_nonburst=3, reply_in_distinct=3, reply_in=3, did_note_present=True,
                 age_days=10.0, distinct_text_ratio=0.9)
    v = classify_agent(s)
    assert v.points == sum(pts for _n, pts, _e in v.used)


def test_agent_signals_to_compact_array_shape_and_values():
    s = _signals(
        post_count=5, rooms=(("room-a", 5),), unsigned_rows=2, reply_in=1, reply_out=1,
        work_cycles=1, template_rows=1, faucet_onboarding_rows=1, github_contrib_rows=1,
        did_note_present=True,
    )
    v = classify_agent(s)
    arr = agent_signals_to_compact_array(s, v)
    assert len(arr) == 12
    assert arr == [
        v.tier, v.points, s.rooms_count, s.reply_in, s.reply_out, s.work_cycles,
        s.template_rows, s.faucet_onboarding_rows, s.github_contrib_rows, 1,
        s.post_count, s.unsigned_rows,
    ]


def test_agent_verdict_to_obj_shape():
    s = _signals(post_count=5, rooms=(("room-a", 5),), work_cycles=1)
    v = classify_agent(s)
    obj = agent_verdict_to_obj(v, s)
    assert obj["tier"] == v.tier
    assert obj["points"] == v.points
    assert obj["signals"]["did"] == DID


def test_agent_signals_rejects_inconsistent_construction():
    with pytest.raises(ValueError):
        _signals(post_count=5, rooms=(("room-a", 3),))  # rooms sum (3) != post_count (5)
    with pytest.raises(ValueError):
        _signals(reply_in=1, reply_in_distinct=2)  # distinct cannot exceed rows
