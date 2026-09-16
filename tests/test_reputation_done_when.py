"""Package B1's done-when test (spec Deliverable item 6): a 2,000-identity burst, generated
INSIDE this test (never committed -- see `reputation_fixture_support.make_burst_posts`), merged
with the committed 20-DID fixture.

Asserts exactly what the spec requires: after `rank()`, every one of the 2,000 injected burst
identities scores `0.0` and sits at the bottom (below every positive-scoring identity); DID A
(whose two real inbound mentions from non-burst DIDs C and D still count, while the 2,000 burst
mentions of it do not) is in the top 3; and every `Score.facts_used` is non-empty and
recomputable -- checked here by recomputing DID A's own score from its `facts_used` pairs alone.
"""

import time
from pathlib import Path

from openagentsearch.reputation.facts import build_facts, is_burst_member, load_posts
from openagentsearch.reputation.score import rank

from reputation_fixture_support import DID_A, NOW, install_fixture, make_burst_posts

BURST_COUNT = 2000


def test_a_2000_identity_burst_scores_zero_and_sinks_while_did_a_stays_top_3(tmp_path: Path):
    root = install_fixture(tmp_path)
    fixture_posts, load_report = load_posts(root)
    assert load_report.rooms == ("b1",)

    burst_posts = make_burst_posts(count=BURST_COUNT, start_ts=NOW - 100.0, window_s=40.0)
    burst_dids = {p.sender for p in burst_posts}
    assert len(burst_dids) == BURST_COUNT  # every synthetic identity is distinct

    all_posts = list(fixture_posts) + burst_posts
    all_facts = build_facts(all_posts)  # default burst_window_s=60, burst_min_new=50
    assert len(all_facts) == 20 + BURST_COUNT

    # The burst is genuinely detected as ONE group (all 2,000 first-seen within the 40s window,
    # well under the default 60s burst_window_s, and 2,000 >= the default burst_min_new of 50).
    burst_ids_seen = {all_facts[d].burst_id for d in burst_dids}
    assert burst_ids_seen != {None}
    assert all(is_burst_member(all_facts[d]) for d in burst_dids)

    scored = rank(all_facts, now=NOW)
    assert len(scored) == 20 + BURST_COUNT
    by_did = {s.did: s for s in scored}

    # 1. Every burst DID scores exactly 0.0 and is flagged burst=True.
    for did in burst_dids:
        assert by_did[did].score == 0.0
        assert by_did[did].burst is True

    # 2. DID A is in the top 3 -- its two real inbound mentions (from non-burst DIDs C and D)
    #    count; the 2,000 burst mentions of it (all from burst members) contribute nothing.
    top_3_dids = [s.did for s in scored[:3]]
    assert DID_A in top_3_dids

    a_score = by_did[DID_A]
    a_values = dict(a_score.facts_used)
    assert int(a_values["inbound_from_non_burst"]) == 2  # unchanged by the 2,000 burst mentions

    # 3. Every burst DID sits strictly below DID A in the ranking (score-tie-broken-by-did order
    #    still places all positive scorers, DID A included, ahead of every 0.0 scorer).
    a_rank = next(i for i, s in enumerate(scored) if s.did == DID_A)
    burst_ranks = [i for i, s in enumerate(scored) if s.did in burst_dids]
    assert a_rank < min(burst_ranks)

    # 4. Every Score.facts_used is non-empty, and DID A's is exactly recomputable from those
    #    pairs alone (no access to `all_facts`/`DidFacts` needed).
    for score in scored:
        assert len(score.facts_used) > 0
    age_days = float(a_values["age_days"])
    ratio = float(a_values["distinct_text_ratio"])
    inbound = int(a_values["inbound_from_non_burst"])
    is_burst = a_values["burst"] == "true"
    recomputed = 0.0 if is_burst else round(age_days * ratio * (1 + inbound), 6)
    assert recomputed == a_score.score
    assert a_score.score == 180.0  # 60.0 age_days * 1.0 ratio * (1 + 2 inbound)


def test_burst_build_and_rank_completes_quickly(tmp_path: Path):
    """Sanity guard against an accidentally quadratic implementation: 2,020 identities is small,
    this must complete well within pytest's own collection/run time, never hang."""
    root = install_fixture(tmp_path)
    fixture_posts, _ = load_posts(root)
    burst_posts = make_burst_posts(count=BURST_COUNT, start_ts=NOW - 100.0, window_s=40.0)
    started = time.perf_counter()
    all_facts = build_facts(list(fixture_posts) + burst_posts)
    rank(all_facts, now=NOW)
    elapsed = time.perf_counter() - started
    assert elapsed < 10.0
