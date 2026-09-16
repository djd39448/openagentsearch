"""Package B1, facts.py: load_posts over the log, normalize_text, mention_targets, build_facts
(distinct-text ratio, mention edges, burst detection boundaries).

Offline, deterministic, nothing hangs: every test either uses the committed
`tests/fixtures/reputation/ledger-20.jsonl` (via `reputation_fixture_support.install_fixture`) or
builds a small in-memory `Post` list directly.
"""

from pathlib import Path

from openagentsearch.reputation.facts import (
    DidFacts,
    Post,
    build_facts,
    is_burst_member,
    load_posts,
    mention_targets,
    normalize_text,
)

from reputation_fixture_support import (
    COLLIDE_SUFFIX,
    DID_A,
    DID_B,
    DID_C,
    DID_D,
    DID_E,
    DID_F,
    DID_G,
    NOW,
    install_fixture,
)

# ============================================================== load_posts


def test_load_posts_counts_the_fixtures_rows_exactly(tmp_path: Path):
    root = install_fixture(tmp_path)
    posts, report = load_posts(root)
    assert report.rows == report.posts + report.skipped_malformed + report.skipped_unsigned
    assert report.rooms == ("b1",)
    assert report.skipped_malformed == 1  # the one unparseable-ts row
    assert report.skipped_unsigned == 2  # the two bare-name "k3w" rows
    assert len(posts) == report.posts
    # deterministic order: (room, seq)
    assert [p.seq for p in posts] == sorted(p.seq for p in posts)


def test_load_posts_room_filter_and_missing_room_are_not_errors(tmp_path: Path):
    root = install_fixture(tmp_path)
    posts, report = load_posts(root, rooms=("b1", "does-not-exist"))
    assert report.rooms == ("b1", "does-not-exist")
    assert len(posts) > 0
    posts_empty, report_empty = load_posts(root, rooms=("nope",))
    assert posts_empty == []
    assert report_empty.rows == 0


def test_unsigned_rows_never_become_posts(tmp_path: Path):
    root = install_fixture(tmp_path)
    posts, _ = load_posts(root)
    assert not any(p.sender == "k3w" for p in posts)


# ============================================================== normalize_text


def test_normalize_text_casefolds_and_collapses_whitespace():
    assert normalize_text("  Hello   WORLD  \n") == "hello world"


def test_normalize_text_casefold_handles_sharp_s():
    # str.casefold() (not merely str.lower()) turns German "ß" into "ss".
    assert normalize_text("Straße") == "strasse"


def test_normalize_text_nfkc_normalizes():
    # U+FF21..FF23 (fullwidth Latin A/B/C) NFKC-normalize to plain ASCII "ABC".
    assert normalize_text("ＡＢＣ") == "abc"


# ============================================================== mention_targets


def test_mention_targets_full_token_and_at_suffix():
    # base58 excludes 0, O, I, l -- every literal below is deliberately built from safe
    # characters only, so the regex's own charset never truncates a "token" mid-string.
    known = {"suffixaa": "did:key:zsuffixAA"}
    text = "hello did:key:zfuLLtoken123 and @suffixaa"
    targets = mention_targets(text, known)
    assert "did:key:zfuLLtoken123" in targets
    assert "did:key:zsuffixAA" in targets
    assert targets == tuple(sorted(set(targets)))  # sorted, deduplicated


def test_mention_targets_unknown_suffix_resolves_to_nothing():
    assert mention_targets("hey @unknown1", {}) == ()


def test_mention_targets_does_not_exclude_sender_itself():
    # mention_targets has no sender parameter -- self-exclusion is the CALLER's job
    # (build_facts). Directly, a self-referencing full token is returned like any other.
    text = "did:key:zsametoken123"
    assert mention_targets(text, {}) == ("did:key:zsametoken123",)


def test_mention_targets_at_suffix_requires_exactly_eight_chars():
    known = {"abcdefgh": "did:key:zTARGET"}
    # nine base58 chars after '@' -- must NOT match the 8-char suffix as a prefix
    assert mention_targets("@abcdefghi", known) == ()
    assert mention_targets("@abcdefgh", known) == ("did:key:zTARGET",)


# ============================================================== build_facts against the fixture


def test_did_a_facts_from_fixture(tmp_path: Path):
    root = install_fixture(tmp_path)
    posts, _ = load_posts(root)
    facts = build_facts(posts)
    a = facts[DID_A]
    assert a.post_count == 3
    assert a.distinct_text_count == 3
    assert a.distinct_text_ratio == 1.0
    assert a.first_seen_ts == NOW - 60 * 86400.0
    assert {from_did for from_did, _count in a.inbound_mentions} == {DID_C, DID_D}
    assert a.burst_id is None
    assert not is_burst_member(a)


def test_did_b_is_a_rate_burst_member_not_a_group_burst(tmp_path: Path):
    root = install_fixture(tmp_path)
    posts, _ = load_posts(root)
    facts = build_facts(posts)
    b = facts[DID_B]
    assert b.post_count == 200
    assert b.burst_id is None  # not a first-seen-cluster burst (only 20 real DIDs)
    assert b.max_posts_per_minute >= 20
    assert is_burst_member(b)


def test_ambiguous_suffix_counts_unresolved_and_resolves_to_neither_collider(tmp_path: Path):
    root = install_fixture(tmp_path)
    posts, _ = load_posts(root)
    facts = build_facts(posts)
    g = facts[DID_G]
    assert g.unresolved_mentions == 1
    e = facts[DID_E]
    f = facts[DID_F]
    assert DID_E[-8:] == COLLIDE_SUFFIX
    assert DID_F[-8:] == COLLIDE_SUFFIX
    assert not any(from_did == g.did for from_did, _ in e.inbound_mentions)
    assert not any(from_did == g.did for from_did, _ in f.inbound_mentions)


def test_build_facts_never_creates_a_self_mention_edge():
    # mention_targets itself has no sender parameter (see its own docstring), but the system
    # built on it (build_facts) must never record a DID mentioning itself, by either mention
    # form -- this is the end-to-end property the B1 spec's "mention_targets never returns the
    # sender" test item is checking.
    did = "did:key:zsamedidbotAA"
    posts = [
        Post(room="r", seq=0, ts=1.0, sender=did, text=f"hello {did} yourself", signed=True),
        Post(room="r", seq=1, ts=2.0, sender=did, text=f"hi @{did[-8:]}", signed=True),
    ]
    facts = build_facts(posts)
    f = facts[did]
    assert f.outbound_mentions == ()
    assert f.inbound_mentions == ()
    assert f.unresolved_mentions == 0


def test_every_fixture_did_gets_facts_and_no_extras(tmp_path: Path):
    root = install_fixture(tmp_path)
    posts, _ = load_posts(root)
    facts = build_facts(posts)
    assert len(facts) == 20
    assert all(isinstance(f, DidFacts) for f in facts.values())


# ============================================================== burst detection boundaries


def _synthetic_first_seen_posts(count: int, *, start_ts: float, spacing_s: float) -> list[Post]:
    return [
        Post(
            room="r",
            seq=i,
            ts=start_ts + i * spacing_s,
            sender=f"did:key:zboundary{i:04d}",
            text=f"post number {i}",
            signed=True,
        )
        for i in range(count)
    ]


def test_burst_window_boundary_49_no_burst_50_yes():
    # 49 DIDs, first-seen 1s apart -> span 48s, all within a 60s window, but 49 < min_new(50).
    posts_49 = _synthetic_first_seen_posts(49, start_ts=1_700_000_000.0, spacing_s=1.0)
    facts_49 = build_facts(posts_49, burst_window_s=60.0, burst_min_new=50)
    assert all(f.burst_id is None for f in facts_49.values())

    # 50 DIDs, same spacing -> span 49s, still within the window, and now >= min_new(50).
    posts_50 = _synthetic_first_seen_posts(50, start_ts=1_700_000_000.0, spacing_s=1.0)
    facts_50 = build_facts(posts_50, burst_window_s=60.0, burst_min_new=50)
    burst_ids = {f.burst_id for f in facts_50.values()}
    assert burst_ids == {0}


def test_per_did_burst_threshold_boundary_19_no_20_yes():
    start = 1_700_000_000.0
    posts_19 = [
        Post(
            room="r", seq=i, ts=start + i * 3.0, sender="did:key:zrateboundary",
            text=f"m{i}", signed=True,
        )
        for i in range(19)
    ]
    facts_19 = build_facts(posts_19)
    d19 = facts_19["did:key:zrateboundary"]
    assert d19.max_posts_per_minute == 19
    assert not is_burst_member(d19, per_did_burst_per_minute=20)

    posts_20 = [
        Post(
            room="r", seq=i, ts=start + i * 3.0, sender="did:key:zrateboundary",
            text=f"m{i}", signed=True,
        )
        for i in range(20)
    ]
    facts_20 = build_facts(posts_20)
    d20 = facts_20["did:key:zrateboundary"]
    assert d20.max_posts_per_minute == 20
    assert is_burst_member(d20, per_did_burst_per_minute=20)
    assert d20.burst_id is None  # per-DID rate burst never gets a group burst_id


# ============================================================== determinism


def test_build_facts_is_deterministic(tmp_path: Path):
    root = install_fixture(tmp_path)
    posts, _ = load_posts(root)
    facts_1 = build_facts(posts)
    facts_2 = build_facts(list(posts))
    assert facts_1 == facts_2


def test_mention_targets_resolves_the_rooms_abbreviated_form():
    """`re z6Mk..HKkZ` -- the room's own abbreviation (prefix, "..", last 4 characters) -- resolves
    through a 4-character suffix in `known`; a 4-character suffix absent from `known` resolves to
    nothing; the abbreviation never matches inside a longer base58 run."""
    target = "did:key:z6MkAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHKkZ"
    known = {"HKkZ": target}
    text = "on that: /healthz is never rate limited (re z6Mk..HKkZ)"
    assert mention_targets(text, known) == (target,)
    assert mention_targets("following z6MkfVWR..HKkZ -- related", known) == (target,)
    assert mention_targets("re z6Mk..QQQQ nobody", known) == ()
    assert mention_targets("z6Mk..HKkZx", known) == ()


def test_build_facts_resolves_abbreviated_mentions_and_counts_ambiguous_ones():
    """End to end: an abbreviated mention becomes an inbound edge when its 4-character suffix is
    unique among senders, and is counted as unresolved (with no edge) when two senders share it."""
    target = "did:key:z6MkTARGETTARGETTARGETTARGETTARGETTARGETTARGETHKkZ"
    twin_a = "did:key:z6MkTWINATWINATWINATWINATWINATWINATWINATWINAZZZZ"
    twin_b = "did:key:z6MkTWINBTWINBTWINBTWINBTWINBTWINBTWINBTWINBZZZZ"
    speaker = "did:key:z6MkSPEAKERSPEAKERSPEAKERSPEAKERSPEAKERSPEAKR1234"
    posts = [
        Post(room="r", seq=1, ts=1_700_000_000.0, sender=target, text="hello", signed=True),
        Post(room="r", seq=2, ts=1_700_000_001.0, sender=twin_a, text="a", signed=True),
        Post(room="r", seq=3, ts=1_700_000_002.0, sender=twin_b, text="b", signed=True),
        Post(room="r", seq=4, ts=1_700_000_003.0, sender=speaker,
             text="re z6Mk..HKkZ agreed; and z6Mk..ZZZZ which one?", signed=True),
    ]
    facts = build_facts(posts)
    assert facts[target].inbound_mentions == ((speaker, 1),)
    assert facts[speaker].outbound_mentions == (target,)
    assert facts[speaker].unresolved_mentions == 1
    assert facts[twin_a].inbound_mentions == () and facts[twin_b].inbound_mentions == ()
