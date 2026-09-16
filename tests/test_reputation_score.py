"""Package B1, score.py: score_did's formula, burst forcing 0.0, facts_used recomputability,
and rank()'s ordering -- against the committed 20-DID fixture (no burst injection here; see
tests/test_reputation_done_when.py for the 2,000-identity burst scenario)."""

from pathlib import Path

import pytest

from openagentsearch.reputation.facts import build_facts, is_burst_member, load_posts
from openagentsearch.reputation.score import Score, rank, score_did

from reputation_fixture_support import DID_A, DID_B, NOW, install_fixture

DAY_S = 86400.0


def _all_facts(tmp_path: Path):
    root = install_fixture(tmp_path)
    posts, _ = load_posts(root)
    return build_facts(posts)


def test_did_a_score_matches_the_hand_computed_formula(tmp_path: Path):
    all_facts = _all_facts(tmp_path)
    a = all_facts[DID_A]
    score = score_did(a, all_facts=all_facts, now=NOW)
    assert score.burst is False
    # age_days = 60.0 exactly (first_seen 60 days before NOW); ratio = 1.0; two non-burst
    # inbound mentions (DID C, DID D) -> 60.0 * 1.0 * 3 = 180.0
    assert score.score == 180.0


def test_burst_member_always_scores_zero_regardless_of_other_facts(tmp_path: Path):
    all_facts = _all_facts(tmp_path)
    b = all_facts[DID_B]
    assert is_burst_member(b)
    score = score_did(b, all_facts=all_facts, now=NOW)
    assert score.burst is True
    assert score.score == 0.0


def test_facts_used_is_non_empty_and_recomputable_for_every_did(tmp_path: Path):
    all_facts = _all_facts(tmp_path)
    for did, facts in all_facts.items():
        score = score_did(facts, all_facts=all_facts, now=NOW)
        assert score.did == did
        assert len(score.facts_used) > 0
        values = dict(score.facts_used)
        assert set(values) == {
            "age_days",
            "distinct_text_ratio",
            "inbound_from_non_burst",
            "burst",
            "post_count",
        }
        age_days = float(values["age_days"])
        ratio = float(values["distinct_text_ratio"])
        inbound = int(values["inbound_from_non_burst"])
        is_burst = values["burst"] == "true"
        recomputed = 0.0 if is_burst else round(age_days * ratio * (1 + inbound), 6)
        assert recomputed == score.score


def test_score_is_deterministic_no_clock_no_randomness(tmp_path: Path):
    all_facts = _all_facts(tmp_path)
    a = all_facts[DID_A]
    s1 = score_did(a, all_facts=all_facts, now=NOW)
    s2 = score_did(a, all_facts=all_facts, now=NOW)
    assert s1 == s2


def test_max_age_days_caps_older_identities(tmp_path: Path):
    all_facts = _all_facts(tmp_path)
    a = all_facts[DID_A]  # 60 days old
    score_uncapped = score_did(a, all_facts=all_facts, now=NOW, max_age_days=90.0)
    score_capped = score_did(a, all_facts=all_facts, now=NOW, max_age_days=30.0)
    assert dict(score_uncapped.facts_used)["age_days"] == "60.0"
    assert dict(score_capped.facts_used)["age_days"] == "30.0"
    assert score_capped.score < score_uncapped.score


def test_age_days_is_clamped_at_zero_when_now_precedes_first_seen(tmp_path: Path):
    all_facts = _all_facts(tmp_path)
    a = all_facts[DID_A]
    # A stale/incorrect `now` (or clock skew) that lands BEFORE first_seen_ts must not produce a
    # negative age_days, and therefore never a negative score, for a non-burst DID.
    score = score_did(a, all_facts=all_facts, now=a.first_seen_ts - DAY_S)
    assert score.burst is False
    assert dict(score.facts_used)["age_days"] == "0.0"
    assert score.score == 0.0
    assert score.score >= 0.0


def test_rank_sorts_by_score_desc_then_did_asc(tmp_path: Path):
    all_facts = _all_facts(tmp_path)
    ranked = rank(all_facts, now=NOW)
    assert isinstance(ranked, list)
    assert all(isinstance(s, Score) for s in ranked)
    assert len(ranked) == len(all_facts)
    scores = [s.score for s in ranked]
    assert scores == sorted(scores, reverse=True)
    # ties (all the zero-score burst members, if any) are broken by did ascending
    zero_score_dids = [s.did for s in ranked if s.score == 0.0]
    assert zero_score_dids == sorted(zero_score_dids)
    assert ranked[0].did == DID_A  # DID A is the single highest-scoring identity in the fixture


def test_score_post_init_rejects_nonzero_score_on_a_burst_member():
    with pytest.raises(ValueError):
        Score(did="did:key:zx", score=1.0, burst=True, facts_used=(("a", "b"),))


def test_score_post_init_rejects_empty_facts_used():
    with pytest.raises(ValueError):
        Score(did="did:key:zx", score=0.0, burst=False, facts_used=())
