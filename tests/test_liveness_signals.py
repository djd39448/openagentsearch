"""LM1-SPEC section 3: regex sets and the shared text-level primitives in
`openagentsearch.liveness.signals`. Each pattern gets one real-log-shaped positive example and one
negative; `mask_text`, `work_cycles`, `reply_targets` (including the U+2026 short form and the
"@nickname is NOT a reply" rule), and `p95` each get their own focused tests."""

from openagentsearch.liveness.signals import (
    FAUCET_ONBOARDING_PATTERNS,
    KIBBLE_LINE_RE,
    REPLY_HEAD_RE,
    build_known_suffix_map,
    is_kibble_line,
    kibble_stage,
    mask_text,
    masked_text_counts,
    matches_faucet_onboarding,
    p95,
    reply_targets,
    starts_as_reply,
    work_cycles,
)

DID_A = "did:key:z6MkQs9bRt7YabcJ8x1HKkZ"
DID_B = "did:key:z6MkQs9bRt7YabcJ8x2uizf"
DID_C = "did:key:z6MkQs9bRt7YabcJ8x3wxyz"


def test_faucet_pattern_faucet_claim():
    text = "FLOP testnet faucet claim. DID: did:key:zAbc"
    assert matches_faucet_onboarding(text)
    assert not matches_faucet_onboarding("no relation to that phrase at all")


def test_faucet_pattern_check_in():
    assert matches_faucet_onboarding("Checking in. Still trying to wrap my head around this.")
    assert not matches_faucet_onboarding("I already checked the logs yesterday")


def test_faucet_pattern_agent_presence():
    assert matches_faucet_onboarding("Agent presence active, ready for the airdrop epoch.")
    assert not matches_faucet_onboarding("the agents were present at the meeting")


def test_faucet_pattern_heartbeat():
    assert matches_faucet_onboarding("- [a9b8c7] heartbeat stable at 4ms latency, all clear")
    assert not matches_faucet_onboarding("my heart is beating fast today")


def test_faucet_pattern_network_participant():
    assert matches_faucet_onboarding("FLOP network participant #1478. Agent infrastructure operational.")
    assert not matches_faucet_onboarding("participant list attached below")


def test_faucet_pattern_infrastructure_operational():
    assert matches_faucet_onboarding("infrastructure operational, no incidents overnight")
    assert not matches_faucet_onboarding("the infrastructure needs an upgrade eventually")


def test_faucet_pattern_meta_layer():
    assert matches_faucet_onboarding("observing technocore meta-layer for anomalies")
    assert matches_faucet_onboarding("meta-layer engaged, syncing state")
    assert not matches_faucet_onboarding("let's meta-discuss the roadmap sometime")


def test_faucet_pattern_ready_for_airdrop():
    assert matches_faucet_onboarding("ready for the airdrop, all checks passed")
    assert not matches_faucet_onboarding("ready for the meeting at noon")


def test_faucet_pattern_greeting():
    assert matches_faucet_onboarding("Hello! I am an autonomous AI agent, glad to be here.")
    assert matches_faucet_onboarding("gm, excited to be part of this network")
    assert not matches_faucet_onboarding("well hello there, fancy seeing you here")


def test_faucet_pattern_lobby_meta_phrases():
    assert matches_faucet_onboarding("autonomous participation logged for this cycle")
    assert matches_faucet_onboarding("did active, identity maintained")
    assert matches_faucet_onboarding("agent online and ready")
    assert matches_faucet_onboarding("node online, awaiting jobs")
    assert not matches_faucet_onboarding("the node was offline for maintenance")


def test_faucet_pattern_fleet_test():
    assert matches_faucet_onboarding("census beacon fleet-test/v1 payload attached")
    assert not matches_faucet_onboarding("fleet test scheduled for next week")


def test_faucet_pattern_count_matches_documented_list():
    assert len(FAUCET_ONBOARDING_PATTERNS) == 11


def test_kibble_line_re_positive_and_negative():
    assert is_kibble_line("JOB v1 | job-42 | summarize the corpus")
    assert is_kibble_line("ATTEST v1 | job-42 | verified, looks good")
    assert not is_kibble_line("just a regular chat message about jobs")
    assert not is_kibble_line("job v1 | job-42 | lowercase stage word never matches")


def test_kibble_stage_extracts_stage_and_job_id():
    assert kibble_stage("CLAIM v1 | job-42 | taking this one") == ("CLAIM", "job-42")
    assert kibble_stage("not a kibble line") is None


def test_kibble_line_re_is_not_matched_against_a_lobby_hello():
    # LM1-SPEC section 3 note: a kibble grammar line is never tested against the faucet list, and
    # a lobby "Hello..." row (NOT kibble grammar) is the correct positive example for pattern 9.
    hello_row = "Hello! I am an autonomous AI agent, glad to be here."
    assert not KIBBLE_LINE_RE.match(hello_row)
    assert matches_faucet_onboarding(hello_row)


def test_reply_head_re_positive_and_negative():
    assert starts_as_reply("Re: seq 56187 -- thanks for the pointer")
    assert starts_as_reply("re: quick question about the schema")
    assert not starts_as_reply("reference implementation is in the repo")  # "re" but not \bre\b head...
    assert not starts_as_reply("just a normal message, no reply prefix")


def test_reply_head_word_boundary():
    # "reference" starts with "re" but REPLY_HEAD_RE requires a word boundary after "re".
    assert REPLY_HEAD_RE.match("Re: seq 1") is not None
    assert REPLY_HEAD_RE.match("reference docs are here") is None


def test_at_nickname_is_not_a_reply():
    # A bare @nickname (not the 8-base58 short-mention form) is never a reply on its own.
    known = build_known_suffix_map([DID_A, DID_B, DID_C])
    text = "@vectis_observer checking in"
    assert not starts_as_reply(text)
    assert reply_targets("did:key:zSomeoneElse", text, known) == ()


def test_reply_targets_full_did_token():
    known = build_known_suffix_map([DID_A, DID_B])
    text = f"agreed with {DID_A} on that point"
    assert reply_targets(DID_B, text, known) == (DID_A,)


def test_reply_targets_at_8_short_form():
    known = build_known_suffix_map([DID_A, DID_B])
    text = f"thanks @{DID_A[-8:]} makes sense"
    assert reply_targets(DID_B, text, known) == (DID_A,)


def test_reply_targets_abbr_dotdot_form():
    known = build_known_suffix_map([DID_A, DID_B])
    text = f"following z6Mk..{DID_A[-4:]}"
    assert reply_targets(DID_B, text, known) == (DID_A,)


def test_reply_targets_abbr_ellipsis_form():
    known = build_known_suffix_map([DID_A, DID_B])
    text = f"following z6Mk…{DID_A[-4:]}"
    assert reply_targets(DID_B, text, known) == (DID_A,)


def test_reply_targets_full_did_token_must_name_a_sender_in_scope():
    # Measured 2026-09-18: a swarm bot's truncated "@did:key:z6Mkhe..." and a leaderboard bot's
    # full-length DID lists named nobody in scope and would otherwise have counted as replies.
    known = build_known_suffix_map([DID_A, DID_B])
    stranger = "did:key:z6MkStrangerNotInScopeXYZ12345"
    assert reply_targets(DID_B, f"seen {stranger} elsewhere", known) == ()
    assert reply_targets(DID_B, "@did:key:z6Mkhe... consensus looks solid", known) == ()
    # ... while the same text naming an in-scope sender still resolves.
    assert reply_targets(DID_B, f"seen {DID_A} elsewhere", known) == (DID_A,)


def test_reply_targets_never_includes_the_sender_itself():
    known = build_known_suffix_map([DID_A, DID_B])
    text = f"talking to myself: {DID_A}"
    assert reply_targets(DID_A, text, known) == ()


def test_reply_targets_ambiguous_suffix_resolves_to_nothing():
    # Two DIDs sharing the same last-8/last-4 characters -- neither suffix goes in `known`.
    collide_a = "did:key:z6MkAAAAAAAAAAAAAAAAAAAAcafJ8HKZ"
    collide_b = "did:key:z6MkBBBBBBBBBBBBBBBBBBBBcafJ8HKZ"
    known = build_known_suffix_map([collide_a, collide_b])
    text = f"@{collide_a[-8:]} hello"
    assert reply_targets("did:key:zSomeoneElse", text, known) == ()


def test_mask_text_replaces_dids_hex_and_digits():
    text = f"seen {DID_A} post tx a1b2c3d4e5 at block 12345"
    masked = mask_text(text)
    assert "<did>" in masked
    assert "<hex>" in masked
    assert DID_A not in masked
    assert "12345" not in masked
    assert "0" in masked


def test_mask_text_replaces_all_short_forms_too():
    at_form = mask_text(f"cc @{DID_A[-8:]}")
    dotdot_form = mask_text(f"cc z6Mk..{DID_A[-4:]}")
    ellipsis_form = mask_text(f"cc z6Mk…{DID_A[-4:]}")
    assert "<did>" in at_form
    assert "<did>" in dotdot_form
    assert "<did>" in ellipsis_form


def test_masked_text_counts_excludes_kibble_lines():
    rows = [
        (1, 0.0, DID_A, "JOB v1 | j1 | do the thing", True),
        (2, 1.0, DID_B, "JOB v1 | j1 | do the thing", True),
        (3, 2.0, DID_C, "hello there friend", True),
    ]
    counts = masked_text_counts(rows)
    # the two identical kibble lines never populate the counter at all
    assert sum(counts.values()) == 1


def test_work_cycles_completes_a_subsequence_match():
    rows = [
        (1, 0.0, DID_A, "JOB v1 | j1 | summarize", True),
        (2, 10.0, DID_B, "CLAIM v1 | j1 | taking it", True),
        (3, 20.0, DID_B, "SUBMIT v1 | j1 | irrelevant stage, ignored", True),
        (4, 30.0, DID_C, "DELIVER v1 | j1 | result attached", True),
        (5, 40.0, DID_A, "ATTEST v1 | j1 | verified", True),
    ]
    cycles = work_cycles(rows)
    assert set(cycles) == {"j1"}
    # membership: every non-JOB stage's signed sender, including the ignored SUBMIT stage
    assert cycles["j1"] == frozenset({DID_B, DID_C, DID_A})


def test_work_cycles_result_and_deliver_are_interchangeable():
    rows_result = [
        (1, 0.0, DID_A, "JOB v1 | j2 | x", True),
        (2, 1.0, DID_B, "CLAIM v1 | j2 | x", True),
        (3, 2.0, DID_B, "RESULT v1 | j2 | x", True),
        (4, 3.0, DID_A, "ATTEST v1 | j2 | x", True),
    ]
    assert "j2" in work_cycles(rows_result)


def test_work_cycles_incomplete_job_is_absent():
    rows = [
        (1, 0.0, DID_A, "JOB v1 | j3 | x", True),
        (2, 1.0, DID_B, "CLAIM v1 | j3 | x", True),
    ]
    assert work_cycles(rows) == {}


def test_work_cycles_unsigned_stage_never_starts_a_cycle_when_prefiltered():
    # Callers that want the agent-scoped view pre-filter to signed rows; an unsigned JOB line
    # dropped by that filter means the subsequence never even starts.
    rows = [
        (2, 1.0, DID_B, "CLAIM v1 | j4 | x", True),
        (3, 2.0, DID_C, "DELIVER v1 | j4 | x", True),
        (4, 3.0, DID_A, "ATTEST v1 | j4 | x", True),
    ]
    assert work_cycles(rows) == {}


def test_work_cycles_membership_is_signed_rows_only():
    rows = [
        (1, 0.0, DID_A, "JOB v1 | j5 | x", True),
        (2, 1.0, DID_B, "CLAIM v1 | j5 | x", False),  # unsigned: never a member
        (3, 2.0, DID_C, "DELIVER v1 | j5 | x", True),
        (4, 3.0, DID_C, "ATTEST v1 | j5 | x", True),
    ]
    cycles = work_cycles(rows)
    assert DID_B not in cycles["j5"]
    assert cycles["j5"] == frozenset({DID_C})


def test_p95_nearest_rank_over_non_empty_buckets():
    assert p95([]) == 0
    assert p95([5]) == 5
    assert p95([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == 10
    # nearest-rank: rank = ceil(0.95 * n); n=100 -> rank 95 -> the 95th smallest value
    assert p95(list(range(1, 101))) == 95
    # a small sample where the top value IS the 95th-percentile pick (rank == n)
    assert p95([1, 2, 150]) == 150


def test_build_known_suffix_map_excludes_ambiguous_suffixes():
    collide_a = "did:key:z6MkAAAAAAAAAAAAAAAAAAAAcafJ8HKZ"
    collide_b = "did:key:z6MkBBBBBBBBBBBBBBBBBBBBcafJ8HKZ"
    known = build_known_suffix_map([collide_a, collide_b, DID_A])
    assert collide_a[-4:] not in known  # shared suffix -> ambiguous -> excluded
    assert DID_A[-8:] in known and known[DID_A[-8:]] == DID_A
    assert DID_A[-4:] in known and known[DID_A[-4:]] == DID_A
